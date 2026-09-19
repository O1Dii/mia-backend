"""S30 end-to-end journeys against the real composed application.

Unlike the module-level suites, every request here goes through ``create_app`` with
``install_runtime_api``: the real auth router, the real bearer authenticator, the real
conversation router and the real PostgreSQL/Redis runtime. Only two boundaries are
substituted, both through seams that already exist in production code:

* Google signature verification, through the S12 ``GoogleIdTokenVerifier(verify_token=...)``
  hook, so no real Google traffic is needed while every claim check still runs.
* The text provider, through the composition's ``fake`` branch, replaced by a counting
  subclass so a replay can prove no second provider call happened.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
import redis.asyncio as redis_asyncio
from alembic import command
from alembic.config import Config
from app import composition as app_composition
from app.core.config import Settings
from app.main import create_app
from app.modules.auth import composition as auth_composition
from app.modules.auth.google import GoogleIdTokenVerifier
from app.modules.auth.tokens import AccessPrincipal, TokenManager
from app.providers.ai.base import AIEvent, TextRequest
from app.providers.ai.fake import FakeAIProvider, FakeTextResponse
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("MIA_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None or TEST_REDIS_URL is None,
    reason="MIA_TEST_DATABASE_URL and MIA_TEST_REDIS_URL are required for E2E journeys",
)

GOOGLE_CLIENT_ID = "mia-e2e-web-client.apps.googleusercontent.test"
AUTH_SECRET = "mia-e2e-auth-signing-secret-at-least-32-bytes"
TEXT_CHUNKS = ("Привет, ", "мир!")
ASSISTANT_TEXT = "".join(TEXT_CHUNKS)


def _offline_google_claims(id_token: str, request: Any, audience: str) -> Mapping[str, Any]:
    """Replace only Google's signature fetch; the real verifier still checks every claim.

    The fake token is ``"<google-sub>|<nonce>"``, mirroring a real ID token that carries the
    client nonce, so audience, issuer, expiry and nonce binding are all still exercised.
    """
    del request, audience
    subject, separator, nonce = id_token.partition("|")
    if not separator:
        raise ValueError("offline Google token must carry a nonce")
    issued_at = datetime.now(UTC)
    return {
        "aud": GOOGLE_CLIENT_ID,
        "iss": "https://accounts.google.com",
        "exp": int((issued_at + timedelta(minutes=5)).timestamp()),
        "iat": int(issued_at.timestamp()),
        "nonce": nonce,
        "sub": subject,
        "email": f"{subject}@example.test",
        "email_verified": True,
        "name": subject.replace("-", " ").title(),
        "picture": "https://example.test/avatar.png",
        "locale": "ru",
    }


class CountingFakeProvider(FakeAIProvider):
    """Fake text provider that records how often the composed app actually called it."""

    def __init__(self) -> None:
        super().__init__(FakeTextResponse(text_chunks=TEXT_CHUNKS))
        self.calls = 0

    async def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]:
        self.calls += 1
        async for event in super().stream(request):
            yield event


@dataclass(frozen=True, slots=True)
class Harness:
    """Settings for the composed application plus the provider it is built with."""

    settings: Settings
    provider: CountingFakeProvider

    @asynccontextmanager
    async def instance(self) -> AsyncIterator[httpx.AsyncClient]:
        """Serve one full application instance; entering it twice emulates a restart."""
        application: FastAPI = create_app(self.settings)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://e2e"
            ) as client:
                yield client
        finally:
            await application.state.runtime.close()

    def _tokens(self) -> TokenManager:
        return TokenManager(
            AUTH_SECRET, access_ttl_seconds=self.settings.auth_access_token_ttl_seconds
        )

    def principal(self, access_token: str) -> AccessPrincipal:
        return self._tokens().verify_access(access_token)

    def expired_access_token(self, access_token: str) -> str:
        """Re-issue the same principal with an already-elapsed lifetime."""
        principal = self.principal(access_token)
        elapsed = timedelta(seconds=self.settings.auth_access_token_ttl_seconds + 60)
        token, _ = self._tokens().issue_access(
            principal.user_id, principal.session_id, now=datetime.now(UTC) - elapsed
        )
        return token


@pytest.fixture(scope="session")
def schema_at_head() -> None:
    if TEST_DATABASE_URL is None:
        return
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(configuration, "head")


@pytest.fixture
async def harness(schema_at_head: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    """Reset shared PostgreSQL/Redis state and pin the credential-free provider seams."""
    del schema_at_head
    assert TEST_DATABASE_URL is not None
    assert TEST_REDIS_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
    await engine.dispose()
    cache = redis_asyncio.from_url(TEST_REDIS_URL)  # type: ignore[no-untyped-call]
    await cache.flushdb()
    await cache.aclose()

    provider = CountingFakeProvider()
    monkeypatch.setattr(
        auth_composition,
        "GoogleIdTokenVerifier",
        lambda: GoogleIdTokenVerifier(verify_token=_offline_google_claims),
    )
    monkeypatch.setattr(app_composition, "FakeAIProvider", lambda: provider)
    yield Harness(
        settings=Settings(
            app_env="test",
            database_url=TEST_DATABASE_URL,
            redis_url=TEST_REDIS_URL,
            google_client_id=GOOGLE_CLIENT_ID,
            auth_jwt_secret=SecretStr(AUTH_SECRET),
        ),
        provider=provider,
    )


def _bearer(access_token: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def _sign_in(client: httpx.AsyncClient, google_sub: str) -> dict[str, Any]:
    """Run the full nonce plus Google ID-token exchange and return the MIA session."""
    nonce = await client.post(
        "/v1/auth/google/nonce", headers={"Idempotency-Key": f"nonce-{google_sub}-0001"}
    )
    assert nonce.status_code == 201, nonce.text
    value = cast(str, nonce.json()["nonce"])
    session = await client.post(
        "/v1/auth/google",
        headers={"Idempotency-Key": f"login-{google_sub}-0001"},
        json={
            "id_token": f"{google_sub}|{value}",
            "nonce": value,
            "device": {"platform": "android", "app_version": "1.0.0"},
        },
    )
    assert session.status_code == 200, session.text
    return cast(dict[str, Any], session.json())


async def _create_conversation(
    client: httpx.AsyncClient, access_token: str, *, title: str, key: str
) -> UUID:
    created = await client.post(
        "/v1/conversations", headers=_bearer(access_token, key), json={"title": title}
    )
    assert created.status_code == 201, created.text
    return UUID(created.json()["id"])


async def _stream_text(
    client: httpx.AsyncClient,
    access_token: str,
    conversation_id: UUID,
    *,
    message: str,
    key: str,
) -> httpx.Response:
    return await client.post(
        f"/v1/conversations/{conversation_id}/messages:stream",
        headers=_bearer(access_token, key),
        json={"text": message},
    )


def _sse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append({"name": fields["event"], "data": json.loads(fields["data"])})
    return events


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], cast(dict[str, Any], event["data"])["payload"])


async def test_sign_in_create_conversation_and_stream_text(harness: Harness) -> None:
    """Google exchange, authenticated create and text SSE work on one composed instance."""
    async with harness.instance() as client:
        session = await _sign_in(client, "e2e-primary-account")
        access_token = cast(str, session["access_token"])
        assert session["token_type"] == "Bearer"
        assert session["user"]["email"] == "e2e-primary-account@example.test"

        identity = await client.get("/v1/me", headers=_bearer(access_token))
        assert identity.status_code == 200
        assert identity.json()["id"] == session["user"]["id"]

        conversation_id = await _create_conversation(
            client, access_token, title="E2E", key="e2e-create-conversation-0001"
        )

        streamed = await _stream_text(
            client,
            access_token,
            conversation_id,
            message="Привет",
            key="e2e-stream-message-0001",
        )
        assert streamed.status_code == 200, streamed.text
        assert streamed.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(streamed.text)
        assert [event["name"] for event in events] == [
            "message.delta",
            "message.delta",
            "message.completed",
        ]
        assert [_payload(event)["text"] for event in events[:2]] == list(TEXT_CHUNKS)
        assert _payload(events[-1])["finish_reason"] == "stop"
        assert _payload(events[-1])["replayed"] is False
        assert harness.provider.calls == 1


async def test_history_survives_a_fresh_application_instance(harness: Harness) -> None:
    """Canonical entries persist across a process-restart equivalent."""
    async with harness.instance() as client:
        session = await _sign_in(client, "e2e-restart-account")
        access_token = cast(str, session["access_token"])
        conversation_id = await _create_conversation(
            client, access_token, title="Restart", key="e2e-restart-conversation-01"
        )
        streamed = await _stream_text(
            client,
            access_token,
            conversation_id,
            message="Что нового?",
            key="e2e-restart-message-0001",
        )
        assert streamed.status_code == 200, streamed.text

    async with harness.instance() as restarted:
        listed = await restarted.get("/v1/conversations", headers=_bearer(access_token))
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["items"]] == [str(conversation_id)]

        entries = await restarted.get(
            f"/v1/conversations/{conversation_id}/entries", headers=_bearer(access_token)
        )
        assert entries.status_code == 200, entries.text
        items = entries.json()["items"]
        assert [(item["role"], item["sequence"]) for item in items] == [
            ("user", 1),
            ("assistant", 2),
        ]
        assert items[0]["content"] == {"text": "Что нового?"}
        assert items[1]["content"] == {"text": ASSISTANT_TEXT}
        assert all(item["delivery_status"] == "delivered" for item in items)


async def test_duplicate_stream_retry_replays_one_completion(harness: Harness) -> None:
    """A retried turn returns a single replayed completion without a second provider call."""
    async with harness.instance() as client:
        session = await _sign_in(client, "e2e-retry-account")
        access_token = cast(str, session["access_token"])
        conversation_id = await _create_conversation(
            client, access_token, title="Retry", key="e2e-retry-conversation-0001"
        )
        first = await _stream_text(
            client, access_token, conversation_id, message="Повтори", key="e2e-retry-message-0001"
        )
        assert first.status_code == 200, first.text
        assert harness.provider.calls == 1

        retry = await _stream_text(
            client, access_token, conversation_id, message="Повтори", key="e2e-retry-message-0001"
        )
        assert retry.status_code == 200, retry.text
        replayed = _sse_events(retry.text)
        assert [event["name"] for event in replayed] == ["message.completed"]
        assert _payload(replayed[0])["replayed"] is True
        assert harness.provider.calls == 1

        entries = await client.get(
            f"/v1/conversations/{conversation_id}/entries", headers=_bearer(access_token)
        )
        assert [item["sequence"] for item in entries.json()["items"]] == [1, 2]


async def test_expired_access_token_refreshes_and_rotates(harness: Harness) -> None:
    """Expiry fails closed, refresh restores access, and the spent refresh token is rejected."""
    async with harness.instance() as client:
        session = await _sign_in(client, "e2e-refresh-account")
        access_token = cast(str, session["access_token"])
        conversation_id = await _create_conversation(
            client, access_token, title="Refresh", key="e2e-refresh-conversation-01"
        )

        expired = harness.expired_access_token(access_token)
        denied = await client.get(f"/v1/conversations/{conversation_id}", headers=_bearer(expired))
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "authentication_required"

        refreshed = await client.post(
            "/v1/auth/refresh",
            headers={"Idempotency-Key": "e2e-refresh-request-0001"},
            json={"refresh_token": session["refresh_token"]},
        )
        assert refreshed.status_code == 200, refreshed.text
        rotated_access = cast(str, refreshed.json()["access_token"])
        assert refreshed.json()["refresh_token"] != session["refresh_token"]
        assert harness.principal(rotated_access).user_id == harness.principal(access_token).user_id

        allowed = await client.get(
            f"/v1/conversations/{conversation_id}", headers=_bearer(rotated_access)
        )
        assert allowed.status_code == 200, allowed.text
        streamed = await _stream_text(
            client,
            rotated_access,
            conversation_id,
            message="После обновления",
            key="e2e-refresh-message-0001",
        )
        assert streamed.status_code == 200, streamed.text

        reused = await client.post(
            "/v1/auth/refresh",
            headers={"Idempotency-Key": "e2e-refresh-request-0002"},
            json={"refresh_token": session["refresh_token"]},
        )
        assert reused.status_code == 401
        revoked = await client.get("/v1/conversations", headers=_bearer(rotated_access))
        assert revoked.status_code == 401


async def test_logout_revokes_the_family_for_every_protected_route(harness: Harness) -> None:
    """Logout fails closed across auth and conversation routes, even for unexpired access."""
    async with harness.instance() as client:
        session = await _sign_in(client, "e2e-logout-account")
        access_token = cast(str, session["access_token"])
        conversation_id = await _create_conversation(
            client, access_token, title="Logout", key="e2e-logout-conversation-001"
        )

        logout = await client.post(
            "/v1/auth/logout",
            headers=_bearer(access_token, "e2e-logout-request-0001"),
            json={"refresh_token": session["refresh_token"]},
        )
        assert logout.status_code == 204, logout.text

        assert (await client.get("/v1/me", headers=_bearer(access_token))).status_code == 401
        assert (
            await client.get("/v1/conversations", headers=_bearer(access_token))
        ).status_code == 401
        assert (
            await client.get(f"/v1/conversations/{conversation_id}", headers=_bearer(access_token))
        ).status_code == 401
        streamed = await _stream_text(
            client,
            access_token,
            conversation_id,
            message="После выхода",
            key="e2e-logout-message-0001",
        )
        assert streamed.status_code == 401
        assert harness.provider.calls == 0

        refused = await client.post(
            "/v1/auth/refresh",
            headers={"Idempotency-Key": "e2e-logout-refresh-0001"},
            json={"refresh_token": session["refresh_token"]},
        )
        assert refused.status_code == 401


async def test_second_account_cannot_reach_another_accounts_conversation(harness: Harness) -> None:
    """Two real Google identities stay isolated across read, stream and history."""
    async with harness.instance() as client:
        owner = await _sign_in(client, "e2e-owner-account")
        owner_token = cast(str, owner["access_token"])
        conversation_id = await _create_conversation(
            client, owner_token, title="Private", key="e2e-owner-conversation-0001"
        )
        streamed = await _stream_text(
            client, owner_token, conversation_id, message="Секрет", key="e2e-owner-message-0001"
        )
        assert streamed.status_code == 200, streamed.text

        stranger = await _sign_in(client, "e2e-stranger-account")
        stranger_token = cast(str, stranger["access_token"])
        assert stranger["user"]["id"] != owner["user"]["id"]

        assert (
            await client.get(
                f"/v1/conversations/{conversation_id}", headers=_bearer(stranger_token)
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/v1/conversations/{conversation_id}/entries", headers=_bearer(stranger_token)
            )
        ).status_code == 404
        hijacked = await _stream_text(
            client,
            stranger_token,
            conversation_id,
            message="Покажи",
            key="e2e-stranger-message-001",
        )
        assert hijacked.status_code == 404
        assert harness.provider.calls == 1
        assert (await client.get("/v1/conversations", headers=_bearer(stranger_token))).json()[
            "items"
        ] == []
