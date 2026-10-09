"""Persistence for model connections and the envelope-encryption key store."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base


class UserDataKey(Base):
    """One wrapped data-encryption key per user. Deleting the row is a crypto-shred."""

    __tablename__ = "user_data_keys"

    # FK target ``users.id`` exists on main today; M10's workspace tables may add a
    # workspace-scoped key later (see the M11 handoff).
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    master_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    rewrapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelConnection(Base):
    """A user's connection to a model provider; the secret is sealed with the user's DEK."""

    __tablename__ = "model_connections"
    __table_args__ = (
        CheckConstraint("capability IN ('tools', 'chat_only')", name="ck_model_connections_cap"),
        CheckConstraint(
            "status IN ('active', 'paused', 'needs_reauth')", name="ck_model_connections_status"
        ),
        Index("ix_model_connections_user", "user_id", "created_at"),
        Index(
            "uq_model_connections_primary",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index(
            "uq_model_connections_chatgpt_plan",
            "user_id",
            unique=True,
            postgresql_where=text("provider = 'chatgpt-plan'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    capability: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tools", server_default="tools"
    )
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_last4: Mapped[str | None] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(String(64))
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GatewaySetting(Base):
    """Host-level key/value settings, e.g. the persistent ``ext_agent_host_id``."""

    __tablename__ = "model_gateway_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
