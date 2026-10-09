"""Durable jobs queue, attempt history, schedules and fired plugin deadlines (M16).

The queue lives in PostgreSQL: workers claim rows with ``FOR UPDATE SKIP LOCKED`` and
hold a lease that they renew; an expired lease makes the job claimable again. Schedules
store their next occurrence in UTC, so they survive any worker restart.
"""

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
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base

JOB_STATUSES = ("queued", "running", "waiting_approval", "succeeded", "failed", "cancelled")
ACTIVE_JOB_STATUSES = ("queued", "running", "waiting_approval")


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'failed', "
            "'cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint("kind IN ('run', 'reminder')", name="ck_jobs_kind"),
        CheckConstraint(
            "origin_type IN ('run', 'schedule', 'app', 'user')", name="ck_jobs_origin_type"
        ),
        CheckConstraint("attempt >= 1", name="ck_jobs_attempt_positive"),
        UniqueConstraint("dedupe_key", name="uq_jobs_dedupe_key"),
        Index("ix_jobs_user_created", "user_id", "created_at"),
        Index(
            "ix_jobs_queued",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_jobs_leased",
            "lease_expires_at",
            postgresql_where=text("status IN ('running', 'waiting_approval')"),
        ),
        Index("ix_jobs_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    origin_type: Mapped[str] = mapped_column(String(16), nullable=False)
    origin_id: Mapped[str] = mapped_column(String(256), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    delivery: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    """``schedule:<id>:<occurrence>``: one job per schedule occurrence, whoever fires it."""
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobAttemptRow(Base):
    """Run history: one row per claim of a job."""

    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_job_attempts_job_attempt"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('succeeded', 'failed', 'cancelled', 'retry', "
            "'interrupted', 'lease_lost')",
            name="ck_job_attempts_outcome",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(64))


class ScheduleRow(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'paused')", name="ck_schedules_status"),
        CheckConstraint("kind IN ('run', 'reminder')", name="ck_schedules_kind"),
        CheckConstraint(
            "misfire_policy IN ('skip', 'run_once', 'limited_catch_up')",
            name="ck_schedules_misfire_policy",
        ),
        CheckConstraint(
            "catch_up_limit BETWEEN 1 AND 24", name="ck_schedules_catch_up_limit_range"
        ),
        Index("ix_schedules_user_created", "user_id", "created_at"),
        Index(
            "ix_schedules_due",
            "next_run_at",
            postgresql_where=text("status = 'active' AND next_run_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    rule: Mapped[str] = mapped_column(String(1000), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    dtstart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """Anchor of the recurrence (from ``DTSTART`` or the creation time)."""
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    delivery: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    misfire_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    catch_up_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeadlineFireRow(Base):
    """A timer/alarm/focus deadline (M14 plugin state) that has been delivered once."""

    __tablename__ = "deadline_fires"
    __table_args__ = (
        PrimaryKeyConstraint(
            "workspace_id", "plugin_id", "item_id", "deadline_ms", name="pk_deadline_fires"
        ),
        Index("ix_deadline_fires_fired_at", "fired_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deadline_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
