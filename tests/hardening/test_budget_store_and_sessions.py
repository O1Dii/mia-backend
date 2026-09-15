"""Redis-backed budget reservations and realtime session slot accounting."""

import asyncio
import os
from uuid import uuid4

import pytest
from app.core.limits import DailyBudget
from app.core.redis import RedisClient
from app.modules.realtime.contracts import INPUT_AUDIO_FORMAT, OUTPUT_AUDIO_FORMAT
from app.modules.realtime.sessions import (
    InMemoryRealtimeSessionStore,
    RealtimeSession,
    RealtimeSessionLimitError,
)


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
        replay_allowed, retry_after = await budget.spend(
            "text_turns", user_id, limit=1, idempotency_key=winning_key
        )
        assert replay_allowed and retry_after > 0
        denied, _ = await budget.spend("text_turns", user_id, limit=1, idempotency_key="third")
        assert not denied
    finally:
        await redis.close()


@pytest.mark.asyncio
async def test_single_active_realtime_session_releases_after_finish() -> None:
    store = InMemoryRealtimeSessionStore(max_active_per_user=1)
    user_id, conversation_id = uuid4(), uuid4()

    async def create(key: str) -> tuple[RealtimeSession, bool]:
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
