"""Installed-plugin registry: installations + ``mia.json`` manifests (M13).

The tool router reads plugins only through this package; plugin code (M14
builtins, declarative servers) is reached through an injected factory.
"""

from app.modules.plugins.registry.installed import (
    NEEDS_ATTENTION,
    InstallationReader,
    InstallationRecord,
    InstalledPlugin,
    PluginRegistry,
    SkillSummary,
    SqlInstallationReader,
    StaticInstallationReader,
)
from app.modules.plugins.registry.limits import (
    MAX_PLUGIN_TOOLS,
    MODEL_TOOL_LIMIT,
    PLUGIN_TOOL_BUDGET,
)
from app.modules.plugins.registry.manifest import (
    ManifestError,
    PluginManifest,
    ServerSpec,
    ToolPolicy,
    parse_manifest,
)
from app.modules.plugins.registry.source import (
    DirectoryManifestSource,
    LoadedManifest,
    ManifestSource,
    StaticManifestSource,
)

__all__ = [
    "MAX_PLUGIN_TOOLS",
    "MODEL_TOOL_LIMIT",
    "NEEDS_ATTENTION",
    "PLUGIN_TOOL_BUDGET",
    "DirectoryManifestSource",
    "InstallationReader",
    "InstallationRecord",
    "InstalledPlugin",
    "LoadedManifest",
    "ManifestError",
    "ManifestSource",
    "PluginManifest",
    "PluginRegistry",
    "ServerSpec",
    "SkillSummary",
    "SqlInstallationReader",
    "StaticManifestSource",
    "StaticInstallationReader",
    "ToolPolicy",
    "parse_manifest",
]
