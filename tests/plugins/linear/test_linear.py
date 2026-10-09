"""Linear plugin: package checks, tile behaviour on recorded results, publication and tool calls.

The recorded fixtures stand in for the hosted Linear MCP server; no test touches the network.
The tool names are the hosted server's documented set as far as it could be verified (see
the M40 handoff): a live ``validate --discover`` against Linear is the user check.
"""

from __future__ import annotations

import copy
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.ports.tool_router import ToolCallRequest
from app.modules.plugins.catalog.errors import PackageInvalidError
from app.modules.plugins.catalog.package import PluginPackage, load_package
from app.modules.plugins.catalog.validation import package_problems
from cli.mia_plugin import run
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent

from tests.catalog import remote_world
from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import MCP_HOST, RESOURCE
from tests.catalog.test_connections_postgres import connect_account, context
from tests.catalog.test_install_postgres import install
from tests.plugins.support import ROOT
from tests.runs.conftest import requires_postgres

PLUGIN = ROOT / "plugins" / "linear"
FIXTURES = Path(__file__).parent / "fixtures"
NODE = shutil.which("node")


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))  # type: ignore[no-any-return]


def recorded_linear(calls: list[tuple[str, dict[str, Any]]], **_: Any) -> MCPServer:
    """A server that exposes the recorded tool set and answers with recorded results."""
    server = MCPServer("linear-recording", version="1.0.0")
    answers = {
        name: fixture(name)["structuredContent"]
        for name in ("list_issues", "get_issue", "create_issue")
    }

    def make(name: str):  # type: ignore[no-untyped-def]
        async def handler(ctx: Context[Any, Any], query: str = "") -> CallToolResult:
            calls.append((name, {"query": query}))
            body = answers.get(name, {})
            return CallToolResult(
                content=[TextContent(text=json.dumps(body))], structured_content=body
            )

        return handler

    for name in fixture("tools_list")["tools"]:
        server.add_tool(make(name), name=name, description=f"Linear {name}")
    return server


def test_manifest_matches_the_recorded_tool_set() -> None:
    manifest = json.loads((PLUGIN / "mia.json").read_text("utf-8"))
    assert set(manifest["tools"]) == set(fixture("tools_list")["tools"])
    assert manifest["server"] == {
        "kind": "remote",
        "url": "https://mcp.linear.app/mcp",
        "auth": "oauth",
    }
    assert manifest["permissions"]["network"] == ["mcp.linear.app"]
    risks = {name: spec["risk"] for name, spec in manifest["tools"].items()}
    assert risks["list_issues"] == risks["get_issue"] == "read"
    assert risks["create_issue"] == risks["update_issue"] == "write_reversible"
    assert risks["create_comment"] == "write_external"
    voice = {name for name, spec in manifest["tools"].items() if spec.get("voice")}
    assert voice == {"list_issues", "get_issue"}
    tile = manifest["surfaces"]["tiles"][0]
    assert tile["refresh"]["tool"] == "list_issues" and tile["bundled"] == "ui/my-issues.html"


def test_the_package_passes_publication_checks() -> None:
    assert package_problems(load_package(PLUGIN)) == []


def test_the_package_ships_no_external_urls_in_the_tile() -> None:
    html = (PLUGIN / "ui" / "my-issues.html").read_text("utf-8")
    assert "http://" not in html and "https://" not in html
    assert "innerHTML" not in html


TILE_HARNESS = (FIXTURES.parent / "tile_harness.js").read_text("utf-8")


def run_tile(tmp_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    (tmp_path / "h.js").write_text(TILE_HARNESS, "utf-8")
    (tmp_path / "p.json").write_text(json.dumps(payload), "utf-8")
    assert NODE is not None
    done = subprocess.run(
        [
            NODE,
            str(tmp_path / "h.js"),
            str(PLUGIN / "ui" / "my-issues.html"),
            str(tmp_path / "p.json"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)  # type: ignore[no-any-return]


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_the_tile_renders_the_recorded_list(tmp_path: Path) -> None:
    out = run_tile(tmp_path, fixture("list_issues"))
    assert out["calls"] == [["list_issues", {"assignee": "me", "limit": 20}]]
    assert out["rows"] == [
        ["ENG-101", "Fix login redirect", "In Progress"],
        ["ENG-102", "Write release notes", "Todo"],
    ]
    assert out["meta"].endswith("2") and out["empty"] == ""


@needs_node
def test_the_tile_handles_an_empty_list_and_an_error(tmp_path: Path) -> None:
    empty = run_tile(tmp_path, fixture("list_issues_empty"))
    assert empty["rows"] == [] and empty["empty"] == "Нет открытых задач"
    failed = run_tile(tmp_path, {"fail": True})
    assert failed["error"] == "boom" and failed["rows"] == []


@pytest.fixture
async def store(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(remote_world, "build_mcp_server", recorded_linear)
    async with build_store() as value:
        yield value


def hosted_package(tmp_path: Path) -> PluginPackage:
    """The real package with only the server address pointed at the in-process test server."""
    return load_package(hosted_directory(tmp_path))


def hosted_directory(tmp_path: Path) -> Path:
    target = tmp_path / "linear"
    shutil.copytree(PLUGIN, target)
    manifest = json.loads((target / "mia.json").read_text("utf-8"))
    manifest["server"]["url"] = RESOURCE
    manifest["permissions"]["network"] = [MCP_HOST]
    (target / "mia.json").write_text(json.dumps(manifest), "utf-8")
    return target


@requires_postgres
async def test_publish_install_connect_and_call_on_recorded_results(
    store: Store, tmp_path: Path
) -> None:
    package = hosted_package(tmp_path)
    published = await store.publish(package, token=store.auth_server.mint_token())
    release = published.release
    assert published.created and release.plugin_id == "team.linear"
    assert {t["name"] for t in release.tools} == set(fixture("tools_list")["tools"])
    assert "tools.write_external" in release.grants and f"network:{MCP_HOST}" in release.grants
    assert any(
        tile["bundled"] == "ui/my-issues.html" for tile in release.manifest["surfaces"]["tiles"]
    )

    user = await store.login("ann")
    response = await install(store, user, "team.linear")
    assert response.status_code == 201, response.text
    installed = response.json()
    assert installed["status"] == "needs_connection"
    await connect_account(store, user, installed["id"])

    ctx = await context(store, user)
    listed = await store.router.call(ctx, ToolCallRequest("c1", "team.linear.list_issues", {}))
    assert listed.status == "succeeded"
    assert listed.structured_content == fixture("list_issues")["structuredContent"]
    assert store.remote.mcp_requests[-1]["authorization"].startswith("Bearer at-")
    got = await store.router.call(ctx, ToolCallRequest("c2", "team.linear.get_issue", {}))
    assert got.structured_content == fixture("get_issue")["structuredContent"]


@requires_postgres
async def test_publication_rejects_a_tool_set_that_drifted_from_the_manifest(
    store: Store, tmp_path: Path
) -> None:
    package = hosted_package(tmp_path)
    manifest = copy.deepcopy(package.manifest)
    manifest["tools"]["delete_issue"] = {"risk": "write_external"}
    drifted = PluginPackage(
        manifest=manifest, files=dict(package.files), directory=package.directory
    )
    with pytest.raises(PackageInvalidError) as failure:
        await store.publish(drifted, token=store.auth_server.mint_token())
    assert {p["code"] for p in failure.value.problems} == {"tools_mismatch"}


@requires_postgres
async def test_the_publish_cli_stores_the_release(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_TEST_TOKEN", store.auth_server.mint_token())

    async def close() -> None:
        return None

    out = io.StringIO()
    code = await run(
        [
            "publish",
            str(hosted_directory(tmp_path)),
            "--as-user",
            store.admin_id,
            "--token-env",
            "LINEAR_TEST_TOKEN",
        ],
        out=out,
        settings=Settings(app_env="test"),
        service_factory=lambda settings: (store.catalog, close),
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 0, lines
    assert lines[-1]["plugin_id"] == "team.linear" and lines[-1]["created"] is True
    release = await store.catalog.release("team.linear", "0.1.0")
    assert release.server_kind == "remote"
