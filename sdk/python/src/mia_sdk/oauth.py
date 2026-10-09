"""«Войти через Мию» for Python apps: authorization code + PKCE S256 (public client, CIMD).

oauth = MiaOAuth(issuer="https://mia.example", client_id="https://app.example/client.json",
                 redirect_uri="https://app.example/callback")
start = oauth.start(scope="openid llm.generate memory.context")
# redirect the browser to start.url; keep start.state and start.verifier in the session
tokens = await oauth.finish(callback_url, state=start.state, verifier=start.verifier)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx


class MiaOAuthError(Exception):
    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description


def pkce_pair() -> tuple[str, str]:
    """``(verifier, challenge)`` for S256: 64 random bytes, base64url without padding."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


@dataclass(frozen=True, slots=True)
class SignInStart:
    url: str
    state: str
    verifier: str
    nonce: str | None = None


@dataclass(frozen=True, slots=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    id_token: str | None = None

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> Tokens:
        return cls(
            access_token=str(body["access_token"]),
            refresh_token=str(body["refresh_token"]),
            expires_in=int(body["expires_in"]),
            scope=str(body.get("scope", "")),
            id_token=body.get("id_token"),
        )


class MiaOAuth:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        redirect_uri: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self._http = http

    def _client(self) -> httpx.AsyncClient:
        return self._http or httpx.AsyncClient(timeout=15.0)

    def start(self, *, scope: str, resource: str | None = None, nonce: bool = True) -> SignInStart:
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        nonce_value = secrets.token_urlsafe(16) if nonce and "openid" in scope.split() else None
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if resource:
            params["resource"] = resource
        if nonce_value:
            params["nonce"] = nonce_value
        url = f"{self.issuer}/oauth/authorize?{urlencode(params)}"
        return SignInStart(url=url, state=state, verifier=verifier, nonce=nonce_value)

    async def finish(self, callback_url: str, *, state: str, verifier: str) -> Tokens:
        """Validate ``state`` and ``iss`` (RFC 9207) of the callback, then redeem the code."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(callback_url).query).items()}
        if not hmac.compare_digest(query.get("state", ""), state):
            raise MiaOAuthError("invalid_state", "The state does not match this sign-in.")
        if query.get("iss") not in (None, self.issuer):
            raise MiaOAuthError("invalid_issuer", "The response came from another issuer.")
        if "error" in query:
            raise MiaOAuthError(query["error"], query.get("error_description", ""))
        code = query.get("code")
        if not code:
            raise MiaOAuthError("invalid_request", "The callback has no code.")
        return await self.exchange_code(code, verifier)

    async def _token(self, data: dict[str, str]) -> Tokens:
        client = self._client()
        try:
            response = await client.post(f"{self.issuer}/oauth/token", data=data)
        finally:
            if self._http is None:
                await client.aclose()
        body = response.json()
        if response.status_code != 200:
            raise MiaOAuthError(str(body.get("error")), str(body.get("error_description", "")))
        return Tokens.from_body(body)

    async def exchange_code(self, code: str, verifier: str) -> Tokens:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": self.redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str, *, scope: str | None = None) -> Tokens:
        """Rotate: the old refresh token stops working; store the new one at once."""
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": refresh_token,
        }
        if scope:
            data["scope"] = scope
        return await self._token(data)

    async def revoke(self, token: str) -> None:
        client = self._client()
        try:
            await client.post(
                f"{self.issuer}/oauth/revoke", data={"token": token, "client_id": self.client_id}
            )
        finally:
            if self._http is None:
                await client.aclose()

    async def userinfo(self, access_token: str) -> dict[str, Any]:
        client = self._client()
        try:
            response = await client.get(
                f"{self.issuer}/oauth/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        finally:
            if self._http is None:
                await client.aclose()
        if response.status_code != 200:
            raise MiaOAuthError("invalid_token", "The access token was rejected.")
        return dict(response.json())
