"""Canonical user persistence models."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared metadata for the initial MIA schema."""


class User(Base):
    """A MIA account linked to one Google subject."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("google_sub", name="uq_users_google_sub"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    display_name: Mapped[str | None] = mapped_column(String(160))
    avatar_url: Mapped[str | None] = mapped_column(String(2048))
    locale: Mapped[str] = mapped_column(
        String(35), nullable=False, default="en", server_default="en"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountDeletion(Base):
    """Journal of account deletions (M17-R1): when, how, what was revoked — never content.

    ``user_id`` deliberately has no foreign key: the ``users`` row is gone afterwards and
    nothing else maps this random id to a person (no e-mail, no Google subject).
    """

    __tablename__ = "account_deletions"
    __table_args__ = (Index("ix_account_deletions_user", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    requested_via: Mapped[str] = mapped_column(String(16), nullable=False)
    """``bearer`` (Android) or ``cookie`` (web)."""
    provider_connections: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    """Plugin accounts whose tokens were deleted and revoked at the provider (best effort)."""
    memory_shredded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    """The user's data key existed and was destroyed (crypto-shred)."""
