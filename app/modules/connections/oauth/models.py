"""Persistence of plugin account connections (OAuth to remote MCP servers).

Tokens never leave this table in clear text: access and refresh tokens (and a registered
client secret) are one JSON blob sealed with the per-user DEK of M11
(``KeyVault.seal_secret``, purpose ``oauth_token``, record id = connection id).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.modules.users.models import Base
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

CONNECTION_STATUSES = ("pending", "connected", "expired", "revoked")
TOKEN_PURPOSE = "oauth_token"


class PluginConnection(Base):
    """The OAuth account of one remote-plugin installation (at most one)."""

    __tablename__ = "plugin_connections"
    __table_args__ = (
        UniqueConstraint("installation_id", name="uq_plugin_connections_installation"),
        ForeignKeyConstraint(
            ["installation_id", "workspace_id"],
            ["installations.id", "installations.workspace_id"],
            name="fk_plugin_connections_installation_workspace",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending', 'connected', 'expired', 'revoked')",
            name="ck_plugin_connections_status",
        ),
        Index("ix_plugin_connections_user", "user_id"),
        Index(
            "uq_plugin_connections_flow_state",
            "flow_state_hash",
            unique=True,
            postgresql_where="flow_state_hash IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    installation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(2048))
    """Canonical resource indicator (RFC 8707) of the MCP server."""
    issuer: Mapped[str | None] = mapped_column(String(2048))
    authorization_endpoint: Mapped[str | None] = mapped_column(String(2048))
    token_endpoint: Mapped[str | None] = mapped_column(String(2048))
    revocation_endpoint: Mapped[str | None] = mapped_column(String(2048))
    client_id: Mapped[str | None] = mapped_column(String(512))
    auth_method: Mapped[str | None] = mapped_column(String(32))
    scopes: Mapped[str | None] = mapped_column(Text)
    account_label: Mapped[str | None] = mapped_column(String(200))
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flow_state_hash: Mapped[str | None] = mapped_column(String(64))
    flow_verifier_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    flow_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flow_return_to: Mapped[str | None] = mapped_column(String(300))
    flow_binding_hash: Mapped[str | None] = mapped_column(String(64))
    """SHA-256 of the browser binding value (web flows only, OI-M15-003)."""
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
