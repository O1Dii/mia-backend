"""A fake auth.openai.com: token endpoint with refresh rotation, JWKS and revoke."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs

import httpx

from tests.models.jwt_tools import RsaSigner

ISSUER = "https://auth.openai.com"
FULL_SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
CLIENT_ID = "oaiapp_test1"


class AuthWorld:
    def __init__(self) -> None:
        self.signer = RsaSigner("kid-1")
        self.nonce = ""
        self.refresh_calls = 0
        self.code_calls = 0
        self.revoked: list[str] = []
        self.current_refresh = ""
        self.counter = 0
        self.delay = 0.0
        self.refresh_failure: tuple[int, dict[str, Any]] | None = None
        self.scope = FULL_SCOPE
        self.id_token_signer: RsaSigner | None = None
        self.transport = httpx.MockTransport(self._handle)
        self.client = httpx.AsyncClient(transport=self.transport)

    def _issue(self) -> dict[str, Any]:
        self.counter += 1
        self.current_refresh = f"refresh-{self.counter}"
        return {
            "access_token": f"access-{self.counter}",
            "refresh_token": self.current_refresh,
            "expires_in": 3600,
            "scope": self.scope,
            "token_type": "Bearer",
        }

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/jwks.json"):
            return httpx.Response(200, json={"keys": [self.signer.jwk()]})
        if path.endswith("/oauth/revoke"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.revoked.append(form["token"])
            return httpx.Response(200)
        assert path.endswith("/oauth/token"), path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if self.delay:
            await asyncio.sleep(self.delay)
        if form["grant_type"] == "authorization_code":
            self.code_calls += 1
            assert form["code_verifier"] and form["resource"] == "https://api.openai.com/v1"
            payload = self._issue()
            signer = self.id_token_signer or self.signer
            payload["id_token"] = signer.sign(
                {
                    "iss": ISSUER,
                    "aud": form["client_id"],
                    "nonce": self.nonce,
                    "exp": 9_999_999_999,
                    "sub": "chatgpt-user",
                }
            )
            return httpx.Response(200, json=payload)
        self.refresh_calls += 1
        if self.refresh_failure is not None:
            status, body = self.refresh_failure
            return httpx.Response(status, json=body)
        assert "scope" not in form
        if form["refresh_token"] != self.current_refresh:
            return httpx.Response(400, json={"error": "refresh_token_reused"})
        return httpx.Response(200, json=self._issue())


def dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)
