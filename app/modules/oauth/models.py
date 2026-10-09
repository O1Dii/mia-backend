"""Persistence of «Войти через Mia»: grants, authorization requests, codes, tokens, spend.

Credentials are never stored in clear text: authorization codes, access and refresh
tokens and consent request ids are kept only as SHA-256 hex digests. Scopes are
space-separated strings (RFC 6749 form).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base

CLIENT_ID_MAX = 255
"""Fits ``memory_*.source_ref`` (256) so app suggestions can name their client."""


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _user() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class OAuthGrant(Base):
    """What one user allowed one client (a CIMD URL) to do; revocable as a whole."""

    __tablename__ = "oauth_grants"
    __table_args__ = (
        Index(
            "uq_oauth_grants_active",
            "user_id",
            "client_id",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
        CheckConstraint("monthly_limit_usd >= 0", name="ck_oauth_grants_limit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = _user()
    client_id: Mapped[str] = mapped_column(String(CLIENT_ID_MAX), nullable=False)
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    client_uri: Mapped[str | None] = mapped_column(String(2048))
    logo_uri: Mapped[str | None] = mapped_column(String(2048))
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    memory_categories: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """Categories the core may inject for ``memory.context`` (space-separated)."""
    monthly_limit_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthAuthorizationRequest(Base):
    """A validated ``/oauth/authorize`` call waiting for the user's consent."""

    __tablename__ = "oauth_authorization_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied')", name="ck_oauth_requests_status"
        ),
        Index("ix_oauth_requests_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    handle_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    client_id: Mapped[str] = mapped_column(String(CLIENT_ID_MAX), nullable=False)
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    client_uri: Mapped[str | None] = mapped_column(String(2048))
    logo_uri: Mapped[str | None] = mapped_column(String(2048))
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(1024), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(2048))
    nonce: Mapped[str | None] = mapped_column(String(512))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    """Bound on first view by a signed-in user; nobody else can see or decide it."""
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decision_key_hash: Mapped[str | None] = mapped_column(String(64))
    redirect_to: Mapped[str | None] = mapped_column(Text)
    """Replayed for the same ``Idempotency-Key``; cleared when the code is redeemed."""
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OAuthCode(Base):
    """A single-use authorization code bound to client, redirect URI and PKCE challenge."""

    __tablename__ = "oauth_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("oauth_grants.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = _user()
    client_id: Mapped[str] = mapped_column(String(CLIENT_ID_MAX), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(String(2048))
    nonce: Mapped[str | None] = mapped_column(String(512))
    auth_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthToken(Base):
    """An access or refresh token; ``family_id`` links one refresh rotation chain."""

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        CheckConstraint("kind IN ('access', 'refresh')", name="ck_oauth_tokens_kind"),
        Index("ix_oauth_tokens_grant", "grant_id"),
        Index("ix_oauth_tokens_family", "family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("oauth_grants.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    code_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID] = _user()
    client_id: Mapped[str] = mapped_column(String(CLIENT_ID_MAX), nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """Refresh tokens only: set when exchanged; a second use revokes the family."""
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthAppUsage(Base):
    """Spend journal: one row per model-backed app request."""

    __tablename__ = "oauth_app_usage"
    __table_args__ = (
        Index("ix_oauth_app_usage_grant_at", "grant_id", "at"),
        CheckConstraint(
            "surface IN ('chat_completions', 'responses', 'mcp')", name="ck_oauth_usage_surface"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    grant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("oauth_grants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = _user()
    client_id: Mapped[str] = mapped_column(String(CLIENT_ID_MAX), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    surface: Mapped[str] = mapped_column(String(32), nullable=False)
    at: Mapped[datetime] = _now()
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """True when the provider reported no price and Mia estimated it from tokens."""
