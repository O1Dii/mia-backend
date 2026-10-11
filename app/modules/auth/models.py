"""Persistent device and refresh-session models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base


class Device(Base):
    """An Android installation associated with one MIA user."""

    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("platform = 'android'", name="ck_devices_platform_android"),
        Index("ix_devices_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False, default="android")
    app_version: Mapped[str] = mapped_column(String(64), nullable=False)
    device_name: Mapped[str | None] = mapped_column(String(128))
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    """Last contact with the API (sign-in; since M84 also requests and heartbeats, throttled)."""
    display_name: Mapped[str | None] = mapped_column(String(128))
    """Name chosen by the user on the «Устройства» screen (M84); wins over ``device_name``."""
    status_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    """The app's last heartbeat report (M84): OS, model, OS permission per capability.
    Names and states only; no phone numbers or other content."""
    status_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    """One rotating refresh-token family for a concrete device login."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_auth_sessions_generation_nonnegative"),
        Index("ix_auth_sessions_family_active", "family_id", "revoked_at"),
        Index("ix_auth_sessions_user_device", "user_id", "device_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reuse_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="SET NULL")
    )


class WebSession(Base):
    """A revocable browser session (HttpOnly cookie) for the same MIA user as Android.

    Only an HMAC of the opaque cookie value is stored; the value itself never
    reaches the database, logs or responses other than ``Set-Cookie``.
    """

    __tablename__ = "web_sessions"
    __table_args__ = (Index("ix_web_sessions_user_expires", "user_id", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE")
    )
    """Set for the app's WebView session (M24 hand-off, M70): requests of this session come
    from that device (ADR-0016 §3). NULL for a browser session."""
    display_name: Mapped[str | None] = mapped_column(String(128))
    """Name chosen by the user on the «Устройства» screen (M84)."""
    client_label: Mapped[str | None] = mapped_column(String(128))
    """«браузер Chrome на macOS», derived once from the User-Agent (never the raw header)."""
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Last request of this session (M84, throttled)."""
