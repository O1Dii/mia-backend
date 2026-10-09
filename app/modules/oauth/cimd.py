"""Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document).

``client_id`` is an https URL; Mia fetches it behind the tool router's SSRF guard
(public addresses only, the connection pinned to the vetted IP), requires the
document's ``client_id`` to equal the URL exactly and accepts public clients only
(``token_endpoint_auth_method: none``). Documents are cached in process.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx2

from app.modules.oauth.models import CLIENT_ID_MAX
from app.modules.tools.netguard import (
    GuardedTransport,
    HostBlockedError,
    HostPolicy,
    Resolver,
    system_resolver,
)

MAX_DOCUMENT_BYTES: Final = 16 * 1024
MAX_REDIRECT_URIS: Final = 10
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
_CACHE_MAX_AGE = re.compile(r"max-age=(\d+)")
_PRIVATE_SCHEME = re.compile(r"^[a-z][a-z0-9+-]*(\.[a-z0-9+-]+)+$")
_FORBIDDEN_SCHEMES: Final = frozenset({"javascript", "data", "file", "vbscript", "about", "blob"})


class ClientMetadataError(Exception):
    """The client_id or its document is not acceptable; ``reason`` is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ClientMetadata:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    client_uri: str | None = None
    logo_uri: str | None = None


def _is_loopback(host: str | None) -> bool:
    return (host or "").lower() in LOOPBACK_HOSTS


def check_client_id(client_id: str, *, allow_loopback: bool = False) -> None:
    """Shape rules for a CIMD URL (before any network access)."""
    if not client_id or len(client_id) > CLIENT_ID_MAX:
        raise ClientMetadataError("client_id_length")
    try:
        parts = urlsplit(client_id)
        port = parts.port
    except ValueError as error:
        raise ClientMetadataError("client_id_malformed") from error
    loopback_dev = allow_loopback and parts.scheme == "http" and _is_loopback(parts.hostname)
    if parts.scheme != "https" and not loopback_dev:
        raise ClientMetadataError("client_id_scheme")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ClientMetadataError("client_id_host")
    if parts.fragment or "#" in client_id:
        raise ClientMetadataError("client_id_fragment")
    if parts.query:
        raise ClientMetadataError("client_id_query")
    segments = parts.path.split("/")
    if not parts.path or parts.path == "/" or any(s in (".", "..") for s in segments):
        raise ClientMetadataError("client_id_path")
    if port is not None and not 0 < port < 65536:
        raise ClientMetadataError("client_id_port")


def check_redirect_uri(uri: str) -> None:
    """https, loopback http (RFC 8252 §7.3) or a reverse-DNS private-use scheme."""
    if not isinstance(uri, str) or not uri or len(uri) > 2048:
        raise ClientMetadataError("redirect_uri_invalid")
    try:
        parts = urlsplit(uri)
    except ValueError as error:
        raise ClientMetadataError("redirect_uri_invalid") from error
    if parts.fragment or "#" in uri:
        raise ClientMetadataError("redirect_uri_fragment")
    scheme = parts.scheme.lower()
    if scheme in _FORBIDDEN_SCHEMES:
        raise ClientMetadataError("redirect_uri_scheme")
    if scheme == "https":
        if not parts.hostname or parts.username is not None:
            raise ClientMetadataError("redirect_uri_host")
        return
    if scheme == "http":
        if not _is_loopback(parts.hostname) or parts.username is not None:
            raise ClientMetadataError("redirect_uri_http_not_loopback")
        return
    if not _PRIVATE_SCHEME.match(scheme):
        raise ClientMetadataError("redirect_uri_scheme")


def _optional_https(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parts = urlsplit(value)
    return value if parts.scheme == "https" and parts.hostname else None


def parse_document(client_id: str, body: bytes) -> ClientMetadata:
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClientMetadataError("document_not_json") from error
    if not isinstance(data, dict):
        raise ClientMetadataError("document_not_object")
    document_id = data.get("client_id")
    if not isinstance(document_id, str) or document_id != client_id:
        raise ClientMetadataError("document_client_id_mismatch")
    for forbidden in ("client_secret", "client_secret_expires_at"):
        if forbidden in data:
            raise ClientMetadataError("document_has_secret")
    method = data.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise ClientMetadataError("document_auth_method")
    grant_types = data.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        raise ClientMetadataError("document_grant_types")
    response_types = data.get("response_types", ["code"])
    if not isinstance(response_types, list) or "code" not in response_types:
        raise ClientMetadataError("document_response_types")
    redirect_uris = data.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or len(redirect_uris) > MAX_REDIRECT_URIS
    ):
        raise ClientMetadataError("document_redirect_uris")
    for uri in redirect_uris:
        check_redirect_uri(uri)
    name = data.get("client_name")
    if not isinstance(name, str) or not name.strip():
        name = urlsplit(client_id).hostname or client_id
    return ClientMetadata(
        client_id=client_id,
        client_name=name.strip()[:200],
        redirect_uris=tuple(str(uri) for uri in redirect_uris),
        client_uri=_optional_https(data.get("client_uri")),
        logo_uri=_optional_https(data.get("logo_uri")),
    )


TransportFactory = Callable[[], httpx2.AsyncBaseTransport]


class ClientMetadataResolver:
    def __init__(
        self,
        *,
        timeout_s: float = 5.0,
        cache_s: int = 3600,
        allow_loopback: bool = False,
        resolver: Resolver = system_resolver,
        http_transport: TransportFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout_s = timeout_s
        self._cache_s = cache_s
        self._allow_loopback = allow_loopback
        self._resolver = resolver
        self._http_transport = http_transport
        self._clock = clock
        self._cache: dict[str, tuple[float, ClientMetadata]] = {}

    def _client(self, client_id: str) -> httpx2.AsyncClient:
        parts = urlsplit(client_id)
        inner = self._http_transport() if self._http_transport else None
        transport: httpx2.AsyncBaseTransport
        if self._allow_loopback and parts.scheme == "http" and _is_loopback(parts.hostname):
            transport = inner or httpx2.AsyncHTTPTransport(retries=0)
        else:
            transport = GuardedTransport(
                HostPolicy([parts.hostname or ""]), resolver=self._resolver, inner=inner
            )
        return httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(self._timeout_s),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "mia-oauth/1", "Accept": "application/json"},
        )

    async def resolve(self, client_id: str) -> ClientMetadata:
        check_client_id(client_id, allow_loopback=self._allow_loopback)
        now = self._clock()
        cached = self._cache.get(client_id)
        if cached is not None and cached[0] > now:
            return cached[1]
        status, headers, body = await self._fetch(client_id)
        if status != 200:
            raise ClientMetadataError("document_status")
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if (
            content_type
            and content_type != "application/json"
            and not content_type.endswith("+json")
        ):
            raise ClientMetadataError("document_content_type")
        metadata = parse_document(client_id, body)
        self._cache[client_id] = (now + self._max_age(headers.get("cache-control")), metadata)
        if len(self._cache) > 1024:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        return metadata

    def _max_age(self, header: str | None) -> float:
        if header:
            lowered = header.lower()
            if "no-store" in lowered or "no-cache" in lowered:
                return 60.0
            match = _CACHE_MAX_AGE.search(lowered)
            if match:
                return float(min(max(int(match.group(1)), 60), self._cache_s))
        return float(self._cache_s)

    async def _fetch(self, client_id: str) -> tuple[int, Any, bytes]:
        try:
            async with self._client(client_id) as client:
                async with client.stream("GET", client_id) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_DOCUMENT_BYTES:
                            raise ClientMetadataError("document_too_large")
                    return response.status_code, response.headers, bytes(body)
        except HostBlockedError as error:
            raise ClientMetadataError("client_id_host_blocked") from error
        except httpx2.HTTPError as error:
            if isinstance(error.__cause__, HostBlockedError):
                raise ClientMetadataError("client_id_host_blocked") from error
            raise ClientMetadataError("document_unreachable") from error
