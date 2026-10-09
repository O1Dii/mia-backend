"""Workspace domain: installations, plugin state, dashboard tiles, reviews (M10)."""

from app.modules.workspace.catalog import CatalogPlugin, PluginCatalog, StaticPluginCatalog
from app.modules.workspace.service import WorkspaceContext, WorkspaceService

__all__ = [
    "CatalogPlugin",
    "PluginCatalog",
    "StaticPluginCatalog",
    "WorkspaceContext",
    "WorkspaceService",
]
