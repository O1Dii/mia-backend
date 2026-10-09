"""Durable, user-scoped tool audit and approvals; raw tool output is never stored.

``tool_executions`` is shared by the legacy executor (turn-scoped rows) and the
M13 router (run-scoped rows: ``run_id``/``operation_id`` set, ``turn_id`` and
``response_id`` NULL). Router rows hold hashes and metadata only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base


class ToolExecutionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    DENIED = "denied"
    EXPIRED = "expired"
    APPROVAL_REQUIRED = "approval_required"


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint("user_id", "call_id", name="uq_tool_executions_user_call"),
        UniqueConstraint("operation_id", name="uq_tool_executions_operation_id"),
        Index("ix_tool_executions_turn", "turn_id", "created_at"),
        Index("ix_tool_executions_run", "run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    """Legacy: the realtime/text call id. Router rows: equal to ``operation_id``."""
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    response_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ToolExecutionStatus] = mapped_column(
        Enum(
            ToolExecutionStatus,
            name="tool_execution_status",
            values_callable=lambda enum: [member.value for member in enum],
            validate_strings=True,
        ),
        nullable=False,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """Legacy executor replay payload only; the router never writes it."""
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # M13 router columns (all NULL on legacy rows).
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tool_call_id: Mapped[str | None] = mapped_column(String(128))
    workspace_id: Mapped[str | None] = mapped_column(String(64))
    profile: Mapped[str | None] = mapped_column(String(16))
    executor: Mapped[str | None] = mapped_column(String(16))
    risk: Mapped[str | None] = mapped_column(String(24))
    plugin_id: Mapped[str | None] = mapped_column(String(128))
    installation_id: Mapped[str | None] = mapped_column(String(64))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result_digest: Mapped[str | None] = mapped_column(String(80))
    """``sha256:`` of the model-facing content; the content itself is not stored."""
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class ToolApproval(Base):
    """An approval bound to one tool call's exact arguments (``approval.schema.json``)."""

    __tablename__ = "tool_approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired', 'cancelled')",
            name="ck_tool_approvals_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'deny')",
            name="ck_tool_approvals_decision",
        ),
        Index("ix_tool_approvals_user_status", "user_id", "status", "created_at"),
        Index("ix_tool_approvals_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool: Mapped[str] = mapped_column(String(200), nullable=False)
    plugin_id: Mapped[str | None] = mapped_column(String(128))
    risk: Mapped[str] = mapped_column(String(24), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    args_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(8))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str | None] = mapped_column(String(16))
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Set when the router executes the approved call; an approval is single-use."""
