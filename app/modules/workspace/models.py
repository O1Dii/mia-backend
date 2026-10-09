"""Workspace persistence: installations, plugin state, dashboard tiles and reviews.

Every tenant-owned row carries ``workspace_id``. Widgets reference their
installation through a composite foreign key ``(installation_id, workspace_id)``
so the database itself refuses a tile that points at another tenant's plugin.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base

INSTALLATION_STATUSES = ("active", "needs_connection", "needs_consent", "disabled")
INSTALLATION_SOURCES = ("catalog", "custom", "legacy_web")
TILE_SIZES = ("1x1", "2x1", "2x2", "4x2", "4x4")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class Workspace(Base):
    """The personal tenant of one MIA user (one per user in the MVP)."""

    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_workspaces_owner_user_id"),
        CheckConstraint("layout_version >= 0", name="ck_workspaces_layout_version_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(
        String(120), nullable=False, default="Personal", server_default="Personal"
    )
    layout_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Installation(Base):
    """A plugin pinned to a version, consented tools digest and granted permissions."""

    __tablename__ = "installations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "plugin_id", name="uq_installations_workspace_plugin"),
        UniqueConstraint("id", "workspace_id", name="uq_installations_id_workspace"),
        CheckConstraint(_in("status", INSTALLATION_STATUSES), name="ck_installations_status"),
        CheckConstraint(_in("source", INSTALLATION_SOURCES), name="ck_installations_source"),
        CheckConstraint("state_version >= 0", name="ck_installations_state_version_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    grants: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    tools_digest: Mapped[str | None] = mapped_column(String(128))
    custom_url: Mapped[str | None] = mapped_column(String(2048))
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PluginState(Base):
    """Per-workspace plugin data; survives reinstall unless deleted explicitly."""

    __tablename__ = "plugin_states"
    __table_args__ = (
        CheckConstraint("state_version >= 0", name="ck_plugin_states_state_version_nonnegative"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plugin_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WidgetInstance(Base):
    """A dashboard tile with separate desktop and mobile placement."""

    __tablename__ = "widget_instances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["installation_id", "workspace_id"],
            ["installations.id", "installations.workspace_id"],
            name="fk_widget_instances_installation_workspace",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("desktop_size", TILE_SIZES), name="ck_widget_instances_desktop_size"),
        CheckConstraint(_in("mobile_size", TILE_SIZES), name="ck_widget_instances_mobile_size"),
        CheckConstraint(
            "desktop_position >= 0 AND mobile_position >= 0",
            name="ck_widget_instances_position_nonnegative",
        ),
        CheckConstraint("state_version >= 0", name="ck_widget_instances_state_version_nonnegative"),
        Index("ix_widget_instances_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    installation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    desktop_size: Mapped[str] = mapped_column(String(8), nullable=False)
    desktop_position: Mapped[int] = mapped_column(Integer, nullable=False)
    mobile_size: Mapped[str] = mapped_column(String(8), nullable=False)
    mobile_position: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Review(Base):
    """One public store review per user and plugin (store API: M15)."""

    __tablename__ = "plugin_reviews"
    __table_args__ = (
        UniqueConstraint("plugin_id", "user_id", name="uq_plugin_reviews_plugin_user"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_plugin_reviews_rating"),
        Index("ix_plugin_reviews_plugin_updated", "plugin_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    text: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceMutation(Base):
    """Idempotency record: replays the stored response for a repeated key."""

    __tablename__ = "workspace_mutations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(SmallInteger)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LegacyWebImport(Base):
    """Audit and idempotency marker of one web-POC SQLite account import."""

    __tablename__ = "legacy_web_imports"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    legacy_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    document_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
