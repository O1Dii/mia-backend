"""M31: the MCP Apps host endpoints for dashboard tiles (document + policed tool proxy)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.api.v1.tiles import install_tiles
from app.modules.auth.composition import request_authenticator
from app.modules.plugins.catalog.package import load_package

from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import MCP_HOST, RESOURCE
from tests.catalog.test_connections_postgres import connect_account
from tests.catalog.test_install_postgres import install
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres

TILE_HTML = (
    "<!doctype html><html><head><title>Issues</title><!--mia:bridge--></head>"
    '<body><div id="x">issues</div><script>window.mia.ready.then(function(){});</script>'
    "</body></html>"
)


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    async with build_store(camel_case_tool=False) as value:
        install_tiles(
            value.app,
            registry=value.tools.registry,
            connector=value.tools.connector,
            router=value.router,
            authenticator=request_authenticator(value.app),
        )
        yield value


def _json(response: httpx.Response, status: int) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def tracker(tmp: Path) -> Path:
    manifest = {
        "schemaVersion": "mia.plugin/1",
        "id": "team.tracker",
        "version": "1.0.0",
        "name": "Tracker",
        "listing": {"description": "Issues from the test tracker", "category": "work"},
        "server": {"kind": "remote", "url": RESOURCE, "auth": "oauth"},
        "skills": ["SKILL.md"],
        "tools": {
            "list_issues": {"risk": "read", "voice": True},
            "create_issue": {"risk": "write_external"},
        },
        "surfaces": {
            "tiles": [
                {
                    "id": "issues",
                    "title": "Issues",
                    "resourceUri": "ui://team.tracker/issues",
                    "bundled": "ui/issues.html",
                    "sizes": ["2x2", "4x2"],
                    "refresh": {"tool": "list_issues", "everySeconds": 60},
                }
            ]
        },
        "permissions": {
            "network": [MCP_HOST],
            "llm": {"viaHost": False},
            "memory": [],
            "notify": False,
        },
    }
    package = tmp / "tracker"
    (package / "ui").mkdir(parents=True)
    (package / "mia.json").write_text(json.dumps(manifest), "utf-8")
    (package / "SKILL.md").write_text("---\ndescription: Issues\n---\nUse it.\n", "utf-8")
    (package / "ui" / "issues.html").write_text(TILE_HTML, "utf-8")
    return package


async def installed_timer(store: Store, user: dict[str, str]) -> str:
    await store.publish_builtin("timer")
    created = _json(await install(store, user, "builtin.timer"), 201)
    assert created["status"] == "active"
    return str(created["id"])


async def test_a_timer_tile_is_served_and_its_tools_run_through_the_router(store: Store) -> None:
    user = await store.login("ann")
    installation = await installed_timer(store, user)
    base = f"/v1/installations/{installation}/tiles/active-timers"

    tile = _json(await store.get(user, base), 200)
    assert (
        tile["plugin_id"] == "builtin.timer" and tile["resource_uri"] == "ui://builtin.timer/tile"
    )
    assert tile["refresh"] == {"tool": "get", "every_seconds": 30}
    assert tile["sizes"] == ["1x1", "2x1"] and tile["mobile"] == "card"
    assert {"start", "get", "pause", "resume", "cancel"} <= set(tile["tools"])
    assert "Content-Security-Policy" in tile["html"] and "window.mia" in tile["html"]
    assert "connect-src 'none'" in tile["csp"]

    started = _json(
        await store.post(
            user, f"{base}/tool-calls", {"name": "start", "arguments": {"seconds": 90}}
        ),
        200,
    )
    assert started["status"] == "succeeded" and not started["is_error"]
    assert "<tool_result" not in started["content"]
    items = started["structured_content"]["items"]
    assert len(items) == 1 and items[0]["status"] == "running"

    # The qualified name works too; the timer state is the plugin's, no model involved.
    got = _json(await store.post(user, f"{base}/tool-calls", {"name": "builtin.timer.get"}), 200)
    assert [i["id"] for i in got["structured_content"]["items"]] == [items[0]["id"]]


async def test_a_tile_reaches_only_its_own_tools_and_only_its_owner(store: Store) -> None:
    ann = await store.login("ann")
    bob = await store.login("bob")
    installation = await installed_timer(store, ann)
    base = f"/v1/installations/{installation}/tiles/active-timers"

    assert (
        _json(await store.post(ann, f"{base}/tool-calls", {"name": "other.plugin.run"}), 404)[
            "error"
        ]["code"]
        == "tool_not_found"
    )
    assert (
        _json(await store.post(ann, f"{base}/tool-calls", {"name": "memory.remember"}), 404)[
            "error"
        ]["code"]
        == "tool_not_found"
    )
    assert (
        _json(await store.get(ann, f"/v1/installations/{installation}/tiles/nope"), 404)["error"][
            "code"
        ]
        == "resource_not_found"
    )
    # Bad arguments are refused by the router's schema check, not run.
    bad = _json(
        await store.post(
            ann, f"{base}/tool-calls", {"name": "start", "arguments": {"seconds": -5}}
        ),
        200,
    )
    assert bad["status"] == "failed" and bad["error_code"] == "tool_invalid_arguments"
    # Another user's installation does not exist for Bob.
    assert _json(await store.get(bob, base), 404)["error"]["code"] == "resource_not_found"
    assert (
        _json(await store.post(bob, f"{base}/tool-calls", {"name": "get"}), 404)["error"]["code"]
        == "resource_not_found"
    )
    assert (await store.client.get(base)).status_code == 401


async def test_a_bundled_tile_of_a_remote_plugin_needs_the_account_and_never_writes_externally(
    store: Store, tmp_path: Path
) -> None:
    published = await store.publish(
        load_package(tracker(tmp_path)), token=store.auth_server.mint_token()
    )
    assert published.created
    user = await store.login("ann")
    installed = _json(await install(store, user, "team.tracker"), 201)
    base = f"/v1/installations/{installed['id']}/tiles/issues"
    assert _json(await store.get(user, base), 409)["error"]["code"] == "connection_required"

    await connect_account(store, user, installed["id"])
    tile = _json(await store.get(user, base), 200)
    # The bundled HTML gets the host CSP and the bridge; write_external tools are not offered.
    assert "default-src 'none'" in tile["html"] and "window.mia" in tile["html"]
    assert tile["tools"] == ["list_issues"] and tile["refresh"]["tool"] == "list_issues"

    listed = _json(await store.post(user, f"{base}/tool-calls", {"name": "list_issues"}), 200)
    assert listed["structured_content"] == {"issues": [{"id": "A-1"}, {"id": "A-2"}]}
    writes_before = [c for c in store.remote.calls if c[0] == "create_issue"]
    refused = _json(
        await store.post(
            user, f"{base}/tool-calls", {"name": "create_issue", "arguments": {"title": "x"}}
        ),
        403,
    )
    assert refused["error"]["code"] == "tool_permission_denied"
    assert [c for c in store.remote.calls if c[0] == "create_issue"] == writes_before
