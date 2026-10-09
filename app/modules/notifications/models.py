"""Assistant inbox, device push tokens and per-channel deliveries (M16)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base


class InboxItemRow(Base):
    __tablename__ = "inbox_items"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('job', 'schedule', 'plugin', 'system')",
            name="ck_inbox_items_source_type",
        ),
        Index("ix_inbox_items_user_created", "user_id", "created_at"),
        Index(
            "ix_inbox_items_unannounced",
            "user_id",
            "created_at",
            postgresql_where=text("announced_at IS NULL AND read_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Mentioned in a «while you were away» note of a later run."""


class PushTokenRow(Base):
    __tablename__ = "push_tokens"
    __table_args__ = (
        CheckConstraint("provider IN ('fcm')", name="ck_push_tokens_provider"),
        UniqueConstraint("device_id", "provider", name="uq_push_tokens_device_provider"),
        Index("ix_push_tokens_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Set when FCM reports the token unregistered; the device re-registers a new one."""


class DeliveryRow(Base):
    """One channel delivery (push, telegram) of one inbox item; retried with backoff."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint("channel IN ('push', 'telegram')", name="ck_deliveries_channel"),
        CheckConstraint(
            "status IN ('pending', 'sent', 'skipped', 'failed')", name="ck_deliveries_status"
        ),
        UniqueConstraint("inbox_item_id", "channel", name="uq_deliveries_item_channel"),
        Index(
            "ix_deliveries_pending",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    inbox_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inbox_items.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
