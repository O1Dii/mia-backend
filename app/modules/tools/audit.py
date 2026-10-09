"""Router audit trail in ``tool_executions`` (continues the S17 table, no content).

A row is written *before* an external call (``running`` with its
``operation_id``) and finished afterwards; policy refusals are written as one
terminal row. Rows carry hashes, ids, risk, status, error code and duration -
never arguments, results or approval text.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.tools.models import ToolExecution, ToolExecutionStatus

logger = logging.getLogger(__name__)


def content_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    operation_id: uuid.UUID
    user_id: str
    run_id: str
    tool_call_id: str
    tool: str
    arguments_hash: str
    """Hex SHA-256 of canonical arguments (no prefix, 64 chars)."""
    risk: str | None
    executor: str | None
    profile: str
    workspace_id: str | None = None
    plugin_id: str | None = None
    installation_id: str | None = None
    approval_id: str | None = None


@dataclass(slots=True)
class AuditRecord:
    entry: AuditEntry
    status: str
    error_code: str | None = None
    result_digest: str | None = None
    duration_ms: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ToolAudit(Protocol):
    async def begin(self, entry: AuditEntry) -> None:
        """Record an operation as ``running`` before it is dispatched."""
        ...

    async def finish(
        self,
        entry: AuditEntry,
        *,
        status: str,
        error_code: str | None = None,
        result_digest: str | None = None,
        duration_ms: int | None = None,
    ) -> None: ...

    async def record(self, entry: AuditEntry, *, status: str, error_code: str | None) -> None:
        """One terminal row for a call that was refused before dispatch."""
        ...


class InMemoryToolAudit:
    def __init__(self) -> None:
        self.records: dict[uuid.UUID, AuditRecord] = {}

    async def begin(self, entry: AuditEntry) -> None:
        self.records[entry.operation_id] = AuditRecord(entry, "running")

    async def finish(
        self,
        entry: AuditEntry,
        *,
        status: str,
        error_code: str | None = None,
        result_digest: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        record = self.records[entry.operation_id]
        record.status, record.error_code = status, error_code
        record.result_digest, record.duration_ms = result_digest, duration_ms

    async def record(self, entry: AuditEntry, *, status: str, error_code: str | None) -> None:
        self.records[entry.operation_id] = AuditRecord(entry, status, error_code)


def _uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class SqlToolAudit:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def _row(self, entry: AuditEntry, status: ToolExecutionStatus) -> ToolExecution:
        return ToolExecution(
            id=uuid.uuid4(),
            user_id=uuid.UUID(entry.user_id),
            call_id=entry.operation_id,
            operation_id=entry.operation_id,
            name=entry.tool[:128],
            arguments_hash=entry.arguments_hash,
            status=status,
            run_id=_uuid(entry.run_id),
            tool_call_id=entry.tool_call_id[:128],
            workspace_id=entry.workspace_id,
            profile=entry.profile,
            executor=entry.executor,
            risk=entry.risk,
            plugin_id=entry.plugin_id,
            installation_id=entry.installation_id,
            approval_id=_uuid(entry.approval_id),
        )

    async def begin(self, entry: AuditEntry) -> None:
        async with self._session_factory() as session, session.begin():
            session.add(self._row(entry, ToolExecutionStatus.RUNNING))

    async def finish(
        self,
        entry: AuditEntry,
        *,
        status: str,
        error_code: str | None = None,
        result_digest: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ToolExecution)
                .where(
                    ToolExecution.operation_id == entry.operation_id,
                    ToolExecution.status == ToolExecutionStatus.RUNNING,
                )
                .values(
                    status=ToolExecutionStatus(status),
                    error_code=error_code,
                    result_digest=result_digest,
                    duration_ms=duration_ms,
                    finished_at=datetime.now(UTC),
                )
            )

    async def record(self, entry: AuditEntry, *, status: str, error_code: str | None) -> None:
        row = self._row(entry, ToolExecutionStatus(status))
        row.error_code = error_code
        row.finished_at = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            session.add(row)
