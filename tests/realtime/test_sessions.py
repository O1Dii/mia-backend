from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from app.modules.realtime.contracts import INPUT_AUDIO_FORMAT, OUTPUT_AUDIO_FORMAT
from app.modules.realtime.sessions import (
    InMemoryRealtimeSessionStore,
    RealtimeSessionConflictError,
    RealtimeSessionNotFoundError,
)

USER_ID = UUID("90000000-0000-4000-8000-000000000001")
OTHER_USER_ID = UUID("90000000-0000-4000-8000-000000000002")
CONVERSATION_ID = UUID("20000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)


@pytest.mark.asyncio
async def test_session_creation_is_idempotent_and_owner_bound() -> None:
    store = InMemoryRealtimeSessionStore()

    created, replayed = await store.create(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        locale="ru-BY",
        input_audio=INPUT_AUDIO_FORMAT,
        output_audio=OUTPUT_AUDIO_FORMAT,
        idempotency_key="realtime-session-key-001",
        now=NOW,
    )
    same, was_replayed = await store.create(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        locale="ru-BY",
        input_audio=INPUT_AUDIO_FORMAT,
        output_audio=OUTPUT_AUDIO_FORMAT,
        idempotency_key="realtime-session-key-001",
        now=NOW,
    )

    assert replayed is False
    assert was_replayed is True
    assert same.id == created.id
    assert (await store.claim(created.id, USER_ID, now=NOW)).id == created.id
    with pytest.raises(RealtimeSessionNotFoundError):
        await store.claim(created.id, OTHER_USER_ID, now=NOW)


@pytest.mark.asyncio
async def test_idempotency_conflict_and_expiry_are_normalized() -> None:
    store = InMemoryRealtimeSessionStore(ttl=timedelta(seconds=10))
    session, _ = await store.create(
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        locale="ru-BY",
        input_audio=INPUT_AUDIO_FORMAT,
        output_audio=OUTPUT_AUDIO_FORMAT,
        idempotency_key="realtime-session-key-002",
        now=NOW,
    )
    with pytest.raises(RealtimeSessionConflictError):
        await store.create(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            locale="en-US",
            input_audio=INPUT_AUDIO_FORMAT,
            output_audio=OUTPUT_AUDIO_FORMAT,
            idempotency_key="realtime-session-key-002",
            now=NOW,
        )
    with pytest.raises(RealtimeSessionNotFoundError):
        await store.claim(session.id, USER_ID, now=NOW + timedelta(seconds=11))
