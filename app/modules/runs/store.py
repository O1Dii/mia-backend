"""Run persistence: an append-only, gap-free event log per run.

``append`` is the only way to change run state after creation: it allocates the
next ``sequence`` and applies ``run_effects`` atomically, and refuses anything
after a terminal event, so a run has exactly one terminal event.
"""

from __future__ import annotations

import copy
import dataclasses
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.types import RunProfile
from app.modules.conversations.repositories import (
    IdempotencyConflictError,
    IdempotencyRepository,
)
from app.modules.runs.domain import (
    TERMINAL_EVENTS,
    TERMINAL_STATUSES,
    NewRun,
    RunFinishedError,
    RunNotFoundError,
    RunRecord,
    RunStatus,
    StoredEvent,
    run_effects,
    utc_now,
)
from app.modules.runs.models import RunEventRow, RunRow

CREATE_ENDPOINT = "POST /v1/runs"
_ACTIVE_STATUSES: tuple[RunStatus, ...] = ("running", "waiting_approval")


class RunStore(Protocol):
    async def create(
        self,
        run: NewRun,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[RunRecord, bool]:
        """Insert a ``running`` run; with a key, replay returns ``(existing, True)`` and a
        different request under the same key raises ``IdempotencyConflictError``."""
        ...

    async def get(
        self, run_id: uuid.UUID, *, user_id: uuid.UUID | None = None
    ) -> RunRecord | None: ...

    async def append(
        self, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any], *, at: datetime
    ) -> StoredEvent:
        """Append the next event. Raises ``RunNotFoundError``/``RunFinishedError``."""
        ...

    async def events_after(
        self, run_id: uuid.UUID, after_sequence: int, *, limit: int = 200
    ) -> list[StoredEvent]: ...

    async def link_turn(
        self, run_id: uuid.UUID, *, conversation_id: uuid.UUID, turn_id: uuid.UUID
    ) -> None: ...

    async def heartbeat(self, run_id: uuid.UUID, *, at: datetime) -> None: ...

    async def request_cancel(
        self, run_id: uuid.UUID, *, user_id: uuid.UUID, reason: str
    ) -> RunRecord | None:
        """Move an active run to ``cancel_requested``; return the current record
        (unchanged when already terminal) or ``None`` when missing/foreign."""
        ...


class InMemoryRunStore:
    """Process-local store for tests and fake-mode development."""

    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, RunRecord] = {}
        self._events: dict[uuid.UUID, list[StoredEvent]] = {}
        self._keys: dict[tuple[uuid.UUID, str], tuple[str, uuid.UUID]] = {}

    async def create(
        self,
        run: NewRun,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[RunRecord, bool]:
        if idempotency_key is not None:
            claimed = self._keys.get((run.user_id, idempotency_key))
            if claimed is not None:
                if claimed[0] != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was already used for a different request"
                    )
                return self._runs[claimed[1]], True
            self._keys[(run.user_id, idempotency_key)] = (request_hash or "", run.id)
        now = utc_now()
        record = RunRecord(
            id=run.id,
            user_id=run.user_id,
            profile=run.profile,
            status="running",
            created_at=now,
            updated_at=now,
            heartbeat_at=now,
            conversation_id=run.conversation_id,
            device_id=run.device_id,
            workspace_id=run.workspace_id,
            app_client_id=run.app_client_id,
            scopes=run.scopes,
            incognito=run.incognito,
            input_text=None if run.incognito else run.input_text,
        )
        self._runs[run.id] = record
        self._events[run.id] = []
        return record, False

    async def get(self, run_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> RunRecord | None:
        record = self._runs.get(run_id)
        if record is None or (user_id is not None and record.user_id != user_id):
            return None
        return record

    async def append(
        self, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any], *, at: datetime
    ) -> StoredEvent:
        record = self._runs.get(run_id)
        if record is None:
            raise RunNotFoundError("run was not found")
        if record.terminal:
            raise RunFinishedError("run already finished")
        body = copy.deepcopy(dict(payload))
        event = StoredEvent(
            id=uuid.uuid4(),
            run_id=run_id,
            sequence=record.last_sequence + 1,
            type=event_type,
            payload=body,
            at=at,
        )
        effects = run_effects(record.status, event_type, body)
        if event_type in TERMINAL_EVENTS:
            effects["finished_at"] = at
        self._runs[run_id] = dataclasses.replace(
            record, last_sequence=event.sequence, updated_at=at, heartbeat_at=at, **effects
        )
        self._events[run_id].append(event)
        return event

    async def events_after(
        self, run_id: uuid.UUID, after_sequence: int, *, limit: int = 200
    ) -> list[StoredEvent]:
        events = self._events.get(run_id, [])
        return events[after_sequence : after_sequence + limit]

    async def link_turn(
        self, run_id: uuid.UUID, *, conversation_id: uuid.UUID, turn_id: uuid.UUID
    ) -> None:
        record = self._runs[run_id]
        self._runs[run_id] = dataclasses.replace(
            record, conversation_id=conversation_id, turn_id=turn_id
        )

    async def heartbeat(self, run_id: uuid.UUID, *, at: datetime) -> None:
        record = self._runs.get(run_id)
        if record is not None and not record.terminal:
            self._runs[run_id] = dataclasses.replace(record, heartbeat_at=at)

    async def request_cancel(
        self, run_id: uuid.UUID, *, user_id: uuid.UUID, reason: str
    ) -> RunRecord | None:
        record = await self.get(run_id, user_id=user_id)
        if record is None:
            return None
        if record.status in _ACTIVE_STATUSES:
            record = dataclasses.replace(
                record, status="cancel_requested", cancel_reason=reason, updated_at=utc_now()
            )
            self._runs[run_id] = record
        return record


def _record(row: RunRow) -> RunRecord:
    return RunRecord(
        id=row.id,
        user_id=row.user_id,
        profile=cast(RunProfile, row.profile),
        status=cast(RunStatus, row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_sequence=row.next_sequence - 1,
        model=row.model,
        conversation_id=row.conversation_id,
        turn_id=row.turn_id,
        device_id=row.device_id,
        workspace_id=row.workspace_id,
        app_client_id=row.app_client_id,
        scopes=frozenset(row.scopes or ()),
        incognito=row.incognito,
        input_text=row.input_text,
        response=row.response,
        error=row.error,
        cancel_reason=row.cancel_reason,
        steps=row.steps,
        tool_calls=row.tool_calls,
        usage=row.usage,
        heartbeat_at=row.heartbeat_at,
        finished_at=row.finished_at,
    )


def _event(row: RunEventRow) -> StoredEvent:
    return StoredEvent(
        id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        type=row.type,
        payload=row.payload,
        at=row.at,
    )


class SqlRunStore:
    """PostgreSQL store; each call owns one short transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        run: NewRun,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[RunRecord, bool]:
        async with self._session_factory() as session, session.begin():
            claim = None
            if idempotency_key is not None:
                idempotency = IdempotencyRepository(session)
                claim = await idempotency.claim(
                    user_id=run.user_id,
                    endpoint=CREATE_ENDPOINT,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash or "",
                )
                if claim.replayed:
                    if claim.record.resource_kind != "run" or claim.record.resource_id is None:
                        raise IdempotencyConflictError("idempotent run has no completed resource")
                    existing = await session.get(RunRow, claim.record.resource_id)
                    if existing is None:
                        raise IdempotencyConflictError("idempotent run is unavailable")
                    return _record(existing), True
            now = utc_now()
            row = RunRow(
                id=run.id,
                user_id=run.user_id,
                workspace_id=run.workspace_id,
                profile=run.profile,
                status="running",
                conversation_id=run.conversation_id,
                device_id=run.device_id,
                app_client_id=run.app_client_id,
                scopes=sorted(run.scopes),
                incognito=run.incognito,
                input_text=None if run.incognito else run.input_text,
                next_sequence=1,
                steps=0,
                tool_calls=0,
                created_at=now,
                updated_at=now,
                heartbeat_at=now,
            )
            session.add(row)
            await session.flush()
            if claim is not None:
                await IdempotencyRepository(session).bind_resource(
                    claim.record, resource_kind="run", resource_id=run.id
                )
            return _record(row), False

    async def get(self, run_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> RunRecord | None:
        statement = select(RunRow).where(RunRow.id == run_id)
        if user_id is not None:
            statement = statement.where(RunRow.user_id == user_id)
        async with self._session_factory() as session:
            row = await session.scalar(statement)
            return _record(row) if row is not None else None

    async def append(
        self, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any], *, at: datetime
    ) -> StoredEvent:
        body = copy.deepcopy(dict(payload))
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
            if row is None:
                raise RunNotFoundError("run was not found")
            if row.status in TERMINAL_STATUSES:
                raise RunFinishedError("run already finished")
            sequence = row.next_sequence
            row.next_sequence = sequence + 1
            for name, value in run_effects(cast(RunStatus, row.status), event_type, body).items():
                setattr(row, name, value)
            if event_type in TERMINAL_EVENTS:
                row.finished_at = at
            row.updated_at = at
            row.heartbeat_at = at
            event = RunEventRow(
                id=uuid.uuid4(),
                run_id=run_id,
                sequence=sequence,
                type=event_type,
                payload=body,
                at=at,
            )
            session.add(event)
            await session.flush()
            return _event(event)

    async def events_after(
        self, run_id: uuid.UUID, after_sequence: int, *, limit: int = 200
    ) -> list[StoredEvent]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(RunEventRow)
                .where(RunEventRow.run_id == run_id, RunEventRow.sequence > after_sequence)
                .order_by(RunEventRow.sequence)
                .limit(limit)
            )
            return [_event(row) for row in rows]

    async def link_turn(
        self, run_id: uuid.UUID, *, conversation_id: uuid.UUID, turn_id: uuid.UUID
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(RunRow)
                .where(RunRow.id == run_id)
                .values(conversation_id=conversation_id, turn_id=turn_id)
            )

    async def heartbeat(self, run_id: uuid.UUID, *, at: datetime) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(RunRow)
                .where(RunRow.id == run_id, RunRow.status.not_in(TERMINAL_STATUSES))
                .values(heartbeat_at=at)
            )

    async def request_cancel(
        self, run_id: uuid.UUID, *, user_id: uuid.UUID, reason: str
    ) -> RunRecord | None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(RunRow)
                .where(
                    RunRow.id == run_id,
                    RunRow.user_id == user_id,
                    RunRow.status.in_(_ACTIVE_STATUSES),
                )
                .values(status="cancel_requested", cancel_reason=reason, updated_at=utc_now())
            )
            row = await session.scalar(
                select(RunRow).where(RunRow.id == run_id, RunRow.user_id == user_id)
            )
            return _record(row) if row is not None else None
