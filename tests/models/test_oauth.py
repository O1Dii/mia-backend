from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app.modules.models.chatgpt_plan import oauth

HOST_ID = "urn:uuid:11111111-1111-1111-1111-111111111111"


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_pkce_challenge_is_s256_of_the_verifier() -> None:
    verifier, challenge = oauth.pkce_pair()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert len(verifier) >= 43


def test_first_login_registers_dynamically_and_relogin_reuses_issued_client() -> None:
    first = query(
        oauth.build_authorize_url(
            redirect_uri="http://127.0.0.1:1455/auth/callback",
            state="s",
            nonce="n",
            challenge="c",
            host_id=HOST_ID,
        )
    )
    assert first["client_id"] == "dynamic_agent_client" and first["agent_name_hint"] == "MIA"
    assert first["ext_agent_host_id"] == HOST_ID
    assert first["resource"] == "https://api.openai.com/v1"
    assert first["scope"] == oauth.SCOPES and first["code_challenge_method"] == "S256"
    assert first["redirect_uri"].startswith("http://127.0.0.1:")

    again = query(
        oauth.build_authorize_url(
            redirect_uri="http://127.0.0.1:1455/auth/callback",
            state="s",
            nonce="n",
            challenge="c",
            host_id=HOST_ID,
            client_id="oaiapp_saved",
        )
    )
    assert again["client_id"] == "oaiapp_saved" and "agent_name_hint" not in again


def test_token_set_round_trip_hides_secrets_and_signed_out_keeps_client() -> None:
    tokens = oauth.TokenSet("oaiapp_x", "ACCESS-SECRET-1", "REFRESH-SECRET-1", 123.0, ["a"])
    assert "SECRET" not in repr(tokens)
    assert oauth.TokenSet.loads(tokens.dumps()) == tokens
    out = tokens.signed_out()
    assert out.client_id == "oaiapp_x" and not out.signed_in and not out.access_token


def test_fresh_uses_a_refresh_skew() -> None:
    tokens = oauth.TokenSet("c", "a", "r", expires_at=1000.0)
    assert tokens.fresh(now=600.0) and not tokens.fresh(now=800.0)


def _token_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_refresh_rotates_and_sends_the_documented_form() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200,
            json={
                "access_token": "A2",
                "refresh_token": "R2",
                "expires_in": 3600,
                "scope": oauth.SCOPES,
            },
        )

    old = oauth.TokenSet("oaiapp_x", "A1", "R1", 0.0, [])
    new = await oauth.refresh_tokens(_token_client(handler), old, clock=lambda: 1000.0)
    assert (new.access_token, new.refresh_token, new.expires_at) == ("A2", "R2", 4600.0)
    assert seen == [
        {
            "grant_type": "refresh_token",
            "client_id": "oaiapp_x",
            "refresh_token": "R1",
            "resource": "https://api.openai.com/v1",
        }
    ]  # no scope on refresh


@pytest.mark.parametrize(
    ("status", "body", "permanent"),
    [
        (400, {"error": "invalid_grant"}, True),
        (400, {"error": "refresh_token_reused"}, True),
        (401, {"error": {"code": "invalid_client"}}, True),
        (503, {"error": "temporarily_unavailable"}, False),
        (500, {}, False),
    ],
)
async def test_refresh_failures_are_classified(
    status: int, body: dict[str, Any], permanent: bool
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    with pytest.raises(oauth.TokenEndpointError) as caught:
        await oauth.refresh_tokens(_token_client(handler), oauth.TokenSet("c", "a", "r", 0.0))
    assert caught.value.permanent is permanent


async def test_network_failure_is_transient() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(oauth.TokenEndpointError) as caught:
        await oauth.refresh_tokens(_token_client(handler), oauth.TokenSet("c", "a", "r", 0.0))
    assert not caught.value.permanent


async def test_callback_server_survives_idle_connections_and_ignores_other_paths() -> None:
    """Regression of the M01 hang: a browser pre-connect must not block the real callback."""
    server = oauth.CallbackServer(0)
    server.start()
    idle = socket.create_connection(("127.0.0.1", server.port))  # connects, sends nothing
    try:
        async with httpx.AsyncClient() as http:
            assert (
                await http.get(f"http://127.0.0.1:{server.port}/favicon.ico")
            ).status_code == 404
            ok = await http.get(f"{server.redirect_uri}?code=c1&state=s1&client_id=oaiapp_z")
            assert ok.status_code == 200
            # a second delivery cannot overwrite the first
            again = await http.get(f"{server.redirect_uri}?code=EVIL&state=s1")
            assert again.status_code == 404
        params = await server.wait(2)
        assert params == {"code": "c1", "state": "s1", "client_id": "oaiapp_z"}
    finally:
        idle.close()
        await asyncio.wait_for(server.stop(), 5)


async def test_callback_server_times_out() -> None:
    server = oauth.CallbackServer(0)
    server.start()
    try:
        with pytest.raises(TimeoutError):
            await server.wait(0.05)
    finally:
        await asyncio.wait_for(server.stop(), 5)
