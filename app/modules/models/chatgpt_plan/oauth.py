"""Sign in with ChatGPT: OAuth Authorization Code + PKCE for a loopback public client.

Contract: ``spikes/m01/DECISION.md`` «Контракт для M11». Tokens live only in memory
and in the encrypted connection secret; they are excluded from ``repr`` and never
logged. The loopback listener is multi-threaded with socket timeouts (the M01 spike
hit a real hang with a single-threaded server and idle browser connections).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
REVOKE_URL = f"{ISSUER}/api/accounts/oauth/revoke"
RESOURCE = "https://api.openai.com/v1"
DYNAMIC_CLIENT = "dynamic_agent_client"
AGENT_NAME = "MIA"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
DEFAULT_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REFRESH_SKEW_SECONDS = 300

_PERMANENT_REFRESH_ERRORS = frozenset(
    {"invalid_grant", "refresh_token_reused", "refresh_token_expired", "invalid_client"}
)


class TokenEndpointError(Exception):
    """Token endpoint failure. ``permanent`` means the refresh token is dead (re-login)."""

    def __init__(self, code: str, *, permanent: bool, status: int = 0) -> None:
        super().__init__(f"token endpoint error: {code}")
        self.code = code
        self.permanent = permanent
        self.status = status


@dataclass(slots=True)
class TokenSet:
    """Plan credentials. Serialized only into the sealed connection secret."""

    client_id: str
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    expires_at: float = 0.0
    scopes: list[str] = field(default_factory=list)
    email_hint: str | None = field(default=None, repr=False)

    def fresh(self, now: float, skew: float = REFRESH_SKEW_SECONDS) -> bool:
        return bool(self.access_token) and self.expires_at - skew > now

    @property
    def signed_in(self) -> bool:
        return bool(self.refresh_token)

    def dumps(self) -> bytes:
        return json.dumps(
            {
                "client_id": self.client_id,
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "scopes": self.scopes,
            },
            separators=(",", ":"),
        ).encode()

    @classmethod
    def loads(cls, raw: bytes) -> TokenSet:
        data = json.loads(raw)
        return cls(
            client_id=str(data["client_id"]),
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=float(data.get("expires_at", 0.0)),
            scopes=[str(s) for s in data.get("scopes", [])],
        )

    def signed_out(self) -> TokenSet:
        """Keep the issued ``client_id`` (re-login reuses it); drop every token."""
        return TokenSet(client_id=self.client_id)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_authorize_url(
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    challenge: str,
    host_id: str,
    client_id: str | None = None,
) -> str:
    """First login registers dynamically; a saved ``oaiapp_`` id is reused without a name hint."""
    params: dict[str, str] = {"client_id": client_id or DYNAMIC_CLIENT}
    if not client_id:
        params["agent_name_hint"] = AGENT_NAME
    params.update(
        {
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


async def _token_post(
    client: httpx.AsyncClient, form: dict[str, str], clock: Any = time.time
) -> tuple[dict[str, Any], float]:
    try:
        response = await client.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        raise TokenEndpointError("network_error", permanent=False) from None
    if response.status_code != 200:
        code = "token_endpoint_error"
        try:
            payload = response.json()
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, str):
                code = err
            elif isinstance(err, dict) and isinstance(err.get("code"), str):
                code = err["code"]
        except ValueError:
            pass
        if len(code) > 64 or not code.replace("_", "").replace("-", "").isalnum():
            code = "token_endpoint_error"
        permanent = code in _PERMANENT_REFRESH_ERRORS or response.status_code in {400, 401}
        raise TokenEndpointError(code, permanent=permanent, status=response.status_code)
    try:
        payload = response.json()
    except ValueError:
        raise TokenEndpointError("bad_response", permanent=False) from None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise TokenEndpointError("bad_response", permanent=False)
    return payload, clock()


async def exchange_code(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    clock: Any = time.time,
) -> tuple[TokenSet, str]:
    """Return ``(tokens, id_token)``; the caller verifies the ID token and the scope."""
    payload, now = await _token_post(
        client,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        },
        clock,
    )
    tokens = TokenSet(
        client_id=client_id,
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token", "")),
        expires_at=now + float(payload.get("expires_in") or 0),
        scopes=str(payload.get("scope", "")).split(),
    )
    return tokens, str(payload.get("id_token", ""))


async def refresh_tokens(
    client: httpx.AsyncClient, tokens: TokenSet, clock: Any = time.time
) -> TokenSet:
    """Refresh with rotation; the new refresh token replaces the old one atomically."""
    payload, now = await _token_post(
        client,
        {
            "grant_type": "refresh_token",
            "client_id": tokens.client_id,
            "refresh_token": tokens.refresh_token,
            "resource": RESOURCE,
        },
        clock,
    )
    return TokenSet(
        client_id=tokens.client_id,
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or tokens.refresh_token),
        expires_at=now + float(payload.get("expires_in") or 0),
        scopes=str(payload.get("scope", "")).split() or tokens.scopes,
    )


async def revoke_refresh_token(client: httpx.AsyncClient, tokens: TokenSet) -> bool:
    if not tokens.refresh_token:
        return True
    try:
        response = await client.post(
            REVOKE_URL,
            data={
                "token": tokens.refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": tokens.client_id,
            },
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


class CallbackServer:
    """Loopback redirect listener on 127.0.0.1 (never ``localhost``), asyncio-friendly."""

    def __init__(self, preferred_port: int = DEFAULT_PORT) -> None:
        self.params: dict[str, str] = {}
        self._loop = asyncio.get_running_loop()
        self._done = asyncio.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 5  # idle/preconnect sockets must not pin a handler thread

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                ok = parts.path == CALLBACK_PATH and not outer.params
                if ok:
                    outer.params = {k: v[0] for k, v in parse_qs(parts.query).items()}
                self.send_response(200 if ok else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(
                    b"<h3>MIA: sign-in complete. You can close this tab.</h3>" if ok else b""
                )
                if ok:
                    outer._loop.call_soon_threadsafe(outer._done.set)

            def log_message(self, *_args: Any) -> None:  # request lines contain the code
                return

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", preferred_port), Handler)
        except OSError:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port: int = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._stopped = False

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def start(self) -> None:
        self._thread.start()

    async def wait(self, timeout_seconds: float) -> dict[str, str]:
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._done.wait()
        except TimeoutError:
            raise TimeoutError("no OAuth callback received") from None
        return self.params

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # shutdown() waits for serve_forever to leave; never block the event loop on it.
        closer = threading.Thread(target=self._httpd.shutdown, daemon=True)
        closer.start()
        await asyncio.to_thread(closer.join, 2.0)
        self._httpd.server_close()
