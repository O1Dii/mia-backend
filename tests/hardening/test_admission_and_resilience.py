"""Quotas, kill switches, circuit breakers and readiness failure modes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import httpx2
import pytest
from app.core.circuit import (
    GuardedRealtimeSession,
    GuardedTextProvider,
    ProviderCircuit,
    ProviderCircuitOpenError,
)
from app.core.limits import BudgetUnavailableError, DailyBudget
from app.core.runtime import Runtime
from app.modules.conversations.repositories import ConversationNotFoundError
from app.modules.conversations.router import build_conversation_router
from app.modules.realtime.contracts import INPUT_AUDIO_FORMAT, OUTPUT_AUDIO_FORMAT
from app.modules.realtime.gateway import RealtimeGateway
from app.modules.realtime.router import build_realtime_router
from app.modules.realtime.sessions import (
    AuthenticatedUser,
    InMemoryRealtimeSessionStore,
    RealtimeSession,
    RealtimeSessionStatus,
)
from app.providers.ai.base import ProviderError, TextMessage, TextRequest
from fastapi import FastAPI
from fastapi.testclient import TestClient

USER_ID = UUID("90000000-0000-4000-8000-000000000001")
CONVERSATION_ID = UUID("20000000-0000-4000-8000-000000000001")
TOKEN = "valid-test-token"
SECRET = "synthetic-secret-and-transcript"


class StaticAuthenticator:
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None:
        return AuthenticatedUser(USER_ID) if bearer_token == TOKEN else None


class CountingStore:
    """In-memory stand-in for the Redis budget script."""

    def __init__(self, *, fail: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.requests: set[str] = set()
        self.fail = fail

    async def reserve_daily_budget(
        self, counter_key: str, request_key: str, *, ttl_seconds: int, limit: int
    ) -> tuple[bool, int]:
        if self.fail:
            raise ConnectionError("redis is unreachable")
        if request_key in self.requests:
            return True, ttl_seconds
        if self.counts.get(counter_key, 0) >= limit:
            return False, ttl_seconds
        self.counts[counter_key] = self.counts.get(counter_key, 0) + 1
        self.requests.add(request_key)
        return True, ttl_seconds


class MissingConversationStreamService:
    """Reached only when admission control allowed the turn."""

    def __init__(self) -> None:
        self.calls = 0

    async def prepare(self, **kwargs: Any) -> None:
        self.calls += 1
        raise ConversationNotFoundError("conversation was not found")


def build_text_client(
    *, budget: DailyBudget | None, limit: int = 1, text_enabled: bool = True
) -> tuple[httpx.AsyncClient, MissingConversationStreamService]:
    stream_service = MissingConversationStreamService()
    app = FastAPI()
    app.include_router(
        build_conversation_router(
            session_factory=None,  # type: ignore[arg-type]
            authenticator=StaticAuthenticator(),
            stream_service=stream_service,  # type: ignore[arg-type]
            budget=budget,
            max_daily_turns=limit,
            text_enabled=text_enabled,
        )
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, stream_service


async def send_turn(client: httpx.AsyncClient, key: str) -> httpx.Response:
    return await client.post(
        f"/v1/conversations/{CONVERSATION_ID}/messages:stream",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": key},
        json={"text": "привет"},
    )


@pytest.mark.asyncio
async def test_daily_text_budget_denies_after_limit_and_replays_are_free() -> None:
    client, stream_service = build_text_client(budget=DailyBudget(CountingStore()), limit=1)
    async with client:
        first = await send_turn(client, "text-turn-key-00000001")
        replay = await send_turn(client, "text-turn-key-00000001")
        denied = await send_turn(client, "text-turn-key-00000002")
    assert first.status_code == 404 and replay.status_code == 404
    assert stream_service.calls == 2
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "rate_limit_exceeded"
    assert denied.json()["error"]["retryable"] is True
    assert int(denied.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_text_admission_fails_closed_when_the_counter_store_is_down() -> None:
    client, stream_service = build_text_client(budget=DailyBudget(CountingStore(fail=True)))
    async with client:
        response = await send_turn(client, "text-turn-key-00000003")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert response.json()["error"]["retryable"] is True
    assert stream_service.calls == 0


@pytest.mark.asyncio
async def test_text_kill_switch_blocks_turns_before_the_provider() -> None:
    client, stream_service = build_text_client(budget=None, text_enabled=False)
    async with client:
        response = await send_turn(client, "text-turn-key-00000004")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert stream_service.calls == 0


@pytest.mark.asyncio
async def test_daily_budget_is_scoped_per_user_and_utc_day() -> None:
    store = CountingStore()
    budget = DailyBudget(store)
    other_user = uuid4()
    assert (await budget.spend("text_turns", USER_ID, limit=1, idempotency_key="a"))[0]
    assert not (await budget.spend("text_turns", USER_ID, limit=1, idempotency_key="b"))[0]
    assert (await budget.spend("text_turns", other_user, limit=1, idempotency_key="a"))[0]
    assert (await budget.spend("live_sessions", USER_ID, limit=1, idempotency_key="a"))[0]
    assert all(str(USER_ID) not in key or "budget:" in key for key in store.counts)
    with pytest.raises(BudgetUnavailableError):
        await DailyBudget(CountingStore(fail=True)).spend(
            "text_turns", USER_ID, limit=1, idempotency_key="a"
        )


def build_realtime_client(
    *,
    budget: DailyBudget | None = None,
    max_daily_sessions: int = 20,
    max_active: int | None = None,
    voice_enabled: bool = True,
    provider_available: Any = None,
) -> tuple[TestClient, InMemoryRealtimeSessionStore]:
    store = InMemoryRealtimeSessionStore(max_active_per_user=max_active)
    authenticator = StaticAuthenticator()

    def provider_factory(session: RealtimeSession) -> Any:
        raise AssertionError("no realtime provider is created by session admission")

    gateway = RealtimeGateway(
        store=store, authenticator=authenticator, provider_factory=provider_factory
    )
    app = FastAPI()
    app.include_router(
        build_realtime_router(
            store=store,
            authenticator=authenticator,
            gateway=gateway,
            budget=budget,
            max_daily_sessions=max_daily_sessions,
            voice_enabled=voice_enabled,
            provider_available=provider_available,
        )
    )
    return TestClient(app), store


def create_session(client: TestClient, key: str) -> httpx2.Response:
    response = client.post(
        "/v1/realtime/sessions",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": key},
        json={
            "conversation_id": str(CONVERSATION_ID),
            "locale": "ru-BY",
            "input_audio": INPUT_AUDIO_FORMAT.model_dump(mode="json"),
            "output_audio": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
        },
    )
    assert isinstance(response, httpx2.Response)
    return response


def test_denied_live_session_is_released_and_reports_the_daily_reason() -> None:
    client, store = build_realtime_client(
        budget=DailyBudget(CountingStore()), max_daily_sessions=1, max_active=1
    )
    first = create_session(client, "realtime-session-key-001")
    assert first.status_code == 201
    client.delete(
        f"/v1/realtime/sessions/{first.json()['id']}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    denied = create_session(client, "realtime-session-key-002")
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "rate_limit_exceeded"
    assert denied.json()["error"]["message"] == "Daily voice limit reached."
    assert int(denied.headers["Retry-After"]) > 0

    # The released session must not keep the single active-session slot.
    again = create_session(client, "realtime-session-key-003")
    assert again.status_code == 429
    assert again.json()["error"]["message"] == "Daily voice limit reached."
    statuses = {session.status for session in asyncio.run(_sessions(store))}
    assert RealtimeSessionStatus.CREATED not in statuses


async def _sessions(store: InMemoryRealtimeSessionStore) -> list[RealtimeSession]:
    ids = [session.id for session in list(store._sessions.values())]  # noqa: SLF001
    resolved = [await store.get(session_id) for session_id in ids]
    return [session for session in resolved if session is not None]


def test_active_live_session_limit_is_enforced_per_user() -> None:
    client, _ = build_realtime_client(max_active=1)
    assert create_session(client, "realtime-session-key-004").status_code == 201
    second = create_session(client, "realtime-session-key-005")
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_exceeded"
    assert second.json()["error"]["message"] == "Active voice limit reached."


def test_voice_kill_switch_and_open_circuit_reject_new_sessions() -> None:
    disabled, _ = build_realtime_client(voice_enabled=False)
    response = disabled.post(
        "/v1/realtime/sessions",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "realtime-key-0000006"},
        json={
            "conversation_id": str(CONVERSATION_ID),
            "locale": "ru-BY",
            "input_audio": INPUT_AUDIO_FORMAT.model_dump(mode="json"),
            "output_audio": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
        },
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"

    tripped, _ = build_realtime_client(provider_available=lambda: False)
    unavailable = create_session(tripped, "realtime-session-key-007")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "realtime_provider_unavailable"
    assert unavailable.json()["error"]["retryable"] is True


class FailingTextProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request: TextRequest) -> AsyncIterator[Any]:
        self.calls += 1
        raise RuntimeError(SECRET)
        yield  # pragma: no cover


def text_request() -> TextRequest:
    return TextRequest(
        turn_id=UUID("40000000-0000-4000-8000-000000000001"),
        response_id=uuid4(),
        messages=[TextMessage(role="user", text="привет")],
        system_instruction="test",
    )


@pytest.mark.asyncio
async def test_text_circuit_opens_probes_once_and_hides_provider_details() -> None:
    now = [0.0]
    circuit = ProviderCircuit(2, 10, name="gemini_text", clock=lambda: now[0])
    provider = FailingTextProvider()
    guarded = GuardedTextProvider(provider, circuit)

    for _ in range(3):
        events = [event async for event in guarded.stream(text_request())]
        assert len(events) == 1
        error = events[0]
        assert isinstance(error, ProviderError)
        assert error.code == "provider_unavailable" and error.retryable is True
        assert SECRET not in error.message
    assert provider.calls == 2 and circuit.state == "open" and not circuit.available()

    now[0] = 11
    assert circuit.state == "half_open" and circuit.available()
    events = [event async for event in guarded.stream(text_request())]
    assert provider.calls == 3 and len(events) == 1
    assert circuit.state == "open"


class FakeRealtimeProviderSession:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.closed = False

    async def configure(self, config: Any) -> None:
        if self.fail:
            raise RuntimeError(SECRET)

    async def send_audio(self, chunk: bytes) -> None:
        return None

    async def commit_audio(self, *, stream_id: UUID, turn_id: UUID) -> None:
        return None

    async def cancel_response(self, response_id: UUID, reason: str) -> None:
        return None

    async def events(self) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_realtime_circuit_opens_on_connect_failures_and_recovers() -> None:
    now = [0.0]
    circuit = ProviderCircuit(2, 30, name="gemini_live", clock=lambda: now[0])
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await GuardedRealtimeSession(FakeRealtimeProviderSession(fail=True), circuit).configure(
                None
            )
    assert circuit.state == "open" and not circuit.available()
    with pytest.raises(ProviderCircuitOpenError):
        await GuardedRealtimeSession(FakeRealtimeProviderSession(fail=False), circuit).configure(
            None
        )

    now[0] = 31
    healthy = FakeRealtimeProviderSession(fail=False)
    await GuardedRealtimeSession(healthy, circuit).configure(None)
    assert circuit.state == "closed" and circuit.available()


class StalledDependency:
    async def ping(self) -> None:
        await asyncio.sleep(5)

    async def schema_revision(self) -> str | None:
        await asyncio.sleep(5)
        return None


class BrokenDependency:
    async def ping(self) -> None:
        raise ConnectionError("dependency is unreachable")

    async def schema_revision(self) -> str | None:
        raise ConnectionError("dependency is unreachable")


class HealthyDependency:
    def __init__(self, revision: str | None = "20260913_0003") -> None:
        self.revision = revision

    async def ping(self) -> None:
        return None

    async def schema_revision(self) -> str | None:
        return self.revision


def runtime(database: Any, redis: Any, revision: str | None = "20260913_0003") -> Runtime:
    return Runtime(
        database=database,
        redis=redis,
        expected_schema_revision=revision,
        readiness_timeout_seconds=0.5,
    )


@pytest.mark.asyncio
async def test_readiness_reports_database_redis_schema_and_timeout_failures() -> None:
    assert await runtime(HealthyDependency(), HealthyDependency()).is_ready() is True
    assert await runtime(BrokenDependency(), HealthyDependency()).is_ready() is False
    assert await runtime(HealthyDependency(), BrokenDependency()).is_ready() is False
    assert await runtime(HealthyDependency(revision=None), HealthyDependency()).is_ready() is False
    assert (
        await runtime(HealthyDependency("20260911_0002"), HealthyDependency()).is_ready() is False
    )

    started = asyncio.get_running_loop().time()
    assert await runtime(StalledDependency(), StalledDependency()).is_ready() is False
    assert asyncio.get_running_loop().time() - started < 2


def test_live_session_duration_cap_closes_with_a_controlled_error() -> None:
    from app.modules.realtime.fake_provider import FakeRealtimeProvider
    from tests.realtime.test_gateway import USER_ID as GATEWAY_USER_ID
    from tests.realtime.test_gateway import configure
    from tests.realtime.test_gateway import create_session as create_ws_session

    assert GATEWAY_USER_ID == USER_ID
    store = InMemoryRealtimeSessionStore()
    authenticator = StaticAuthenticator()
    gateway = RealtimeGateway(
        store=store,
        authenticator=authenticator,
        provider_factory=FakeRealtimeProvider,
        max_session_seconds=0.3,
    )
    app = FastAPI()
    app.include_router(
        build_realtime_router(store=store, authenticator=authenticator, gateway=gateway)
    )
    client = TestClient(app)
    session_id = create_ws_session(client, key="realtime-duration-key-001")
    with client.websocket_connect(
        f"/v1/realtime?session_id={session_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as websocket:
        websocket.send_json(configure(session_id))
        event = websocket.receive_json()
        assert event["type"] == "session.ready"
        while event["type"] != "error":
            event = websocket.receive_json()
        error = event
        assert error["payload"]["code"] == "rate_limit_exceeded"
        assert error["payload"]["retryable"] is True
        assert SECRET not in error["payload"]["message"]
    session = asyncio.run(store.get(UUID(session_id)))
    assert session is not None and session.status is not RealtimeSessionStatus.CREATED
