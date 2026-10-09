"""Store persistence: published plugin releases (immutable, one row per id + version)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.users.models import Base

RELEASE_STATUSES = ("published", "yanked")


class PluginRelease(Base):
    """One published version of a plugin: manifest, consented tool snapshot and package files.

    ``tools`` is the ``tools/list`` seen at publication joined with the manifest's risk
    overlay; ``tools_digest`` is :func:`app.modules.tools.digest.compute_tools_digest` over the
    same listing, so an installation pinned to it is never blocked by the router as changed.
    ``files`` holds the package files that are not code (``SKILL.md``, ``ui/*.html``, images)
    as ``{path: {"encoding": "utf8"|"base64", "content": str, "sha256": str}}``.
    """

    __tablename__ = "plugin_releases"
    __table_args__ = (
        UniqueConstraint("plugin_id", "version", name="uq_plugin_releases_plugin_version"),
        CheckConstraint("status IN ('published', 'yanked')", name="ck_plugin_releases_status"),
        Index("ix_plugin_releases_plugin_version_key", "plugin_id", "version_key"),
        Index("ix_plugin_releases_category", "category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plugin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    version_key: Mapped[str] = mapped_column(String(160), nullable=False)
    """Sortable form of the semver (see ``catalog.versions.version_key``)."""
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="published", server_default="published"
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    author: Mapped[str | None] = mapped_column(String(60))
    server_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    tools_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    grants: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    store_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    """Showcase-only data that is not part of ``mia.json`` (tagline, colour, art, featured)."""
    files: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    package_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
