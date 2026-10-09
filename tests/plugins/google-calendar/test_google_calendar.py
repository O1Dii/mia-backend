"""Google Calendar plugin: package checks, tile on recorded results, server on recorded Google
responses, and publication through the store.

Nothing touches the network: Google answers come from recorded fixtures through
``httpx.MockTransport``; the hosted MCP server is stood in by an in-process recording server.
The live Google OAuth smoke is a user check (see the M42 handoff).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.core.ports.tool_router import ToolCallRequest
from app.modules.plugins.catalog.errors import PackageInvalidError
from app.modules.plugins.catalog.package import PluginPackage, load_package
from app.modules.plugins.catalog.validation import package_problems
from mcp import Client
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent

from tests.catalog import remote_world
from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import MCP_HOST, RESOURCE
from tests.catalog.test_connections_postgres import connect_account, context
from tests.catalog.test_install_postgres import install
from tests.plugins.support import ROOT
from tests.runs.conftest import requires_postgres

PLUGIN = ROOT / "plugins" / "google-calendar"
FIXTURES = Path(__file__).parent / "fixtures"
NODE = shutil.which("node")


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))  # type: ignore[no-any-return]


def load_server_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "calendar_mcp", PLUGIN / "server" / "calendar_mcp.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calendar_mcp = load_server_module()
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("UTC"))


# ---- package ---------------------------------------------------------------------------------


def test_manifest_matches_the_server_tool_set() -> None:
    manifest = json.loads((PLUGIN / "mia.json").read_text("utf-8"))
    assert set(manifest["tools"]) == set(fixture("tools_list")["tools"])
    assert manifest["server"]["kind"] == "remote" and manifest["server"]["auth"] == "oauth"
    assert {spec["risk"] for spec in manifest["tools"].values()} == {"read"}
    assert all(spec.get("voice") for spec in manifest["tools"].values())
    tile = manifest["surfaces"]["tiles"][0]
    assert tile["title"] == "Сегодня" and tile["refresh"]["tool"] == "today_events"
    assert tile["bundled"] == "ui/today.html"
    host = manifest["server"]["url"].split("/")[2]
    assert manifest["permissions"]["network"] == [host]


def test_the_package_passes_publication_checks() -> None:
    assert package_problems(load_package(PLUGIN)) == []


def test_the_package_stores_no_server_code() -> None:
    assert not any(path.endswith(".py") for path in load_package(PLUGIN).files)


def test_the_tile_ships_no_external_urls_or_innerhtml() -> None:
    html = (PLUGIN / "ui" / "today.html").read_text("utf-8")
    assert "http://" not in html and "https://" not in html and "innerHTML" not in html


# ---- tile ------------------------------------------------------------------------------------

TILE_HARNESS = (Path(__file__).parent / "tile_harness.js").read_text("utf-8")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_tile(tmp_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    (tmp_path / "h.js").write_text(TILE_HARNESS, "utf-8")
    (tmp_path / "p.json").write_text(json.dumps(payload), "utf-8")
    assert NODE is not None
    done = subprocess.run(
        [NODE, str(tmp_path / "h.js"), str(PLUGIN / "ui" / "today.html"), str(tmp_path / "p.json")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)  # type: ignore[no-any-return]


@needs_node
def test_the_tile_renders_the_recorded_day(tmp_path: Path) -> None:
    out = run_tile(tmp_path, fixture("today_events"))
    assert out["calls"][0][0] == "today_events" and "timezone" in out["calls"][0][1]
    assert out["rows"] == [
        ["весь день", "День рождения Ани", ""],
        ["09:00", "(без названия)", ""],
        ["15:30", "Планёрка", "Переговорная 2"],
    ]
    assert out["meta"].endswith("3") and out["empty"] == ""


@needs_node
def test_the_tile_handles_an_empty_day_and_an_error(tmp_path: Path) -> None:
    empty = run_tile(tmp_path, fixture("today_events_empty"))
    assert empty["rows"] == [] and empty["empty"] == "Сегодня событий нет"
    failed = run_tile(tmp_path, {"fail": True})
    assert failed["error"] == "boom" and failed["rows"] == []


# ---- server on recorded Google responses -----------------------------------------------------


class Google:
    """Recorded Calendar API: remembers requests, answers with one recorded body."""

    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self.body, self.status = body, status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


async def call(
    google: Google, tool: str, arguments: dict[str, Any], *, token: str = "ya29.test"
) -> CallToolResult:
    http = httpx.AsyncClient(transport=httpx.MockTransport(google))
    server = calendar_mcp.build_server(http=http, token_source=lambda ctx: token, now=lambda: NOW)
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)


async def test_today_events_normalizes_sorts_and_drops_cancelled() -> None:
    google = Google(fixture("google_events_today"))
    result = await call(google, "today_events", {"timezone": "Europe/Warsaw"})
    assert not result.is_error
    assert result.structured_content == fixture("today_events")["structuredContent"]
    request = google.requests[0]
    assert request.headers["authorization"] == "Bearer ya29.test"
    assert request.url.path == "/calendar/v3/calendars/primary/events"
    query = dict(request.url.params)
    assert query["singleEvents"] == "true" and query["orderBy"] == "startTime"
    assert query["timeMin"] == "2026-10-09T00:00:00+02:00"
    assert query["timeMax"] == "2026-10-10T00:00:00+02:00"
    assert request.method == "GET" and len(google.requests) == 1


async def test_today_uses_the_users_day_not_utc() -> None:
    google = Google(fixture("google_events_empty"))
    late = datetime(2026, 10, 9, 23, 30, tzinfo=ZoneInfo("UTC"))  # already 10 Oct in Warsaw
    http = httpx.AsyncClient(transport=httpx.MockTransport(google))
    server = calendar_mcp.build_server(http=http, token_source=lambda c: "t", now=lambda: late)
    async with Client(server) as client:
        result = await client.call_tool("today_events", {"timezone": "Europe/Warsaw"})
    assert result.structured_content["date"] == "2026-10-10"
    assert google.requests[0].url.params["timeMin"] == "2026-10-10T00:00:00+02:00"


async def test_empty_day_and_calendar_id_is_url_encoded() -> None:
    google = Google(fixture("google_events_empty"))
    result = await call(google, "today_events", {"calendar_id": "ann@example.test"})
    assert result.structured_content["events"] == []
    assert google.requests[0].url.raw_path.startswith(
        b"/calendar/v3/calendars/ann%40example.test/events"
    )


async def test_list_events_validates_the_range() -> None:
    google = Google(fixture("google_events_today"))
    ok = await call(
        google,
        "list_events",
        {"time_min": "2026-10-09T00:00:00+02:00", "time_max": "2026-10-16T00:00:00+02:00"},
    )
    assert not ok.is_error and len(ok.structured_content["events"]) == 3
    for lo, hi in [
        ("2026-10-09T00:00:00", "2026-10-10T00:00:00+02:00"),
        ("2026-10-10T00:00:00+02:00", "2026-10-09T00:00:00+02:00"),
        ("2026-10-01T00:00:00+02:00", "2026-12-01T00:00:00+02:00"),
        ("nope", "2026-10-10T00:00:00+02:00"),
    ]:
        bad = await call(google, "list_events", {"time_min": lo, "time_max": hi})
        assert bad.is_error, (lo, hi)
    assert len(google.requests) == 1  # invalid input never reaches Google


@pytest.mark.parametrize(
    ("status", "needle"), [(401, "переподключи"), (403, "доступ"), (404, "не найден"), (500, "500")]
)
async def test_google_errors_become_tool_errors_without_leaking_the_token(
    status: int, needle: str
) -> None:
    result = await call(Google(fixture("google_error_401"), status), "today_events", {})
    assert result.is_error
    text = " ".join(c.text for c in result.content if isinstance(c, TextContent))
    assert needle in text and "ya29" not in text


async def test_unknown_timezone_and_missing_bearer() -> None:
    google = Google(fixture("google_events_empty"))
    assert (await call(google, "today_events", {"timezone": "Mars/Base"})).is_error
    server = calendar_mcp.build_server(
        http=httpx.AsyncClient(transport=httpx.MockTransport(google))
    )
    async with Client(server) as client:  # in-memory transport: no HTTP request, so no bearer
        result = await client.call_tool("today_events", {})
    assert result.is_error and not google.requests


def test_the_server_only_reads_google() -> None:
    source = (PLUGIN / "server" / "calendar_mcp.py").read_text("utf-8")
    assert "calendar.events.readonly" in source
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in source


# ---- store: publish, install, connect, call --------------------------------------------------


def recorded_calendar(calls: list[tuple[str, dict[str, Any]]], **_: Any) -> MCPServer:
    server = MCPServer("calendar-recording", version="1.0.0")

    def make(name: str):  # type: ignore[no-untyped-def]
        async def handler(ctx: Context[Any, Any], query: str = "") -> CallToolResult:
            calls.append((name, {"query": query}))
            body = fixture("today_events")["structuredContent"]
            return CallToolResult(
                content=[TextContent(text=json.dumps(body))], structured_content=body
            )

        return handler

    for name in fixture("tools_list")["tools"]:
        server.add_tool(make(name), name=name, description=f"Calendar {name}")
    return server


@pytest.fixture
async def store(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(remote_world, "build_mcp_server", recorded_calendar)
    async with build_store() as value:
        yield value


def hosted_package(tmp_path: Path) -> PluginPackage:
    target = tmp_path / "google-calendar"
    shutil.copytree(PLUGIN, target)
    manifest = json.loads((target / "mia.json").read_text("utf-8"))
    manifest["server"]["url"] = RESOURCE
    manifest["permissions"]["network"] = [MCP_HOST]
    (target / "mia.json").write_text(json.dumps(manifest), "utf-8")
    return load_package(target)


@requires_postgres
async def test_publish_install_connect_and_call_on_recorded_results(
    store: Store, tmp_path: Path
) -> None:
    published = await store.publish(hosted_package(tmp_path), token=store.auth_server.mint_token())
    release = published.release
    assert published.created and release.plugin_id == "team.google-calendar"
    assert {t["name"] for t in release.tools} == {"today_events", "list_events"}
    assert "tools.read" in release.grants and "tools.write_external" not in release.grants
    assert "tools.write_reversible" not in release.grants

    user = await store.login("ann")
    response = await install(store, user, "team.google-calendar")
    assert response.status_code == 201, response.text
    installed = response.json()
    assert installed["status"] == "needs_connection"
    await connect_account(store, user, installed["id"])

    ctx = await context(store, user)
    result = await store.router.call(
        ctx, ToolCallRequest("c1", "team.google-calendar.today_events", {})
    )
    assert result.status == "succeeded"
    assert result.structured_content == fixture("today_events")["structuredContent"]
    assert store.remote.mcp_requests[-1]["authorization"].startswith("Bearer at-")


@requires_postgres
async def test_publication_rejects_a_tool_set_that_drifted_from_the_manifest(
    store: Store, tmp_path: Path
) -> None:
    package = hosted_package(tmp_path)
    manifest = copy.deepcopy(package.manifest)
    manifest["tools"]["create_event"] = {"risk": "write_external"}
    drifted = PluginPackage(
        manifest=manifest, files=dict(package.files), directory=package.directory
    )
    with pytest.raises(PackageInvalidError) as failure:
        await store.publish(drifted, token=store.auth_server.mint_token())
    assert {p["code"] for p in failure.value.problems} == {"tools_mismatch"}
