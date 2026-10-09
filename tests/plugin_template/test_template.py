"""The plugin template: package checks, server and manifest agreement, publication via the CLI.

Publication runs the real ``cli.mia_plugin`` entry point on the catalog test store (PostgreSQL,
``MIA_TEST_DATABASE_URL``). The template's own server stands in for the hosted MCP server, so
no test touches the network.
"""

from __future__ import annotations

import io
import json
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.modules.plugins.catalog.package import load_package
from app.modules.plugins.catalog.service import CatalogService
from app.modules.plugins.catalog.validation import package_problems
from cli.mia_plugin import run
from mcp import Client

from plugins._template.server.plugin_server import build_server
from tests.catalog import remote_world
from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import MCP_HOST, RESOURCE
from tests.plugins.support import ROOT
from tests.runs.conftest import requires_postgres

TEMPLATE = ROOT / "plugins" / "_template"
PLUGIN_ID = "team.plugin-template"


def manifest_of(directory: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((directory / "mia.json").read_text("utf-8"))
    return data


def test_the_manifest_passes_the_publication_checks() -> None:
    assert package_problems(load_package(TEMPLATE)) == []


async def test_the_validate_command_accepts_the_template() -> None:
    out = io.StringIO()
    code = await run(["validate", str(TEMPLATE)], out=out, settings=Settings(app_env="test"))
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 0
    assert lines[-1] == {"plugin_id": PLUGIN_ID, "valid": True, "version": "0.1.0"}


async def test_the_server_lists_exactly_the_tools_of_the_manifest() -> None:
    async with Client(build_server()) as client:
        listed = await client.list_tools()
    assert {tool.name for tool in listed.tools} == set(manifest_of(TEMPLATE)["tools"])


async def test_the_tools_behave_as_the_manifest_describes() -> None:
    server = build_server()
    async with Client(server) as client:
        empty = await client.call_tool("get_status", {})
        added = await client.call_tool("add_note", {"text": "Купить молоко"})
        status = await client.call_tool("get_status", {})
        refused = await client.call_tool("add_note", {"text": "   "})
    assert not empty.is_error and empty.structured_content is not None
    assert empty.structured_content["notes_count"] == 0
    assert not added.is_error and added.structured_content == {"notes_count": 1, "index": 1}
    assert status.structured_content is not None
    assert status.structured_content["notes_count"] == 1
    assert refused.is_error


def test_the_tile_ships_no_external_urls_and_keeps_the_bridge_marker() -> None:
    html = (TEMPLATE / "ui" / "status.html").read_text("utf-8")
    assert "<!--mia:bridge-->" in html
    assert "http://" not in html and "https://" not in html
    assert "innerHTML" not in html


def hosted_directory(tmp_path: Path) -> Path:
    """The template with its server address pointed at the in-process test server."""
    target = tmp_path / "plugin-template"
    shutil.copytree(TEMPLATE, target)
    manifest = manifest_of(target)
    manifest["server"]["url"] = RESOURCE
    manifest["permissions"]["network"] = [MCP_HOST]
    (target / "mia.json").write_text(json.dumps(manifest), "utf-8")
    return target


@pytest.fixture
async def store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    # The template declares ``auth: none``: Mia sends no bearer, so the test server takes none.
    monkeypatch.setattr(remote_world, "build_mcp_server", lambda *_, **__: build_server())
    async with build_store(require_auth=False) as value:
        yield value


@requires_postgres
async def test_mia_plugin_publish_stores_the_template_release(store: Store, tmp_path: Path) -> None:
    async def close() -> None:
        return None

    def factory(_settings: Settings) -> tuple[CatalogService, Callable[[], Awaitable[None]]]:
        return store.catalog, close

    out = io.StringIO()
    code = await run(
        [
            "publish",
            str(hosted_directory(tmp_path)),
            "--as-user",
            store.admin_id,
        ],
        out=out,
        settings=Settings(app_env="test"),
        service_factory=factory,
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 0, lines
    assert lines[-1]["plugin_id"] == PLUGIN_ID and lines[-1]["created"] is True
    release = await store.catalog.release(PLUGIN_ID, "0.1.0")
    assert release.server_kind == "remote"
    assert {tool["name"] for tool in release.tools} == {"get_status", "add_note"}
    assert "tools.write_reversible" in release.grants
    assert f"network:{MCP_HOST}" in release.grants
    assert "ui/status.html" in release.files and "SKILL.md" in release.files
