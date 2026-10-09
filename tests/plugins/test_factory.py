"""The in-process server factory handed to M13's router."""

from __future__ import annotations

import copy
import inspect
from pathlib import Path
from typing import Any

import pytest
from app.modules.plugins.builtin import (
    BuiltinManifestSource,
    UnsupportedServerError,
    build_inprocess_server,
    make_inprocess_factory,
)
from app.modules.plugins.declarative import convert_manifest
from mcp.server.mcpserver import MCPServer

from tests.plugins.support import ROOT, Caller, builtin, connect, load_json, make_services, meta

EXAMPLE = ROOT / "contracts/plugins/v2/examples/focus-board.json"


def test_signatures_match_the_router_contract() -> None:
    factory = make_inprocess_factory(make_services())
    assert list(inspect.signature(factory).parameters) == ["manifest", "plugin_dir"]
    assert list(inspect.signature(build_inprocess_server).parameters) == [
        "manifest",
        "plugin_dir",
        "services",
    ]


async def test_factory_builds_every_builtin_and_the_servers_work() -> None:
    factory = make_inprocess_factory(make_services())
    for plugin in BuiltinManifestSource().list():
        server = factory(plugin.manifest, plugin.directory)
        assert isinstance(server, MCPServer)
        async with connect(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(plugin.manifest["tools"])


async def test_model_calls_timer_through_the_factory_and_in_memory_transport() -> None:
    """The MW2 gate in miniature: a router-like client calls ``timer.start`` over MCP."""
    plugin = builtin("builtin.timer")
    server = make_inprocess_factory(make_services())(plugin.manifest, plugin.directory)
    async with connect(server) as client:
        result = await Caller(client, meta())("start", {"seconds": 300, "label": "чай"})
    assert result["changed"]["label"] == "чай"


async def test_factory_builds_declarative_manifests(tmp_path: Path) -> None:
    converted = convert_manifest(load_json(EXAMPLE))
    server = make_inprocess_factory(make_services())(converted.manifest, tmp_path)
    async with connect(server) as client:
        result = await Caller(client, meta())("set_focus", {"task": "тест"})
    assert result["state"] == {"task": "тест"}


def with_server(plugin_id: str, **server: Any) -> dict[str, Any]:
    manifest = copy.deepcopy(dict(builtin(plugin_id).manifest))
    manifest["server"] = server
    return manifest


@pytest.mark.parametrize(
    "server",
    [
        {"kind": "builtin", "module": "app.modules.workspace.service"},
        {"kind": "builtin", "module": "os"},
        {"kind": "builtin", "module": "app.plugins.builtin.missing.server"},
        {"kind": "builtin", "module": "app.plugins.builtin.notes.server"},  # id mismatch
        {"kind": "builtin"},
        {"kind": "remote", "url": "https://mcp.example.com/mcp", "auth": "none"},
        {"kind": "worker"},
        {},
    ],
)
def test_foreign_or_unsupported_servers_are_refused(server: dict[str, Any]) -> None:
    plugin = builtin("builtin.timer")
    manifest = with_server("builtin.timer", **server)
    with pytest.raises(UnsupportedServerError):
        make_inprocess_factory(make_services())(manifest, plugin.directory)


def test_a_manifest_cannot_import_arbitrary_code() -> None:
    manifest = with_server("builtin.timer", kind="builtin", module="app.core.config")
    with pytest.raises(UnsupportedServerError, match="must be under"):
        build_inprocess_server(manifest, Path("."), services=make_services())
