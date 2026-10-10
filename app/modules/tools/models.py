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
    device_consent: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """``DeviceConsent`` (M70): only the target device may decide this approval."""


# -- M70: device access (ADR-0016) -------------------------------------------------------


class DeviceRunOrigin(Base):
    """Who started a run (requester of device tools): set by ``POST /v1/runs`` (M70)."""

    __tablename__ = "device_run_origins"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('device_app', 'device_webview', 'web', 'unknown')",
            name="ck_device_run_origins_kind",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE")
    )
    """The requester's own verified device (app or its WebView); NULL for a browser."""
    web_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("web_sessions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceToolDeclarationSet(Base):
    """Last declared device tools of a device, so other clients see them after a restart."""

    __tablename__ = "device_tool_declarations"

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceGrant(Base):
    """Consent given on the target device: for one run, or persistent for one requester."""

    __tablename__ = "device_grants"
    __table_args__ = (
        CheckConstraint("scope IN ('run', 'persistent')", name="ck_device_grants_scope"),
        CheckConstraint(
            "(scope = 'run') = (run_id IS NOT NULL)", name="ck_device_grants_run_scope"
        ),
        Index("ix_device_grants_lookup", "user_id", "target_device_id", "capability"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    target_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requester_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    requester_label: Mapped[str] = mapped_column(String(128), nullable=False)
    requester_key: Mapped[str | None] = mapped_column(String(80))
    requester_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE")
    )
    requester_web_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("web_sessions.id", ondelete="CASCADE")
    )
    """A persistent grant to a browser ends with its session (sign-out, expiry, pruning)."""
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceCapabilitySetting(Base):
    """Per device: may other clients ask for a capability (``ask``) or never (``deny``)."""

    __tablename__ = "device_capability_settings"
    __table_args__ = (
        CheckConstraint("remote IN ('ask', 'deny')", name="ck_device_capability_settings_remote"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    remote: Mapped[str] = mapped_column(String(8), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceAccessLogRow(Base):
    """Device reads/actions requested by other clients: what, from where, when, decision."""

    __tablename__ = "device_access_log"
    __table_args__ = (
        Index("ix_device_access_log_device", "user_id", "device_id", "created_at"),
        Index("ix_device_access_log_approval", "approval_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    requester_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    requester_label: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """For ``pending`` rows: when the consent request lapses (listed as ``expired``)."""
