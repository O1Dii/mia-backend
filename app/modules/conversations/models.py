"""Canonical conversation, turn, entry, and idempotency models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base


def _enum_values(enum: type[StrEnum]) -> list[str]:
    return [member.value for member in enum]


class TurnSource(StrEnum):
    ANDROID_TEXT = "android_text"
    ANDROID_VOICE = "android_voice"
    RUN_VOICE = "run_voice"
    RUN_CHAT = "run_chat"
    RUN_JOB = "run_job"
    RUN_APP = "run_app"


class TurnStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class EntryRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class EntryKind(StrEnum):
    TEXT = "text"
    TRANSCRIPT = "transcript"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ARTIFACT_REF = "artifact_ref"


class Modality(StrEnum):
    TEXT = "text"
    AUDIO = "audio"


class ChatKind(StrEnum):
    """What a conversation is to the user (ADR-0017); ``NULL`` = a legacy S-era conversation."""

    CHAT = "chat"
    TELEGRAM = "telegram"
    """The dialog with the Mia Telegram bot; one per user, ``/new`` turns it into a plain chat."""
    BACKGROUND = "background"
    """Hidden per-user context of job runs; never listed as a chat."""


class TitleSource(StrEnum):
    AUTO = "auto"
    """Provisional title from the first message; the model may replace it."""
    MODEL = "model"
    USER = "user"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    DELIVERED = "delivered"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("next_sequence >= 1", name="ck_conversations_next_sequence_positive"),
        Index("ix_conversations_user_updated", "user_id", "updated_at", "id"),
        Index(
            "uq_conversations_primary_feed",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary_feed"),
        ),
        CheckConstraint(
            "kind IS NULL OR kind IN ('chat', 'telegram', 'background')",
            name="ck_conversations_kind",
        ),
        CheckConstraint(
            "title_source IS NULL OR title_source IN ('auto', 'model', 'user')",
            name="ck_conversations_title_source",
        ),
        Index("ix_conversations_user_kind_activity", "user_id", "kind", "last_message_at"),
        Index(
            "uq_conversations_telegram_chat",
            "user_id",
            unique=True,
            postgresql_where=text("kind = 'telegram'"),
        ),
        Index(
            "uq_conversations_background",
            "user_id",
            unique=True,
            postgresql_where=text("kind = 'background'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    summary_until_sequence: Mapped[int | None] = mapped_column(BigInteger)
    """Entries with ``sequence <= summary_until_sequence`` are covered by ``summary``."""
    is_primary_feed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    """The chat created implicitly when a user has none (formerly the single feed, M12)."""
    next_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str | None] = mapped_column(String(16))
    """:class:`ChatKind` value; ``NULL`` for legacy conversations (not chats)."""
    title_source: Mapped[str | None] = mapped_column(String(8))
    """:class:`TitleSource` value of ``title``."""
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Pinned chats are listed first and never removed by retention."""
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Last user or assistant message; retention counts inactivity from here."""
    last_assistant_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Last assistant answer («Продолжить прошлый» within a few minutes)."""


class ChatSettingsRow(Base):
    """Per-user chat settings; a missing row means the defaults (60 days)."""

    __tablename__ = "chat_settings"
    __table_args__ = (
        CheckConstraint(
            "retention_days IS NULL OR retention_days IN (30, 60, 180)",
            name="ck_chat_settings_retention_days",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    retention_days: Mapped[int | None] = mapped_column(Integer)
    """Days of inactivity before an unpinned chat is deleted; ``NULL`` = never."""
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("id", "conversation_id", name="uq_turns_id_conversation"),
        UniqueConstraint(
            "conversation_id", "idempotency_key", name="uq_turns_conversation_idempotency"
        ),
        Index("ix_turns_conversation_started", "conversation_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[TurnSource] = mapped_column(
        Enum(
            TurnSource,
            name="turn_source",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    status: Mapped[TurnStatus] = mapped_column(
        Enum(
            TurnStatus,
            name="turn_status",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=TurnStatus.PENDING,
        server_default=TurnStatus.PENDING.value,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["turn_id", "conversation_id"],
            ["turns.id", "turns.conversation_id"],
            name="fk_entries_turn_conversation",
            ondelete="CASCADE",
        ),
        UniqueConstraint("conversation_id", "sequence", name="uq_entries_conversation_sequence"),
        Index("ix_entries_conversation_sequence", "conversation_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[EntryRole] = mapped_column(
        Enum(EntryRole, name="entry_role", values_callable=_enum_values, validate_strings=True),
        nullable=False,
    )
    kind: Mapped[EntryKind] = mapped_column(
        Enum(EntryKind, name="entry_kind", values_callable=_enum_values, validate_strings=True),
        nullable=False,
    )
    modality: Mapped[Modality] = mapped_column(
        Enum(Modality, name="entry_modality", values_callable=_enum_values, validate_strings=True),
        nullable=False,
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        Enum(
            DeliveryStatus,
            name="entry_delivery_status",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=DeliveryStatus.PENDING,
        server_default=DeliveryStatus.PENDING.value,
    )
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyRecord(Base):
    """One canonical mutation key scoped by user and endpoint."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "endpoint", "idempotency_key", name="uq_idempotency_user_endpoint_key"
        ),
        Index("ix_idempotency_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_kind: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
