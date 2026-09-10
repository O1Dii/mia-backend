from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from app.modules.conversations import (
    ConversationRepository,
    ConversationService,
    DeliveryStatus,
    EntryKind,
    EntryRole,
    IdempotencyConflictError,
    Modality,
    TurnSource,
)
from app.modules.conversations.models import Entry
from app.modules.conversations.repositories import ConversationNotFoundError
from app.modules.users import UserRepository
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


def database_url() -> str:
    return cast(str, TEST_DATABASE_URL)


def alembic_config() -> Config:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url())
    return configuration


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    await asyncio.to_thread(command.upgrade, alembic_config(), "head")
    engine = create_async_engine(database_url(), pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_round_trip_up_down_up() -> None:
    configuration = alembic_config()
    engine = create_async_engine(database_url())

    await asyncio.to_thread(command.downgrade, configuration, "base")
    await asyncio.to_thread(command.upgrade, configuration, "head")
    assert {
        "users",
        "conversations",
        "turns",
        "entries",
        "idempotency_keys",
    } <= await table_names(engine)

    await asyncio.to_thread(command.downgrade, configuration, "base")
    assert not ({"users", "conversations", "turns", "entries"} & await table_names(engine))

    await asyncio.to_thread(command.upgrade, configuration, "head")
    assert "entries" in await table_names(engine)
    await engine.dispose()


async def create_user(factory: async_sessionmaker[AsyncSession], *, subject: str) -> UUID:
    async with factory() as session, session.begin():
        user = await UserRepository(session).create(
            google_sub=subject,
            email=f"{subject}@example.test",
            email_verified=True,
            locale="ru-BY",
            timezone="Europe/Minsk",
        )
        return user.id


@pytest.mark.asyncio
async def test_idempotent_conversation_create_replays_and_conflicts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_user(session_factory, subject="idempotency-user")
    service = ConversationService(session_factory)
    key = "create-conversation-0001"

    created = await service.create_conversation(user_id=user_id, idempotency_key=key, title="First")
    replayed = await service.create_conversation(
        user_id=user_id, idempotency_key=key, title="First"
    )

    assert created.replayed is False
    assert replayed.replayed is True
    assert replayed.value.id == created.value.id
    with pytest.raises(IdempotencyConflictError, match="different request"):
        await service.create_conversation(user_id=user_id, idempotency_key=key, title="Changed")


@pytest.mark.asyncio
async def test_ownership_filters_turns_entries_and_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner_id = await create_user(session_factory, subject="owner")
    stranger_id = await create_user(session_factory, subject="stranger")
    service = ConversationService(session_factory)
    created = await service.create_conversation(
        user_id=owner_id,
        idempotency_key="create-conversation-owner",
        title="Owned",
    )
    turn = await service.create_turn(
        user_id=owner_id,
        conversation_id=created.value.id,
        idempotency_key="create-turn-owner-0001",
        source=TurnSource.ANDROID_TEXT,
    )

    async with session_factory() as session, session.begin():
        repository = ConversationRepository(session)
        assert (
            await repository.get_owned(user_id=stranger_id, conversation_id=created.value.id)
            is None
        )
        with pytest.raises(ConversationNotFoundError):
            await repository.create_turn(
                user_id=stranger_id,
                conversation_id=created.value.id,
                idempotency_key="stranger-turn-0001",
                source=TurnSource.ANDROID_TEXT,
            )
        entry = await repository.append_entry(
            user_id=owner_id,
            conversation_id=created.value.id,
            turn_id=turn.value.id,
            role=EntryRole.USER,
            kind=EntryKind.TEXT,
            modality=Modality.TEXT,
            content={"text": "Hello"},
            delivery_status=DeliveryStatus.DELIVERED,
        )
        assert entry.sequence == 1
        foreign_page = await repository.list_entries(
            user_id=stranger_id, conversation_id=created.value.id
        )
        assert foreign_page.items == ()


@pytest.mark.asyncio
async def test_row_lock_allocates_monotonic_sequences_concurrently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await create_user(session_factory, subject="sequence-owner")
    service = ConversationService(session_factory)
    conversation = await service.create_conversation(
        user_id=user_id,
        idempotency_key="create-conversation-sequence",
    )
    turn = await service.create_turn(
        user_id=user_id,
        conversation_id=conversation.value.id,
        idempotency_key="create-turn-sequence-001",
        source=TurnSource.ANDROID_VOICE,
    )

    async def append(text_value: str) -> int:
        async with session_factory() as session, session.begin():
            entry = await ConversationRepository(session).append_entry(
                user_id=user_id,
                conversation_id=conversation.value.id,
                turn_id=turn.value.id,
                role=EntryRole.ASSISTANT,
                kind=EntryKind.TRANSCRIPT,
                modality=Modality.AUDIO,
                content={"text": text_value},
            )
            return entry.sequence

    sequences = await asyncio.gather(append("one"), append("two"))

    assert sorted(sequences) == [1, 2]
    async with session_factory() as session:
        rows = tuple((await session.scalars(select(Entry).order_by(Entry.sequence))).all())
        assert [row.sequence for row in rows] == [1, 2]
