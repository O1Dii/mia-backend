"""Installed plugins of one user: installation rows joined with their manifests.

Only installations of the caller's own workspace are ever read (the SQL reader
joins on ``workspaces.owner_user_id``), so a foreign ``workspace_id`` in a
context can never surface another tenant's plugins.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.plugins.registry.manifest import ManifestError, PluginManifest, parse_manifest
from app.modules.plugins.registry.source import ManifestSource
from app.modules.workspace.models import Installation, Workspace

logger = logging.getLogger(__name__)

SKILL_DESCRIPTION_MAX = 300
SKILL_BODY_MAX = 16_000


@dataclass(frozen=True, slots=True)
class InstallationRecord:
    id: str
    workspace_id: str
    plugin_id: str
    version: str
    status: str
    grants: tuple[str, ...]
    tools_digest: str | None
    manifest: Mapping[str, Any] | None


class InstallationReader(Protocol):
    async def installations(
        self, *, user_id: str, workspace_id: str | None
    ) -> Sequence[InstallationRecord]:
        """All installations of the user's own workspace (any status)."""
        ...


class SqlInstallationReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def installations(
        self, *, user_id: str, workspace_id: str | None
    ) -> Sequence[InstallationRecord]:
        try:
            owner = uuid.UUID(user_id)
            workspace = uuid.UUID(workspace_id) if workspace_id else None
        except ValueError:
            return ()
        query = (
            select(Installation)
            .join(Workspace, Workspace.id == Installation.workspace_id)
            .where(Workspace.owner_user_id == owner)
            .order_by(Installation.created_at, Installation.id)
        )
        if workspace is not None:
            query = query.where(Installation.workspace_id == workspace)
        async with self._session_factory() as session:
            rows = (await session.scalars(query)).all()
        return [
            InstallationRecord(
                id=str(row.id),
                workspace_id=str(row.workspace_id),
                plugin_id=row.plugin_id,
                version=row.version,
                status=row.status,
                grants=tuple(row.grants),
                tools_digest=row.tools_digest,
                manifest=row.manifest,
            )
            for row in rows
        ]


class StaticInstallationReader:
    """In-memory installations keyed by user id (tests, no-database runs)."""

    def __init__(self, items: Mapping[str, Sequence[InstallationRecord]] | None = None) -> None:
        self.items: dict[str, list[InstallationRecord]] = {
            user: list(records) for user, records in (items or {}).items()
        }

    async def installations(
        self, *, user_id: str, workspace_id: str | None
    ) -> Sequence[InstallationRecord]:
        records = self.items.get(user_id, [])
        if workspace_id is None:
            return list(records)
        return [record for record in records if record.workspace_id == workspace_id]


@dataclass(frozen=True, slots=True)
class SkillSummary:
    plugin_id: str
    path: str
    description: str


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    installation: InstallationRecord
    manifest: PluginManifest
    plugin_dir: Path | None

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    @property
    def active(self) -> bool:
        return self.installation.status == "active"


def _skill_file(plugin_dir: Path, relative: str) -> Path | None:
    candidate = (plugin_dir / relative).resolve()
    root = plugin_dir.resolve()
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def _frontmatter_description(text: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, separator, value = line.partition(":")
        if separator and key.strip() == "description":
            cleaned = " ".join(value.strip().strip("\"'").split())
            return cleaned[:SKILL_DESCRIPTION_MAX] or None
    return None


def _read_skill(plugin_dir: Path, relative: str) -> str | None:
    path = _skill_file(plugin_dir, relative)
    if path is None:
        return None
    try:
        return path.read_text("utf-8")[: SKILL_BODY_MAX * 2]
    except (OSError, UnicodeDecodeError):
        return None


class PluginRegistry:
    """Resolve the user's installations to parsed manifests (+ package dirs)."""

    def __init__(self, reader: InstallationReader, source: ManifestSource | None = None) -> None:
        self._reader = reader
        self._source = source

    async def installed(
        self, *, user_id: str, workspace_id: str | None
    ) -> Sequence[InstalledPlugin]:
        """Every installation with a usable manifest, in installation order.

        Installations without a manifest (custom MCP URL awaiting consent) or
        with an invalid one are skipped; the caller decides about ``status``.
        """
        records = await self._reader.installations(user_id=user_id, workspace_id=workspace_id)
        plugins: list[InstalledPlugin] = []
        for record in records:
            plugin = await self._resolve(record)
            if plugin is not None:
                plugins.append(plugin)
        return plugins

    async def _resolve(self, record: InstallationRecord) -> InstalledPlugin | None:
        raw: Mapping[str, Any] | None = None
        plugin_dir: Path | None = None
        if self._source is not None:
            loaded = await self._source.load(record.plugin_id, record.version)
            if loaded is not None:
                raw, plugin_dir = loaded.manifest, loaded.plugin_dir
        if raw is None:
            raw = record.manifest
        if raw is None:
            return None
        try:
            manifest = parse_manifest(raw)
        except ManifestError as error:
            logger.warning(
                "plugin_manifest_invalid",
                extra={"plugin_id": record.plugin_id, "reason": str(error)},
            )
            return None
        if (manifest.plugin_id, manifest.version) != (record.plugin_id, record.version):
            logger.warning("plugin_manifest_mismatch", extra={"plugin_id": record.plugin_id})
            return None
        return InstalledPlugin(installation=record, manifest=manifest, plugin_dir=plugin_dir)

    @staticmethod
    async def skill_summaries(plugin: InstalledPlugin) -> list[SkillSummary]:
        """Front-matter ``description`` of each packaged ``SKILL.md`` (prompt material)."""
        if plugin.plugin_dir is None:
            return []
        summaries: list[SkillSummary] = []
        for relative in plugin.manifest.skills:
            text = await asyncio.to_thread(_read_skill, plugin.plugin_dir, relative)
            description = _frontmatter_description(text) if text else None
            if description:
                summaries.append(SkillSummary(plugin.plugin_id, relative, description))
        return summaries

    @staticmethod
    async def skill_bodies(plugin: InstalledPlugin) -> list[str]:
        """Full ``SKILL.md`` texts, loaded only when the plugin is selected."""
        if plugin.plugin_dir is None:
            return []
        bodies: list[str] = []
        for relative in plugin.manifest.skills:
            text = await asyncio.to_thread(_read_skill, plugin.plugin_dir, relative)
            if text:
                bodies.append(text[:SKILL_BODY_MAX])
        return bodies
