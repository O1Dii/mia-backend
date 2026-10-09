"""Where the router finds a plugin's ``mia.json`` and package directory.

The catalog (M15) publishes versions later; builtin plugins (M14) ship as
directories ``app/plugins/builtin/<id>/`` with ``mia.json``, ``SKILL.md`` and
``ui/``. The router never imports plugin code: it reads manifests through this
port and asks an injected factory for in-process servers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.ports.types import JsonObject

logger = logging.getLogger(__name__)

MAX_MANIFEST_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class LoadedManifest:
    manifest: JsonObject
    plugin_dir: Path | None = None
    """Package directory (``SKILL.md``, ``ui/``); None when only the JSON is known."""


class ManifestSource(Protocol):
    async def load(self, plugin_id: str, version: str) -> LoadedManifest | None:
        """Return the manifest of exactly ``plugin_id``@``version`` or None."""
        ...


class StaticManifestSource:
    """In-memory manifests for tests and explicit wiring."""

    def __init__(self, manifests: Iterable[LoadedManifest] = ()) -> None:
        self._items: dict[tuple[str, str], LoadedManifest] = {}
        for item in manifests:
            self.add(item)

    def add(self, item: LoadedManifest) -> None:
        plugin_id, version = item.manifest.get("id"), item.manifest.get("version")
        if not isinstance(plugin_id, str) or not isinstance(version, str):
            raise ValueError("manifest needs id and version")
        self._items[(plugin_id, version)] = item

    async def load(self, plugin_id: str, version: str) -> LoadedManifest | None:
        return self._items.get((plugin_id, version))


class DirectoryManifestSource:
    """Read ``<root>/<dir>/mia.json`` packages (builtin plugins, M14).

    Directories are scanned once, lazily; a broken manifest is skipped with a
    warning instead of failing every run.
    """

    def __init__(self, *roots: Path) -> None:
        self._roots = roots
        self._index: dict[tuple[str, str], LoadedManifest] | None = None

    def _scan(self) -> dict[tuple[str, str], LoadedManifest]:
        index: dict[tuple[str, str], LoadedManifest] = {}
        for root in self._roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*/mia.json")):
                try:
                    if path.stat().st_size > MAX_MANIFEST_BYTES:
                        raise ValueError("manifest too large")
                    data = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    logger.warning("plugin_manifest_unreadable", extra={"path": str(path)})
                    continue
                if not isinstance(data, Mapping):
                    continue
                plugin_id, version = data.get("id"), data.get("version")
                if isinstance(plugin_id, str) and isinstance(version, str):
                    index[(plugin_id, version)] = LoadedManifest(dict(data), path.parent)
        return index

    async def load(self, plugin_id: str, version: str) -> LoadedManifest | None:
        if self._index is None:
            self._index = await asyncio.to_thread(self._scan)
        return self._index.get((plugin_id, version))
