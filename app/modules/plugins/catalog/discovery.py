"""Ask a plugin's MCP server what it serves (``server/discover`` + ``tools/list``).

Publication connects the way the router does (same client, same SSRF guard), so the
digest computed here is the digest the router will compute for the pinned release.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.shared.exceptions import MCPError

from app.modules.plugins.catalog.package import PluginPackage
from app.modules.plugins.catalog.validation import APP_ONLY_TOOL, Problem, ui_html_problems
from app.modules.plugins.registry import (
    InstallationRecord,
    InstalledPlugin,
    ManifestError,
    parse_manifest,
)
from app.modules.plugins.registry.manifest import RemoteAuth
from app.modules.tools.digest import ServerTool, compute_tools_digest
from app.modules.tools.mcp_client import (
    ConnectionRequired,
    InProcessServerFactory,
    McpConnector,
    McpFailure,
)

PUBLISHER_USER = "publisher"


class _TokenCredentials:
    """Static bearer for discovering an authenticated remote server at publication."""

    def __init__(self, token: str | None) -> None:
        self._token = token

    async def headers(
        self, *, user_id: str, installation_id: str, plugin_id: str, auth: RemoteAuth
    ) -> Mapping[str, str] | None:
        return {"Authorization": f"Bearer {self._token}"} if self._token else None


@dataclass(frozen=True, slots=True)
class Discovered:
    tools: tuple[ServerTool, ...]
    """Every tool the server lists (the router's digest covers all of them)."""
    digest: str
    ui: dict[str, str]
    """Served ``ui://`` resources of the tiles (in-process servers only)."""


class DiscoveryError(Exception):
    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("; ".join(str(problem) for problem in problems))
        self.problems = problems


def overlay_problems(manifest: Mapping[str, Any], tools: tuple[ServerTool, ...]) -> list[Problem]:
    """The manifest's ``tools`` overlay must equal the server's ``tools/list``."""
    served = {tool.name for tool in tools}
    declared = set(manifest["tools"])
    if manifest["server"]["kind"] == "declarative":
        served.discard(APP_ONLY_TOOL)
        declared.discard(APP_ONLY_TOOL)
    problems = [
        Problem("tools_mismatch", f"tools.{name}", "declared in mia.json but not served")
        for name in sorted(declared - served)
    ]
    problems.extend(
        Problem("tools_mismatch", f"tools.{name}", "served but missing from mia.json")
        for name in sorted(served - declared)
    )
    return problems


async def discover(
    package: PluginPackage,
    *,
    connector: McpConnector,
    server_factory: InProcessServerFactory | None,
    plugin_dir: Path | None = None,
) -> Discovered:
    """Connect, list tools, and (in process) read every tile resource."""
    manifest = package.manifest
    try:
        parsed = parse_manifest(manifest)
    except ManifestError as error:
        raise DiscoveryError([Problem("manifest_semantics", "", str(error))]) from error
    record = InstallationRecord(
        id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        plugin_id=parsed.plugin_id,
        version=parsed.version,
        status="active",
        grants=(),
        tools_digest=None,
        manifest=manifest,
    )
    directory = plugin_dir or package.directory
    plugin = InstalledPlugin(installation=record, manifest=parsed, plugin_dir=directory)
    try:
        tools = tuple(await connector.list_tools(plugin, user_id=PUBLISHER_USER))
    except ConnectionRequired as error:
        raise DiscoveryError(
            [Problem("discover_auth_required", "server", "the server needs a token: pass --token")]
        ) from error
    except McpFailure as failure:
        raise DiscoveryError(
            [Problem("discover_failed", "server", f"tools/list failed ({failure.kind})")]
        ) from failure
    if not tools:
        raise DiscoveryError([Problem("tools_mismatch", "tools", "the server lists no tools")])
    problems = overlay_problems(manifest, tools)
    ui: dict[str, str] = {}
    if parsed.server.in_process and manifest.get("surfaces", {}).get("tiles"):
        if server_factory is None or directory is None:
            raise DiscoveryError(
                [
                    Problem(
                        "discover_failed", "server", "in-process plugins need a package directory"
                    )
                ]
            )
        ui, ui_problems = await _read_tiles(manifest, server_factory(manifest, directory))
        problems.extend(ui_problems)
    if problems:
        raise DiscoveryError(problems)
    return Discovered(tools=tools, digest=compute_tools_digest(tools), ui=ui)


async def _read_tiles(
    manifest: Mapping[str, Any], server: Any
) -> tuple[dict[str, str], list[Problem]]:
    ui: dict[str, str] = {}
    problems: list[Problem] = []
    async with Client(server, cache=None) as client:
        for tile in manifest["surfaces"]["tiles"]:
            uri = tile["resourceUri"]
            try:
                result = await client.read_resource(uri)
            except MCPError:
                problems.append(
                    Problem("ui_unreadable", f"tiles.{tile['id']}", f"{uri} is not served")
                )
                continue
            text = "".join(str(getattr(content, "text", "") or "") for content in result.contents)
            ui[uri] = text
            problems.extend(ui_html_problems(text, f"tiles.{tile['id']}", require_host_csp=True))
    return ui, problems


def token_connector(
    token: str | None,
    *,
    server_factory: InProcessServerFactory | None,
    global_allowlist: Iterable[str] | None = None,
    list_timeout_s: float = 8.0,
) -> McpConnector:
    """A connector for publication: in-process factory plus an optional static bearer."""
    return McpConnector(
        server_factory=server_factory,
        credentials=_TokenCredentials(token),
        global_allowlist=global_allowlist,
        list_timeout_s=list_timeout_s,
    )
