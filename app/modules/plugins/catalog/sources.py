"""Wiring helpers between the store and the router/installer ports.

- :class:`ReleaseManifestSource` gives the router ``mia.json`` and a package directory
  (``SKILL.md``, bundled UI) of any published release; files are materialized from the
  database on first use into a directory named after the package digest.
- :class:`CompositeManifestSource` / :class:`CompositeCatalog` try the sources in order,
  so builtin packages in the image keep working next to published releases.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from app.modules.plugins.catalog.package import PackageFile, decode_files
from app.modules.plugins.catalog.service import CatalogService
from app.modules.plugins.install.consent import requires_connection
from app.modules.plugins.install.grants import required_grants
from app.modules.plugins.registry import LoadedManifest, ManifestSource
from app.modules.workspace.catalog import CatalogPlugin, PluginCatalog

_MATERIALIZED_SUFFIXES = (".md", ".html", ".txt")


def default_packages_dir() -> Path:
    return Path(tempfile.gettempdir()) / "mia-plugin-packages"


def _materialize(directory: Path, files: dict[str, PackageFile]) -> None:
    """Write the text files of a release atomically (temp dir, then rename)."""
    if directory.is_dir():
        return
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=directory.parent, prefix=".stage-"))
    try:
        for path, file in files.items():
            if not path.lower().endswith(_MATERIALIZED_SUFFIXES):
                continue
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.content)
        try:
            os.rename(staging, directory)
        except OSError:
            if not directory.is_dir():
                raise
            shutil.rmtree(staging, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


class ReleaseManifestSource:
    """``ManifestSource`` backed by ``plugin_releases`` (any status: installs keep running)."""

    def __init__(self, catalog: CatalogService, packages_dir: Path | None = None) -> None:
        self._catalog = catalog
        self._root = packages_dir or default_packages_dir()
        self._loaded: dict[tuple[str, str], LoadedManifest] = {}

    async def load(self, plugin_id: str, version: str) -> LoadedManifest | None:
        key = (plugin_id, version)
        cached = self._loaded.get(key)
        if cached is not None:
            return cached
        release = await self._catalog.any_release(plugin_id, version)
        if release is None:
            return None
        files = decode_files(release.files)
        # The directory name carries the content digest: a changed package never reuses it.
        digest = hashlib.sha256(release.package_digest.encode()).hexdigest()[:16]
        directory = self._root / plugin_id / f"{version}-{digest}"
        await asyncio.to_thread(_materialize, directory, files)
        loaded = LoadedManifest(dict(release.manifest), directory)
        self._loaded[key] = loaded
        return loaded


class CompositeManifestSource:
    def __init__(self, *sources: ManifestSource) -> None:
        self._sources: Sequence[ManifestSource] = sources

    async def load(self, plugin_id: str, version: str) -> LoadedManifest | None:
        for source in self._sources:
            loaded = await source.load(plugin_id, version)
            if loaded is not None:
                return loaded
        return None


class CompositeCatalog:
    """Published releases first, then the builtin packages of the image.

    A builtin fallback gets the same ``grants`` and ``requires_connection`` a release would,
    so installs from either source carry the consent vocabulary of M15.
    """

    def __init__(self, primary: PluginCatalog, *fallbacks: PluginCatalog) -> None:
        self._primary = primary
        self._fallbacks: Sequence[PluginCatalog] = fallbacks

    async def resolve(self, plugin_id: str, version: str) -> CatalogPlugin | None:
        found = await self._primary.resolve(plugin_id, version)
        if found is not None:
            return found
        for fallback in self._fallbacks:
            plugin = await fallback.resolve(plugin_id, version)
            if plugin is None:
                continue
            if plugin.manifest is None:
                return plugin
            return replace(
                plugin,
                grants=tuple(sorted(required_grants(plugin.manifest))),
                requires_connection=requires_connection(plugin.manifest),
            )
        return None
