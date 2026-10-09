"""History retention sweep and incognito run purge (OI-M12-001)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app.modules.conversations.models import (
    Conversation,
    Entry,
    EntryKind,
    EntryRole,
    Modality,
    Turn,
    TurnSource,
    TurnStatus,
)
from app.modules.memory import RetentionService, RetentionSweeper
from app.modules.runs.models import RunEventRow, RunRow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.memory.conftest import MemoryWorld
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


async def seed(
    factory: async_sessionmaker[AsyncSession], user: uuid.UUID, *, age: timedelta
) -> tuple[uuid.UUID, uuid.UUID]:
    """One finished turn (with an entry) and one finished run of the given age."""
    at = NOW - age
    async with factory() as session, session.begin():
        conversation = await session.scalar(
            select(Conversation).where(Conversation.user_id == user)
        )
        if conversation is None:
            conversation = Conversation(
                id=uuid.uuid4(), user_id=user, summary="old summary", summary_until_sequence=1
            )
            session.add(conversation)
            await session.flush()
        sequence = conversation.next_sequence
        conversation.next_sequence = sequence + 1
        turn = Turn(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            source=TurnSource.RUN_CHAT,
            status=TurnStatus.COMPLETED,
            idempotency_key=str(uuid.uuid4()),
            started_at=at,
            completed_at=at,
        )
        session.add(turn)
        await session.flush()
        session.add(
            Entry(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=sequence,
                role=EntryRole.USER,
                kind=EntryKind.TEXT,
                modality=Modality.TEXT,
                content={"text": "привет"},
            )
        )
        run = RunRow(
            id=uuid.uuid4(),
            user_id=user,
            profile="chat",
            status="completed",
            created_at=at,
            updated_at=at,
            finished_at=at,
        )
        session.add(run)
        await session.flush()
        session.add(
            RunEventRow(id=uuid.uuid4(), run_id=run.id, sequence=1, type="run.started",
                        payload={}, at=at)
        )  # fmt: skip
    return turn.id, run.id


async def count(factory: async_sessionmaker[AsyncSession], model: type, user: uuid.UUID) -> int:
    async with factory() as session:
        if model is RunRow:
            statement = select(func.count()).select_from(RunRow).where(RunRow.user_id == user)
        else:
            statement = (
                select(func.count())
                .select_from(Turn)
                .join(Conversation, Conversation.id == Turn.conversation_id)
                .where(Conversation.user_id == user)
            )
        return int(await session.scalar(statement) or 0)


async def test_sweep_follows_each_users_setting(mem: MemoryWorld) -> None:
    keeper, pruner = mem.users
    for user in (keeper, pruner):
        await seed(mem.factory, user, age=timedelta(days=40))
        await seed(mem.factory, user, age=timedelta(days=2))
    await mem.service.update_settings(user_id=str(pruner), history_retention="30d")

    report = await RetentionService(mem.factory, clock=lambda: NOW).sweep()

    assert (report.users, report.turns, report.runs) == (1, 1, 1)
    assert await count(mem.factory, Turn, keeper) == 2
    assert await count(mem.factory, RunRow, keeper) == 2
    assert await count(mem.factory, Turn, pruner) == 1
    assert await count(mem.factory, RunRow, pruner) == 1
    async with mem.factory() as session:
        summary = await session.scalar(
            select(Conversation.summary).where(Conversation.user_id == pruner)
        )
        kept = await session.scalar(
            select(Conversation.summary).where(Conversation.user_id == keeper)
        )
        orphans = await session.scalar(
            select(func.count())
            .select_from(RunEventRow)
            .where(RunEventRow.run_id.not_in(select(RunRow.id)))
        )
    assert summary is None  # it may have described deleted turns
    assert kept == "old summary"
    assert orphans == 0

    await mem.service.update_settings(user_id=str(pruner), history_retention="none")
    await RetentionService(mem.factory, clock=lambda: NOW).sweep()
    assert await count(mem.factory, Turn, pruner) == 0
    assert await count(mem.factory, RunRow, pruner) == 0


async def test_finished_incognito_runs_expire(mem: MemoryWorld) -> None:
    user = mem.user
    async with mem.factory() as session, session.begin():
        for minutes, status in ((90, "completed"), (10, "completed"), (90, "running")):
            at = NOW - timedelta(minutes=minutes)
            session.add(
                RunRow(
                    id=uuid.uuid4(),
                    user_id=user,
                    profile="chat",
                    status=status,
                    incognito=True,
                    created_at=at,
                    updated_at=at,
                    finished_at=at if status == "completed" else None,
                )
            )
    service = RetentionService(mem.factory, incognito_ttl=timedelta(hours=1), clock=lambda: NOW)
    report = await service.sweep()
    assert report.incognito_runs == 1
    assert await count(mem.factory, RunRow, user) == 2


async def test_sweeper_runs_in_background_and_stops(mem: MemoryWorld) -> None:
    calls: list[int] = []

    class Recording(RetentionService):
        async def sweep(self):  # type: ignore[no-untyped-def]
            calls.append(1)
            raise RuntimeError("boom")  # failures are logged, the loop goes on

    sweeper = RetentionSweeper(Recording(mem.factory), interval_s=0.01)
    sweeper.start()
    await asyncio.sleep(0.08)
    await sweeper.stop()
    assert len(calls) >= 2
    disabled = RetentionSweeper(RetentionService(mem.factory), interval_s=0)
    disabled.start()
    await disabled.stop()
