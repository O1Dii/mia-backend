"""Persistent runs and their append-only event log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base

_STATUS_CHECK = (
    "status IN ('running', 'waiting_approval', 'cancel_requested', "
    "'completed', 'failed', 'cancelled')"
)
_PROFILE_CHECK = "profile IN ('voice', 'chat', 'job', 'app')"


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="ck_runs_status"),
        CheckConstraint(_PROFILE_CHECK, name="ck_runs_profile"),
        CheckConstraint("next_sequence >= 1", name="ck_runs_next_sequence_positive"),
        Index("ix_runs_user_created", "user_id", "created_at"),
        Index(
            "ix_runs_active_heartbeat",
            "heartbeat_at",
            postgresql_where=text("status NOT IN ('completed', 'failed', 'cancelled')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str | None] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str | None] = mapped_column(String(200))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("turns.id", ondelete="SET NULL")
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    app_client_id: Mapped[str | None] = mapped_column(String(512))
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(200)), nullable=False, default=list, server_default="{}"
    )
    incognito: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    input_text: Mapped[str | None] = mapped_column(Text)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cancel_reason: Mapped[str | None] = mapped_column(String(32))
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    next_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEventRow(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        CheckConstraint("sequence >= 1", name="ck_run_events_sequence_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
