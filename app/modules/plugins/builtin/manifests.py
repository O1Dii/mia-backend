"""Manifest source of the builtin plugins (``app/plugins/builtin/*/mia.json``)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUILTIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "builtin"


@dataclass(frozen=True, slots=True)
class BuiltinPlugin:
    manifest: Mapping[str, Any]
    directory: Path
    """Package directory with ``mia.json``, ``SKILL.md`` and ``ui/``."""

    @property
    def plugin_id(self) -> str:
        return str(self.manifest["id"])

    @property
    def version(self) -> str:
        return str(self.manifest["version"])

    def skill_text(self) -> str:
        """Concatenated ``SKILL.md`` files listed in the manifest."""
        paths = self.manifest.get("skills", [])
        return "\n\n".join((self.directory / path).read_text(encoding="utf-8") for path in paths)


class BuiltinManifestSource:
    """Lists builtin manifests by plugin id and version (one version per id today)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or BUILTIN_ROOT
        self._plugins: dict[tuple[str, str], BuiltinPlugin] | None = None

    def list(self) -> list[BuiltinPlugin]:
        return sorted(self._load().values(), key=lambda plugin: (plugin.plugin_id, plugin.version))

    def get(self, plugin_id: str, version: str | None = None) -> BuiltinPlugin | None:
        """The plugin with this id and version (the latest when ``version`` is omitted)."""
        matches = [p for p in self.list() if p.plugin_id == plugin_id]
        if version is not None:
            matches = [p for p in matches if p.version == version]
        return matches[-1] if matches else None

    def _load(self) -> dict[tuple[str, str], BuiltinPlugin]:
        if self._plugins is None:
            plugins: dict[tuple[str, str], BuiltinPlugin] = {}
            for path in sorted(self._root.glob("*/mia.json")):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                plugin = BuiltinPlugin(manifest=manifest, directory=path.parent)
                plugins[(plugin.plugin_id, plugin.version)] = plugin
            self._plugins = plugins
        return self._plugins
