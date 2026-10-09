"""History retention (Review §7.3, ADR-0012 item 6; closes OI-M12-001 for MVP).

Per user setting ``history_retention``: ``30d`` | ``1y`` | ``forever`` | ``none``.
A sweep deletes, for every user whose setting is not ``forever``:

- finished feed turns older than the cutoff (entries cascade) and resets the
  feed summary of the touched conversations, since it may describe them;
- finished runs older than the cutoff (``run_events`` cascade) and their
  approval rows (exact tool arguments).

``none`` uses the cutoff «now»: history is removed on the next sweep, not
prevented from being written (OI-M17-002). Independently of the setting,
finished **incognito** runs (whose events hold the answer text) are deleted
``incognito_ttl`` after they end. The memory ledger itself is not affected.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.conversations.models import Conversation, Turn, TurnStatus
from app.modules.memory.domain import HistoryRetention
from app.modules.memory.models import MemorySettings
from app.modules.runs.domain import TERMINAL_STATUSES
from app.modules.runs.models import RunRow
from app.modules.tools.models import ToolApproval

logger = logging.getLogger(__name__)

RETENTION_WINDOWS: Final[dict[HistoryRetention, timedelta | None]] = {
    "30d": timedelta(days=30),
    "1y": timedelta(days=365),
    "none": timedelta(0),
    "forever": None,
}
_FINISHED_TURNS = (TurnStatus.COMPLETED, TurnStatus.INTERRUPTED, TurnStatus.FAILED)


@dataclass(frozen=True, slots=True)
class SweepReport:
    users: int = 0
    turns: int = 0
    runs: int = 0
    incognito_runs: int = 0


class RetentionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        incognito_ttl: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._incognito_ttl = incognito_ttl
        self._clock = clock

    async def sweep(self) -> SweepReport:
        now = self._clock()
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(MemorySettings.user_id, MemorySettings.history_retention).where(
                        MemorySettings.history_retention != "forever"
                    )
                )
            ).all()
        turns = runs = 0
        for user_id, retention in rows:
            window = RETENTION_WINDOWS.get(retention)
            if window is None:
                continue
            purged_turns, purged_runs = await self.purge_user(user_id, now - window)
            turns += purged_turns
            runs += purged_runs
        incognito = await self.purge_incognito(now - self._incognito_ttl)
        report = SweepReport(users=len(rows), turns=turns, runs=runs, incognito_runs=incognito)
        logger.info(
            "memory_retention_sweep",
            extra={"users": report.users, "turns": turns, "runs": runs, "incognito": incognito},
        )
        return report

    async def purge_user(self, user_id: uuid.UUID, cutoff: datetime) -> tuple[int, int]:
        async with self._sessions() as session, session.begin():
            conversations = select(Conversation.id).where(Conversation.user_id == user_id)
            removed = (
                await session.execute(
                    delete(Turn)
                    .where(
                        Turn.conversation_id.in_(conversations),
                        Turn.started_at < cutoff,
                        Turn.status.in_(_FINISHED_TURNS),
                    )
                    .returning(Turn.conversation_id)
                )
            ).all()
            touched = {row[0] for row in removed}
            if touched:
                await session.execute(
                    update(Conversation)
                    .where(Conversation.id.in_(touched))
                    .values(summary=None, summary_until_sequence=None)
                )
            runs = await self._delete_runs(
                session,
                RunRow.user_id == user_id,
                RunRow.created_at < cutoff,
            )
        return len(removed), runs

    async def purge_incognito(self, finished_before: datetime) -> int:
        async with self._sessions() as session, session.begin():
            return await self._delete_runs(
                session,
                RunRow.incognito.is_(True),
                RunRow.finished_at.is_not(None),
                RunRow.finished_at < finished_before,
            )

    async def _delete_runs(self, session: AsyncSession, *conditions: object) -> int:
        ids = list(
            (
                await session.scalars(
                    select(RunRow.id).where(
                        RunRow.status.in_(sorted(TERMINAL_STATUSES)),
                        *conditions,  # type: ignore[arg-type]
                    )
                )
            ).all()
        )
        if not ids:
            return 0
        await session.execute(delete(ToolApproval).where(ToolApproval.run_id.in_(ids)))
        await session.execute(delete(RunRow).where(RunRow.id.in_(ids)))
        return len(ids)


class RetentionSweeper:
    """Background loop in the app lifespan; first sweep after one interval."""

    def __init__(self, service: RetentionService, interval_s: float) -> None:
        self._service = service
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._interval > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._service.sweep()
            except Exception:
                logger.exception("memory_retention_sweep_failed")
