"""OAuth 2.1 client for remote MCP servers (MCP authorization, spec 2026-07-28).

Steps implemented here, all behind the SSRF guard of the tool router
(:class:`~app.modules.tools.netguard.GuardedTransport`):

1. ``discover``: find the Protected Resource Metadata (RFC 9728) from the
   ``WWW-Authenticate`` challenge of the MCP endpoint or the well-known URLs, check that its
   ``resource`` is the server URL, pick the authorization server and read its metadata
   (RFC 8414, OIDC fallback). The server must support PKCE ``S256``; every endpoint must be
   ``https`` on the issuer's host.
2. ``register``: Client ID Metadata Document when the server advertises it and Mia has a
   public https origin, otherwise Dynamic Client Registration (RFC 7591). Public client.
3. ``authorization_url``: authorization code + PKCE ``S256`` + ``resource`` (RFC 8707).
4. ``exchange_code`` / ``refresh``: token endpoint with ``code_verifier`` and ``resource``.
5. ``revoke``: RFC 7009 on the refresh and access token.

Secrets are never logged and never put in exception messages.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx2
from app.modules.connections.oauth import pkce
from app.modules.connections.oauth.errors import OAuthProtocolError, TransientOAuthError
from app.modules.tools.netguard import GuardedTransport, HostBlockedError, HostPolicy, Resolver
from app.modules.tools.netguard import system_resolver as _system_resolver

MAX_BODY_BYTES: Final = 256 * 1024
CLIENT_NAME: Final = "Mia"
HttpTransportFactory = Any  # Callable[[], httpx2.AsyncBaseTransport]; Any keeps tests simple

_CHALLENGE_PARAM = re.compile(r'(\w+)="([^"]*)"')


def canonical_resource(url: str) -> str:
    """RFC 8707 resource identifier of an MCP server: lowercase origin, no fragment/query."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise OAuthProtocolError("the MCP server URL must be https")
    netloc = parts.hostname.lower() + (f":{parts.port}" if parts.port not in (None, 443) else "")
    path = parts.path.rstrip("/") if parts.path != "/" else ""
    return urlunsplit(("https", netloc, path, "", ""))


@dataclass(frozen=True, slots=True)
class ServerAuth:
    """What discovery learned about one MCP server's authorization."""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None
    registration_endpoint: str | None
    cimd_supported: bool
    auth_methods: tuple[str, ...]
    scopes: str | None


@dataclass(frozen=True, slots=True)
class Registration:
    client_id: str
    client_secret: str | None
    auth_method: str


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float | None
    scope: str | None


class InvalidGrantError(Exception):
    """The refresh token (or code) was rejected: the user has to connect again."""


def _challenge_params(header: str | None) -> dict[str, str]:
    if not header or not header.lower().startswith("bearer"):
        return {}
    return {name.lower(): value for name, value in _CHALLENGE_PARAM.findall(header)}


def _https_on_host(url: object, host: str, what: str) -> str:
    if not isinstance(url, str):
        raise OAuthProtocolError(f"{what} is missing")
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != host:
        raise OAuthProtocolError(f"{what} must be an https URL on the issuer host")
    return url


def _strings(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


class OAuthClient:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        http_transport: HttpTransportFactory | None = None,
        timeout_s: float = 10.0,
        global_allowlist: Iterable[str] | None = None,
        clock: Any = time.time,
    ) -> None:
        self._resolver: Resolver = resolver or _system_resolver
        self._http_transport = http_transport
        self._timeout_s = timeout_s
        self._global_allowlist = (
            frozenset(global_allowlist) if global_allowlist is not None else None
        )
        self._clock = clock

    # -------------------------------------------------------------------- http

    def _client(self, hosts: Iterable[str]) -> httpx2.AsyncClient:
        transport = GuardedTransport(
            HostPolicy(hosts, global_allowlist=self._global_allowlist),
            resolver=self._resolver,
            inner=self._http_transport() if self._http_transport else None,
        )
        return httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(self._timeout_s),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "mia-connections/1"},
        )

    async def _send(
        self,
        client: httpx2.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[int, httpx2.Headers, bytes]:
        try:
            async with client.stream(method, url, **kwargs) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise OAuthProtocolError("the response is too large")
                return response.status_code, response.headers, bytes(body)
        except HostBlockedError as error:
            raise OAuthProtocolError("the host is not allowed") from error
        except httpx2.HTTPError as error:
            leaves = [error, error.__cause__]
            if any(isinstance(leaf, HostBlockedError) for leaf in leaves):
                raise OAuthProtocolError("the host is not allowed") from error
            raise TransientOAuthError(type(error).__name__) from error

    @staticmethod
    def _json(status: int, body: bytes, what: str) -> dict[str, Any]:
        if status != 200:
            raise OAuthProtocolError(f"{what}: HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError as error:
            raise OAuthProtocolError(f"{what}: not JSON") from error
        if not isinstance(data, dict):
            raise OAuthProtocolError(f"{what}: not an object")
        return data

    # --------------------------------------------------------------- discovery

    async def discover(self, server_url: str, network: Iterable[str]) -> ServerAuth:
        resource = canonical_resource(server_url)
        host = urlsplit(resource).hostname or ""
        hosts = {*network, host}
        async with self._client(hosts) as client:
            _, challenge = await self._probe(client, server_url)
            prm_url, scope = await self._prm_location(client, resource, challenge)
            prm = await self._protected_resource(client, prm_url, resource)
            issuer = self._issuer(prm)
            hosts.add(urlsplit(issuer).hostname or "")
        async with self._client(hosts) as client:
            metadata = await self._as_metadata(client, issuer)
        issuer_host = (urlsplit(issuer).hostname or "").lower()
        methods = _strings(metadata.get("code_challenge_methods_supported"))
        if "S256" not in methods:
            raise OAuthProtocolError("the server does not support PKCE S256", unsupported=True)
        for key, expected in (
            ("response_types_supported", "code"),
            ("grant_types_supported", "authorization_code"),
        ):
            listed = _strings(metadata.get(key))
            if listed and expected not in listed:
                raise OAuthProtocolError(
                    f"the server does not support {expected}", unsupported=True
                )
        revocation = metadata.get("revocation_endpoint")
        registration = metadata.get("registration_endpoint")
        scopes = scope or " ".join(_strings(prm.get("scopes_supported"))) or None
        return ServerAuth(
            resource=resource,
            issuer=issuer,
            authorization_endpoint=_https_on_host(
                metadata.get("authorization_endpoint"), issuer_host, "authorization_endpoint"
            ),
            token_endpoint=_https_on_host(
                metadata.get("token_endpoint"), issuer_host, "token_endpoint"
            ),
            revocation_endpoint=(
                _https_on_host(revocation, issuer_host, "revocation_endpoint")
                if revocation
                else None
            ),
            registration_endpoint=(
                _https_on_host(registration, issuer_host, "registration_endpoint")
                if registration
                else None
            ),
            cimd_supported=metadata.get("client_id_metadata_document_supported") is True,
            auth_methods=_strings(metadata.get("token_endpoint_auth_methods_supported")),
            scopes=scopes,
        )

    async def requires_auth(self, server_url: str, network: Iterable[str]) -> bool:
        """True when an unauthenticated MCP request is refused with 401/403."""
        host = urlsplit(canonical_resource(server_url)).hostname or ""
        async with self._client({*network, host}) as client:
            status, _ = await self._probe(client, server_url)
        return status in (401, 403)

    async def _probe(
        self, client: httpx2.AsyncClient, server_url: str
    ) -> tuple[int, dict[str, str]]:
        """An unauthenticated MCP request: a protected server answers 401 with a challenge."""
        status, headers, _ = await self._send(
            client,
            "POST",
            server_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        if status not in (401, 403):
            return status, {}
        return status, _challenge_params(headers.get("www-authenticate"))

    async def _prm_location(
        self, client: httpx2.AsyncClient, resource: str, challenge: Mapping[str, str]
    ) -> tuple[str, str | None]:
        scope = challenge.get("scope")
        located = challenge.get("resource_metadata")
        if located:
            return located, scope
        parts = urlsplit(resource)
        path = quote(parts.path) if parts.path else ""
        base = f"https://{parts.netloc}/.well-known/oauth-protected-resource"
        candidates = [base + path, base] if path else [base]
        for candidate in candidates:
            status, _, _ = await self._send(client, "GET", candidate)
            if status == 200:
                return candidate, scope
        raise OAuthProtocolError(
            "the server does not publish protected resource metadata", unsupported=True
        )

    async def _protected_resource(
        self, client: httpx2.AsyncClient, url: str, resource: str
    ) -> dict[str, Any]:
        status, _, body = await self._send(client, "GET", url)
        prm = self._json(status, body, "protected resource metadata")
        stated = prm.get("resource")
        if not isinstance(stated, str) or canonical_resource(stated) != resource:
            raise OAuthProtocolError("protected resource metadata is for another resource")
        return prm

    @staticmethod
    def _issuer(prm: Mapping[str, Any]) -> str:
        for candidate in _strings(prm.get("authorization_servers")):
            parts = urlsplit(candidate)
            if (
                parts.scheme == "https"
                and parts.hostname
                and not parts.query
                and not parts.fragment
            ):
                return candidate.rstrip("/") if parts.path in ("", "/") else candidate
        raise OAuthProtocolError("no usable authorization server is listed", unsupported=True)

    async def _as_metadata(self, client: httpx2.AsyncClient, issuer: str) -> dict[str, Any]:
        parts = urlsplit(issuer)
        origin = f"https://{parts.netloc}"
        path = parts.path.rstrip("/")
        candidates = [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        ]
        for candidate in dict.fromkeys(candidates):
            status, _, body = await self._send(client, "GET", candidate)
            if status != 200:
                continue
            metadata = self._json(status, body, "authorization server metadata")
            if metadata.get("issuer") != issuer and str(metadata.get("issuer", "")).rstrip(
                "/"
            ) != issuer.rstrip("/"):
                raise OAuthProtocolError("authorization server metadata is for another issuer")
            return metadata
        raise OAuthProtocolError("authorization server metadata was not found")

    # ------------------------------------------------------------ registration

    async def register(
        self,
        auth: ServerAuth,
        *,
        redirect_uri: str,
        cimd_url: str | None,
    ) -> Registration:
        if auth.cimd_supported and cimd_url is not None:
            return Registration(client_id=cimd_url, client_secret=None, auth_method="none")
        if auth.registration_endpoint is None:
            raise OAuthProtocolError(
                "the server needs a pre-registered client, which Mia does not have",
                unsupported=True,
            )
        method = (
            "none" if not auth.auth_methods or "none" in auth.auth_methods else auth.auth_methods[0]
        )
        hosts = {urlsplit(auth.registration_endpoint).hostname or ""}
        async with self._client(hosts) as client:
            status, _, body = await self._send(
                client,
                "POST",
                auth.registration_endpoint,
                json={
                    "client_name": CLIENT_NAME,
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": method,
                    "application_type": "web",
                },
                headers={"Accept": "application/json"},
            )
        if status not in (200, 201):
            raise OAuthProtocolError(f"client registration: HTTP {status}")
        data = self._json(200, body, "client registration")
        client_id = data.get("client_id")
        secret = data.get("client_secret")
        granted = data.get("token_endpoint_auth_method")
        if not isinstance(client_id, str) or not client_id:
            raise OAuthProtocolError("client registration returned no client_id")
        return Registration(
            client_id=client_id,
            client_secret=secret if isinstance(secret, str) and secret else None,
            auth_method=granted if isinstance(granted, str) else method,
        )

    # --------------------------------------------------------------- authorize

    @staticmethod
    def authorization_url(
        auth_endpoint: str,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        verifier: str,
        resource: str,
        scope: str | None,
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": pkce.challenge(verifier),
            "code_challenge_method": "S256",
            "resource": resource,
        }
        if scope:
            params["scope"] = scope
        separator = "&" if urlsplit(auth_endpoint).query else "?"
        return f"{auth_endpoint}{separator}{urlencode(params)}"

    # ------------------------------------------------------------------ tokens

    def _client_auth(
        self, client_id: str, secret: str | None, method: str, form: dict[str, str]
    ) -> httpx2.BasicAuth | None:
        if secret is not None and method == "client_secret_basic":
            return httpx2.BasicAuth(client_id, secret)
        form["client_id"] = client_id
        if secret is not None and method == "client_secret_post":
            form["client_secret"] = secret
        return None

    async def _token_request(
        self,
        token_endpoint: str,
        form: dict[str, str],
        *,
        client_id: str,
        secret: str | None,
        method: str,
    ) -> TokenSet:
        auth = self._client_auth(client_id, secret, method, form)
        host = urlsplit(token_endpoint).hostname or ""
        async with self._client({host}) as client:
            status, _, body = await self._send(
                client,
                "POST",
                token_endpoint,
                data=form,
                auth=auth,
                headers={"Accept": "application/json"},
            )
        if status >= 500:
            raise TransientOAuthError(f"token endpoint HTTP {status}")
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            data = {}
        if status in (400, 401):
            if isinstance(data, dict) and data.get("error") == "invalid_grant":
                raise InvalidGrantError()
            detail = data.get("error", status) if isinstance(data, dict) else status
            raise OAuthProtocolError(f"token endpoint rejected the request ({detail})")
        if status != 200 or not isinstance(data, dict):
            raise OAuthProtocolError(f"token endpoint: HTTP {status}")
        access = data.get("access_token")
        token_type = data.get("token_type")
        if not isinstance(access, str) or not access:
            raise OAuthProtocolError("token endpoint returned no access_token")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise OAuthProtocolError("token endpoint returned an unsupported token_type")
        refresh = data.get("refresh_token")
        expires_in = data.get("expires_in")
        scope = data.get("scope")
        return TokenSet(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=(
                self._clock() + float(expires_in)
                if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
                else None
            ),
            scope=scope if isinstance(scope, str) else None,
        )

    async def exchange_code(
        self,
        *,
        token_endpoint: str,
        code: str,
        verifier: str,
        redirect_uri: str,
        resource: str,
        client_id: str,
        client_secret: str | None,
        auth_method: str,
    ) -> TokenSet:
        return await self._token_request(
            token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "resource": resource,
            },
            client_id=client_id,
            secret=client_secret,
            method=auth_method,
        )

    async def refresh(
        self,
        *,
        token_endpoint: str,
        refresh_token: str,
        resource: str,
        scope: str | None,
        client_id: str,
        client_secret: str | None,
        auth_method: str,
    ) -> TokenSet:
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "resource": resource}
        if scope:
            form["scope"] = scope
        return await self._token_request(
            token_endpoint, form, client_id=client_id, secret=client_secret, method=auth_method
        )

    async def revoke(
        self,
        *,
        revocation_endpoint: str,
        tokens: Iterable[tuple[str, str]],
        client_id: str,
        client_secret: str | None,
        auth_method: str,
    ) -> bool:
        """Revoke ``(token, hint)`` pairs (RFC 7009); True when every request was accepted."""
        host = urlsplit(revocation_endpoint).hostname or ""
        accepted = True
        async with self._client({host}) as client:
            for token, hint in tokens:
                form = {"token": token, "token_type_hint": hint}
                auth = self._client_auth(client_id, client_secret, auth_method, form)
                try:
                    status, _, _ = await self._send(
                        client, "POST", revocation_endpoint, data=form, auth=auth
                    )
                except (OAuthProtocolError, TransientOAuthError):
                    accepted = False
                    continue
                accepted = accepted and status == 200
        return accepted
