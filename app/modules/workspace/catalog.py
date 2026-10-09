"""Catalog lookup boundary used when installing a plugin.

M10 only persists installations. The real catalog (published `mia.json`
versions, review, builtins) is provided by M14/M15 through
:class:`PluginCatalog`; until then the composition uses an empty catalog, so
catalog installs answer 404 and nothing unreviewed can be installed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CatalogPlugin:
    plugin_id: str
    version: str
    tools_digest: str
    """Digest of the tool descriptions shown at consent; installs must match it."""
    grants: tuple[str, ...] = ()
    """Permissions requested by the manifest and granted on install."""
    manifest: Mapping[str, Any] | None = None
    requires_connection: bool = False
    """True when the plugin needs an OAuth account (status ``needs_connection``)."""


class PluginCatalog(Protocol):
    async def resolve(self, plugin_id: str, version: str) -> CatalogPlugin | None: ...


class StaticPluginCatalog:
    """In-memory catalog for tests and builtins wiring."""

    def __init__(self, plugins: Iterable[CatalogPlugin] = ()) -> None:
        self._plugins = {(plugin.plugin_id, plugin.version): plugin for plugin in plugins}

    async def resolve(self, plugin_id: str, version: str) -> CatalogPlugin | None:
        return self._plugins.get((plugin_id, version))
