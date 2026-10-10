"""Run lifecycle: create, execute in the background, cancel, and stream events.

Runs are detached from HTTP connections: a client that disconnects resumes by
cursor and loses nothing. Live runs are tracked per process; a run whose
heartbeat stopped (process restart) and which no local task owns is ended with
``run.failed``/``run_interrupted`` when someone looks at it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Coroutine, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.modules.conversations.repositories import ConversationNotFoundError
from app.modules.runs.domain import (
    EXTERNAL_EVENT_TYPES,
    CancelReason,
    NewRun,
    RunFinishedError,
    RunNotFoundError,
    RunRecord,
    StoredEvent,
)
from app.modules.runs.engine import CancelSignal, RunEngine
from app.modules.runs.errors import public_error

logger = logging.getLogger(__name__)


class VoiceFeedOnlyError(ValueError):
    """Voice runs always write to the primary feed (ADR-0011 §6)."""


@dataclass(slots=True)
class _LiveRun:
    task: asyncio.Task[None]
    signal: CancelSignal


class RunService:
    def __init__(
        self,
        engine: RunEngine,
        *,
        keepalive_s: float = 15.0,
        orphan_after_s: float = 60.0,
        page_size: int = 200,
    ) -> None:
        self.engine = engine
        self.store = engine.store
        self.notifier = engine.notifier
        self._keepalive_s = keepalive_s
        self._orphan_after = timedelta(seconds=orphan_after_s)
        self._page_size = page_size
        self._live: dict[uuid.UUID, _LiveRun] = {}
        self._background: set[asyncio.Task[Any]] = set()

    async def start(
        self,
        new: NewRun,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[RunRecord, bool]:
        """Create the run and start executing it; a replayed key returns the existing run."""
        feed = self.engine.feed
        if new.conversation_id is not None and feed is not None:
            if not await feed.owns_conversation(
                user_id=new.user_id, conversation_id=new.conversation_id
            ):
                raise ConversationNotFoundError("conversation was not found")
        record, replayed = await self.store.create(
            new, idempotency_key=idempotency_key, request_hash=request_hash
        )
        if not replayed:
            self._spawn(record, new.input_text)
        return record, replayed

    def _spawn(self, record: RunRecord, input_text: str) -> None:
        signal = CancelSignal()
        task = asyncio.create_task(
            self.engine.run(record, input_text, signal, background=self.spawn_background),
            name=f"run:{record.id}",
        )
        self._live[record.id] = _LiveRun(task=task, signal=signal)
        task.add_done_callback(lambda _: self._live.pop(record.id, None))

    def spawn_background(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self._background.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                logger.warning("run_background_task_failed")

        task.add_done_callback(done)

    def is_live(self, run_id: uuid.UUID) -> bool:
        return run_id in self._live

    async def get(self, *, user_id: uuid.UUID, run_id: uuid.UUID) -> RunRecord:
        record = await self.store.get(run_id, user_id=user_id)
        if record is None:
            raise RunNotFoundError("run was not found")
        if await self._reap_if_orphan(record):
            record = await self.store.get(run_id, user_id=user_id) or record
        return record

    async def cancel(
        self, *, user_id: uuid.UUID, run_id: uuid.UUID, reason: CancelReason = "user"
    ) -> RunRecord:
        """Request cancellation; ``run.cancelled`` follows on the stream."""
        record = await self.store.request_cancel(run_id, user_id=user_id, reason=reason)
        if record is None:
            raise RunNotFoundError("run was not found")
        if record.terminal:
            return record
        live = self._live.get(run_id)
        if live is not None:
            live.signal.request(reason)
        elif self._orphaned(record):
            await self._append_quietly(run_id, "run.cancelled", {"reason": reason})
        return record

    async def publish(
        self, *, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
    ) -> StoredEvent:
        """Append an event produced by another module into a live run.

        Allowed types: ``device_tool.call`` (M23), ``memory.suggested`` (M17),
        ``job.created`` (M16). Raises ``RunFinishedError`` after the terminal event.
        """
        if event_type not in EXTERNAL_EVENT_TYPES:
            raise ValueError(f"{event_type} cannot be published by other modules")
        event = await self.store.append(run_id, event_type, payload, at=self.engine.clock())
        self.notifier.notify(run_id)
        return event

    async def events(
        self, *, user_id: uuid.UUID, run_id: uuid.UUID, after_sequence: int = 0
    ) -> AsyncGenerator[StoredEvent | None, None]:
        """Events after ``after_sequence`` until the terminal one; ``None`` = keepalive."""
        await self.get(user_id=user_id, run_id=run_id)
        cursor = after_sequence
        while True:
            waiter = self.notifier.watch(run_id)
            batch = await self.store.events_after(run_id, cursor, limit=self._page_size)
            for event in batch:
                yield event
                cursor = event.sequence
                if event.terminal:
                    return
            if batch:
                continue
            current = await self.store.get(run_id)
            if current is None or (current.terminal and current.last_sequence <= cursor):
                return
            try:
                await asyncio.wait_for(waiter.wait(), timeout=self._keepalive_s)
            except TimeoutError:
                yield None
                record = await self.store.get(run_id)
                if record is not None:
                    await self._reap_if_orphan(record)

    async def wait_finished(self, run_id: uuid.UUID) -> None:
        """Wait for the local task of ``run_id`` (tests, graceful shutdown)."""
        live = self._live.get(run_id)
        if live is not None:
            await asyncio.gather(live.task, return_exceptions=True)

    async def aclose(self) -> None:
        tasks = [live.task for live in self._live.values()] + list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _orphaned(self, record: RunRecord) -> bool:
        if record.terminal or record.id in self._live:
            return False
        seen = record.heartbeat_at or record.updated_at
        return seen < self.engine.clock() - self._orphan_after

    async def _reap_if_orphan(self, record: RunRecord) -> bool:
        if not self._orphaned(record):
            return False
        if record.status == "cancel_requested":
            reason = record.cancel_reason if record.cancel_reason else "user"
            return await self._append_quietly(record.id, "run.cancelled", {"reason": reason})
        return await self._append_quietly(
            record.id, "run.failed", {"error": public_error("run_interrupted")}
        )

    async def _append_quietly(
        self, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
    ) -> bool:
        try:
            current = await self.store.get(run_id)
            if current is not None and current.last_sequence == 0:
                await self.store.append(
                    run_id,
                    "run.started",
                    {"profile": current.profile, "model": current.model or "mia:default"},
                    at=self.engine.clock(),
                )
            await self.store.append(run_id, event_type, payload, at=self.engine.clock())
        except RunFinishedError:
            return False
        self.notifier.notify(run_id)
        return True
