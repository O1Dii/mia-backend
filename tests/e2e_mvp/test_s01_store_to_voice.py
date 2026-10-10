"""Scenario 1: the store — publish → install → connect → tile → voice.

The real Linear package (``plugins/linear``, its address pointed at the in-process server with
Linear's recorded tool set) goes through the publishing CLI, the store API, the OAuth
connection (PKCE + resource indicator at a fake provider), the dashboard tile host and a voice
run from the phone. Live Linear is a user check (UC-M40-1, OI-M72-R1-004).
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from cli.mia_plugin import run as plugin_cli

from tests.e2e_mvp.harness import Mvp, call, final, payloads, requires_stack, say, types

pytestmark = requires_stack

LIST = "team.linear.list_issues"


def offered_name(names: set[str], suffix: str) -> str:
    [name] = [n for n in names if n.endswith(suffix)]
    return name


async def test_publish_install_connect_tile_and_voice(
    mvp: Mvp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. The store admin publishes the package with the CLI (discovery needs a server token).
    monkeypatch.setenv("MVP_LINEAR_TOKEN", mvp.auth_server.mint_token())

    async def close() -> None:
        return None

    out = io.StringIO()
    code = await plugin_cli(
        [
            "publish",
            str(mvp.linear_directory(tmp_path)),
            "--as-user",
            mvp.admin_id,
            "--token-env",
            "MVP_LINEAR_TOKEN",
        ],
        out=out,
        settings=mvp.settings,
        service_factory=lambda settings: (mvp.catalog, close),
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 0, lines
    assert lines[-1]["plugin_id"] == "team.linear" and lines[-1]["created"] is True

    # 2. The phone user finds it in the store and installs it after the consent screen.
    phone = await mvp.phone("ann")
    await phone.register_device_tools()
    listed = await phone.get("/v1/plugins")
    assert listed.status_code == 200, listed.text
    assert "team.linear" in {p["plugin_id"] for p in listed.json()["items"]}
    entry = (await phone.get("/v1/plugins/team.linear")).json()
    assert entry["consent"]["requires_connection"] is True
    installed = await mvp.install(phone, "team.linear")
    assert installed["status"] == "needs_connection"
    iid = installed["id"]

    # 3. «Подключить аккаунт»: OAuth at the provider, back to Mia's callback.
    callback = await mvp.connect(phone, iid)
    assert callback.status_code in (200, 302, 303), callback.text
    state = (await phone.get(f"/v1/installations/{iid}")).json()
    assert state["status"] == "active", state
    connections = (await phone.get("/v1/connections")).json()["items"]
    assert [c["installation_id"] for c in connections] == [iid]
    assert all("token" not in key for c in connections for key in c)

    # 4. The tile: the host serves the bundled document with its CSP and bridge; the tile's
    #    refresh tool runs through the router with the connection's token and no model.
    tile = await phone.get(f"/v1/installations/{iid}/tiles/my-issues")
    assert tile.status_code == 200, tile.text
    doc = tile.json()
    assert "default-src 'none'" in doc["html"] and "window.mia" in doc["html"]
    assert doc["refresh"]["tool"] == "list_issues" and "create_comment" not in doc["tools"]
    placed = {
        "id": str(uuid.uuid4()),
        "installation_id": iid,
        "tile_id": "my-issues",
        "size": "2x2",
        "position": 0,
    }
    saved = await phone.put("/v1/dashboard", {"expected_version": 0, "tiles": [placed]})
    assert saved.status_code == 200, saved.text
    model_calls = len(mvp.model.requests)
    refreshed = await phone.post(
        f"/v1/installations/{iid}/tiles/my-issues/tool-calls",
        {"name": "list_issues", "arguments": {"assignee": "me", "limit": 20}},
    )
    assert refreshed.status_code == 200, refreshed.text
    issues = refreshed.json()["structured_content"]["issues"]
    assert [i["identifier"] for i in issues] == ["ENG-101", "ENG-102"]
    assert len(mvp.model.requests) == model_calls  # tiles never ask the model
    assert mvp.remote.mcp_requests[-1]["authorization"].startswith("Bearer at-")

    # The same account in the browser sees the same dashboard (one account, web + Android).
    web = await mvp.browser("ann")
    assert web.user_id == phone.user_id
    dashboard = (await web.get("/v1/dashboard")).json()
    assert [t["tile_id"] for t in dashboard["tiles"]] == ["my-issues"]

    # 5. Voice: the plugin's voice tool is offered to the voice run and called.
    def ask_linear(request: Any) -> Any:
        names = {tool.name for tool in request.tools}
        return call(offered_name(names, ".list_issues"), {"assignee": "me"})

    mvp.model.then(ask_linear, say("У вас две задачи: ENG-101 и ENG-102."))
    events = await phone.run("Какие у меня задачи в Linear?", profile="voice")
    assert types(events)[0] == "run.started" and types(events)[-1] == "run.completed"
    [started] = payloads(events, "tool.call.started")
    assert started["tool"] == LIST
    [completed] = payloads(events, "tool.call.completed")
    assert completed["status"] == "succeeded"
    assert "ENG-101" in final(events)["speech"]
    assert LIST in mvp.model.offered(0)
    # Voice offers only the plugin's voice tools (manifest ``voice: true``).
    linear_offered = {n for n in mvp.model.offered(0) if n.startswith("team.linear.")}
    assert linear_offered == {"team.linear.list_issues", "team.linear.get_issue"}
    # The tool result reached the model framed as untrusted data.
    assert any("ENG-101" in m for m in mvp.model.tool_messages(1))

    # The same events replay over HTTP SSE (what the app reads), contract-valid.
    run_id = events[0]["run_id"]
    assert types(await phone.sse(run_id)) == types(events)
