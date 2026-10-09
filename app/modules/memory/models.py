"""Memory Ledger tables (ADR-0012, ``contracts/mvp/v1/memory.schema.json``).

Personal content (fact text, person names/aliases/relation/identifiers,
preference values, pending person drafts) is stored only as AES-GCM
ciphertext under the user's DEK (``app.core.crypto`` via M11's ``KeyVault``).
Plain columns hold metadata needed for filtering: status, category,
sensitivity, source type, timestamps. ``search_index`` is a ``tsvector`` of
keyed hashes of word stems (blind index): Postgres FTS ranks facts without the
words being readable, and crypto-shredding the DEK makes the index useless.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base

CATEGORIES = ("profile", "work", "home", "health", "finance", "interests", "other")
_STATUS_CHECK = "status IN ('suggested', 'active', 'rejected')"
_SENSITIVITY_CHECK = "sensitivity IN ('normal', 'sensitive')"
_CATEGORY_CHECK = "category IN (" + ", ".join(f"'{c}'" for c in CATEGORIES) + ")"
_SOURCE_CHECK = (
    "source_type IN ('user_said', 'turn', 'plugin', 'import', 'app', 'device_contacts', 'telegram')"
)
_RETENTION_CHECK = "history_retention IN ('30d', '1y', 'forever', 'none')"


def _now_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MemoryFact(Base):
    __tablename__ = "memory_facts"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="ck_memory_facts_status"),
        CheckConstraint(_SENSITIVITY_CHECK, name="ck_memory_facts_sensitivity"),
        CheckConstraint(_CATEGORY_CHECK, name="ck_memory_facts_category"),
        CheckConstraint(_SOURCE_CHECK, name="ck_memory_facts_source"),
        CheckConstraint("scope IN ('user', 'automation')", name="ck_memory_facts_scope"),
        Index("ix_memory_facts_user_status", "user_id", "status", "created_at"),
        Index("ix_memory_facts_search", "search_index", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(256))
    text_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    person_draft_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    """Pending ``Person`` payload (JSON) applied when the fact is confirmed."""
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_people.id", ondelete="SET NULL")
    )
    search_index: Mapped[str] = mapped_column(TSVECTOR, nullable=False)
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = _now_column()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryPerson(Base):
    __tablename__ = "memory_people"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="ck_memory_people_status"),
        CheckConstraint(_SENSITIVITY_CHECK, name="ck_memory_people_sensitivity"),
        CheckConstraint(_SOURCE_CHECK, name="ck_memory_people_source"),
        Index("ix_memory_people_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(256))
    payload_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    """JSON ``{display_name, aliases, relation, identifiers}``."""
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = _now_column()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryPreference(Base):
    __tablename__ = "memory_preferences"
    __table_args__ = (
        CheckConstraint(_SOURCE_CHECK, name="ck_memory_preferences_source"),
        Index("uq_memory_preferences_user_key", "user_id", "key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = _now_column()


class MemorySettings(Base):
    """Privacy settings (not personal content, so stored in the clear)."""

    __tablename__ = "memory_settings"
    __table_args__ = (CheckConstraint(_RETENTION_CHECK, name="ck_memory_settings_retention"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    autosave_categories: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, server_default="{}"
    )
    history_retention: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="forever"
    )
    updated_at: Mapped[datetime] = _now_column()


class MemoryAccessLog(Base):
    """Who read or changed memory, when, and how many records — never content."""

    __tablename__ = "memory_access_log"
    __table_args__ = (Index("ix_memory_access_log_user_at", "user_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    at: Mapped[datetime] = _now_column()
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    """``run`` | ``user`` | ``app`` | ``system``."""
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    app_client_id: Mapped[str | None] = mapped_column(String(512))
