"""In-process MCP server factory for builtin and declarative manifests.

The tool router (M13) connects to the returned server through the SDK in-memory
transport; it does not import plugin code. Wire it as::

    factory = make_inprocess_factory(HostServices(state=SqlPluginStateStore(...), ...))
    server = factory(manifest, plugin_dir)
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from mcp.server.mcpserver import MCPServer

from app.modules.plugins.builtin.services import HostServices

BUILTIN_MODULE_PREFIX: Final = "app.plugins.builtin."

InprocessFactory = Callable[[Mapping[str, Any], Path], MCPServer]


class UnsupportedServerError(ValueError):
    """The manifest cannot run in process (remote server, foreign module, bad module)."""


def build_inprocess_server(
    manifest: Mapping[str, Any], plugin_dir: Path, *, services: HostServices
) -> MCPServer:
    """Create the MCP server of a ``server.kind: builtin | declarative`` manifest."""
    server = manifest.get("server") or {}
    kind = server.get("kind")
    if kind == "builtin":
        module_name = str(server.get("module", ""))
        # Only first-party builtin packages are importable; a manifest is data, not code.
        if not module_name.startswith(BUILTIN_MODULE_PREFIX):
            raise UnsupportedServerError(f"builtin module must be under {BUILTIN_MODULE_PREFIX}")
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            raise UnsupportedServerError(f"cannot import {module_name}") from error
        if getattr(module, "PLUGIN_ID", None) != manifest.get("id"):
            raise UnsupportedServerError(f"{module_name} does not implement {manifest.get('id')}")
        built: MCPServer = module.build_server(services, manifest, plugin_dir)
        return built
    if kind == "declarative":
        from app.modules.plugins.declarative.host import build_declarative_server

        return build_declarative_server(manifest, services, plugin_dir)
    raise UnsupportedServerError(f"server.kind {kind!r} does not run in process")


def make_inprocess_factory(services: HostServices) -> InprocessFactory:
    """Bind the host services; the result has the router signature ``(manifest, plugin_dir)``."""

    def factory(manifest: Mapping[str, Any], plugin_dir: Path) -> MCPServer:
        return build_inprocess_server(manifest, plugin_dir, services=services)

    return factory
