"""Security and admission controls shared across backend modules."""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.core.circuit import GuardedTextProvider, ProviderCircuit
from app.core.limits import DailyBudget
from app.core.logging import JsonFormatter
from app.core.redis import RedisClient
from app.core.runtime import Runtime
from app.modules.realtime.contracts import INPUT_AUDIO_FORMAT, OUTPUT_AUDIO_FORMAT
from app.modules.realtime.sessions import InMemoryRealtimeSessionStore, RealtimeSessionLimitError
from app.providers.ai.base import ProviderError, TextMessage, TextRequest
from fastapi.testclient import TestClient


def test_json_logging_never_serializes_content_exception_or_untrusted_extra() -> None:
    secret = "synthetic-secret-and-transcript"
    record = logging.LogRecord("mia", logging.ERROR, __file__, 1, secret, (), None)
    record.exc_text = secret
    record.request_id = secret
    record.path = f"/v1/conversations?token={secret}"
    record.duration_ms = 12
    payload = json.loads(JsonFormatter().format(record))
    assert secret not in json.dumps(payload)
    assert payload["event"] == "redacted_event"
    assert payload["duration_ms"] == 12
    assert "path" not in payload
    record.msg = "synthetic_secret_and_transcript"
    assert json.loads(JsonFormatter().format(record))["event"] == "redacted_event"


def test_untrusted_request_id_is_regenerated_and_not_logged(client: TestClient) -> None:
    supplied = "synthetic-secret\ntranscript"
    response = client.get("/health/live", headers={"X-Request-ID": supplied})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != supplied
    assert len(response.headers["X-Request-ID"]) == 32


class Counter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.requests: set[str] = set()

    async def reserve_daily_budget(
        self, counter_key: str, request_key: str, *, ttl_seconds: int, limit: int
    ) -> tuple[bool, int]:
        if request_key in self.requests:
            return True, ttl_seconds
        if self.counts.get(counter_key, 0) >= limit:
            return False, ttl_seconds
        self.counts[counter_key] = self.counts.get(counter_key, 0) + 1
        self.requests.add(request_key)
        return True, ttl_seconds


@pytest.mark.asyncio
async def test_daily_budget_is_user_scoped_and_denies_after_limit() -> None:
    budget = DailyBudget(Counter())
    user_a, user_b = uuid4(), uuid4()
    assert (await budget.spend("text_turns", user_a, limit=1, idempotency_key="first"))[0]
    assert (await budget.spend("text_turns", user_a, limit=1, idempotency_key="first"))[0]
    allowed, retry_after = await budget.spend(
        "text_turns", user_a, limit=1, idempotency_key="second"
    )
    assert not allowed and retry_after > 0
    assert (await budget.spend("text_turns", user_b, limit=1, idempotency_key="first"))[0]


@pytest.mark.asyncio
async def test_redis_budget_reservation_is_atomic_and_idempotent() -> None:
    url = os.environ.get("MIA_TEST_REDIS_URL")
    if url is None:
        pytest.skip("MIA_TEST_REDIS_URL is required for the Redis budget test")
    redis = RedisClient(url)
    try:
        budget = DailyBudget(redis)
        user_id = uuid4()
        first = await asyncio.gather(
            *(
                budget.spend("text_turns", user_id, limit=1, idempotency_key=key)
                for key in ("first", "second")
            )
        )
        assert sum(allowed for allowed, _ in first) == 1
        winning_key = "first" if first[0][0] else "second"
        assert (await budget.spend("text_turns", user_id, limit=1, idempotency_key=winning_key))[0]
    finally:
        await redis.close()


@pytest.mark.asyncio
async def test_single_active_realtime_session_releases_after_finish() -> None:
    store = InMemoryRealtimeSessionStore(max_active_per_user=1)
    user_id, conversation_id = uuid4(), uuid4()

    async def create(key: str):  # type: ignore[no-untyped-def]
        return await store.create(
            user_id=user_id,
            conversation_id=conversation_id,
            locale="en-US",
            input_audio=INPUT_AUDIO_FORMAT,
            output_audio=OUTPUT_AUDIO_FORMAT,
            idempotency_key=key,
        )

    first, _ = await create("first-realtime-key")
    with pytest.raises(RealtimeSessionLimitError):
        await create("second-realtime-key")
    replay, replayed = await create("first-realtime-key")
    assert replayed and replay.id == first.id
    await store.finish(first.id, interrupted=False)
    second, _ = await create("second-realtime-key")
    assert second.id != first.id


class FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request: TextRequest):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise RuntimeError("synthetic-secret-and-transcript")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_provider_circuit_opens_and_recovers_with_single_probe() -> None:
    now = [0.0]
    circuit = ProviderCircuit(2, 10, clock=lambda: now[0])
    provider = FailingProvider()
    guarded = GuardedTextProvider(provider, circuit)
    request = TextRequest(
        turn_id=UUID("40000000-0000-4000-8000-000000000001"),
        response_id=uuid4(),
        messages=[TextMessage(role="user", text="test")],
        system_instruction="test",
    )
    for _ in range(3):
        events = [event async for event in guarded.stream(request)]
        assert len(events) == 1 and isinstance(events[0], ProviderError)
        assert "synthetic-secret" not in events[0].message
    assert provider.calls == 2
    now[0] = 11
    events = [event async for event in guarded.stream(request)]
    assert len(events) == 1 and provider.calls == 3


class SlowDependency:
    async def ping(self) -> None:
        await asyncio.sleep(3)


@pytest.mark.asyncio
async def test_readiness_times_out_on_stalled_dependency() -> None:
    runtime = Runtime(database=SlowDependency(), redis=SlowDependency())  # type: ignore[arg-type]
    started = datetime.now(UTC)
    assert await runtime.is_ready() is False
    assert (datetime.now(UTC) - started).total_seconds() < 2.5
