"""Timer: several named countdowns; the deadline is stored, firing is M16's job."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.modules.plugins.builtin.services import HostServices
from app.plugins.builtin._countdown import build_countdown_server

PLUGIN_ID = "builtin.timer"


def build_server(
    services: HostServices, manifest: Mapping[str, Any], plugin_dir: Path
) -> MCPServer:
    return build_countdown_server(
        services,
        manifest,
        plugin_dir,
        multiple=True,
        default_label="Таймер",
        default_seconds=300,
        max_seconds=86400,
        noun="таймер",
    )
