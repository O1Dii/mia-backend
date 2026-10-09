"""Focus: one pomodoro-style countdown (25 min work, 5 min break)."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.modules.plugins.builtin.services import HostServices
from app.plugins.builtin._countdown import build_countdown_server

PLUGIN_ID = "builtin.focus"


def build_server(
    services: HostServices, manifest: Mapping[str, Any], plugin_dir: Path
) -> MCPServer:
    return build_countdown_server(
        services,
        manifest,
        plugin_dir,
        multiple=False,
        default_label="Фокус",
        default_seconds=1500,
        max_seconds=10800,
        noun="фокус",
    )
