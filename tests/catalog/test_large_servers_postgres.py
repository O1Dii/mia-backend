"""Servers added by URL with many tools; installations never disappear silently (M15-R1).

``big.test.example`` serves 76 tools like Linear's MCP server did on the test server, where
the stored 76-tool manifest exceeded the old 64-tool limit and the registry skipped the
installation on every run with only a redacted log line.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from typing import Any

import httpx
import pytest
from app.core.logging import JsonFormatter
from app.core.ports.tool_router import ToolCallRequest, ToolContext
from app.modules.plugins.registry import MAX_PLUGIN_TOOLS, PLUGIN_TOOL_BUDGET
from app.modules.workspace.models import Installation
from sqlalchemy import update

from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import BIG_HOST, BIG_URL, big_tool_names
from tests.runs.conftest import requires_postgres

pytestmark = requires_postgres

REGISTRY_LOGGER = "app.modules.plugins.registry.installed"


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    async with build_store() as value:
        yield value


def _json(response: httpx.Response, status: int) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


async def _context(store: Store, user: dict[str, str]) -> ToolContext:
    workspace = _json(await store.get(user, "/v1/workspace"), 200)
    return ToolContext(
        user_id=workspace["owner_user_id"],
        run_id=str(uuid.uuid4()),
        profile="chat",
        workspace_id=workspace["id"],
    )


async def _consent(
    store: Store, user: dict[str, str], iid: str, found: dict[str, Any], tools: list[str] | None
) -> httpx.Response:
    current = _json(await store.get(user, f"/v1/installations/{iid}"), 200)
    body: dict[str, Any] = {
        "expected_version": current["state_version"],
        "tools_digest": found["tools_digest"],
        "grants": [g["id"] for g in found["grants"]],
    }
    if tools is not None:
        body["tools"] = tools
    return await store.post(user, f"/v1/installations/{iid}/consent", body)


async def _plugin_tools(store: Store, ctx: ToolContext, plugin_id: str) -> list[str]:
    listed = await store.router.list_tools(ctx)
    prefix = f"{plugin_id}."
    return [d.spec.name.removeprefix(prefix) for d in listed if d.spec.name.startswith(prefix)]


async def _set_row(store: Store, iid: str, **values: Any) -> None:
    async with store.factory() as session, session.begin():
        await session.execute(
            update(Installation).where(Installation.id == uuid.UUID(iid)).values(**values)
        )


async def test_a_76_tool_server_is_added_picked_consented_and_called(store: Store) -> None:
    user = await store.login("ann")
    ctx = await _context(store, user)
    created = _json(await store.post(user, "/v1/installations", {"custom_mcp_url": BIG_URL}), 201)
    iid, plugin_id = created["id"], created["plugin_id"]
    others = sum(
        1 for d in await store.router.list_tools(ctx) if d.plugin_id and d.plugin_id != plugin_id
    )

    # 1. Probe: every tool is shown; a large server starts without destructive tools.
    found = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    assert found["state"] == "ready" and found["host"] == BIG_HOST
    names = big_tool_names()
    assert [t["name"] for t in found["tools"]] == names
    assert found["tool_limit"] == min(MAX_PLUGIN_TOOLS, PLUGIN_TOOL_BUDGET - others)
    selected = [t["name"] for t in found["tools"] if t["selected"]]
    assert len(selected) == 58
    assert not any(n.startswith(("delete_", "archive_")) for n in selected)
    assert {"list_issues", "get_issue", "create_issue", "update_issue"} <= set(selected)

    # 2. The selection is checked against the server and the limit.
    unknown = _json(await _consent(store, user, iid, found, [*selected, "drop_tables"]), 422)
    assert unknown["error"]["details"]["unknown_tools"] == ["drop_tables"]
    _json(await _consent(store, user, iid, found, []), 422)
    too_many = [f"x_{i}" for i in range(found["tool_limit"] + 1)]
    _json(await _consent(store, user, iid, found, too_many), 422)

    # 3. Consent to the ticked tools (plus one destructive the user wants).
    chosen = [*selected, "delete_comment"]
    active = _json(await _consent(store, user, iid, found, chosen), 200)
    assert active["status"] == "active" and active["tools_digest"] == found["tools_digest"]

    # 4. The chat run lists exactly those tools, and a call works after approval.
    offered = await _plugin_tools(store, ctx, plugin_id)
    assert sorted(offered) == sorted(chosen)
    request = ToolCallRequest("call_1", f"{plugin_id}.list_issues", {"query": "open"})
    pending = await store.router.call(ctx, request)
    assert pending.status == "approval_required" and pending.approval is not None
    await store.tools.approvals.decide(
        user_id=ctx.user_id,
        approval_id=pending.approval.id,
        decision="approve",
        args_hash=pending.approval.args_hash,
        channel="web",
    )
    done = await store.router.call(
        ctx, dataclasses.replace(request, approval_id=pending.approval.id)
    )
    assert done.status == "succeeded"
    assert store.remote.big_calls == [("list_issues", {"query": "open"})]
    # A tool the user left out is not there.
    skipped = await store.router.call(ctx, ToolCallRequest("c2", f"{plugin_id}.delete_issue", {}))
    assert skipped.error_code == "tool_not_found"

    # 5. «Мои плагины» knows it works and how many tools reach the model.
    health = _json(await store.get(user, f"/v1/installations/{iid}/health"), 200)
    assert health["state"] == "ok" and health["problem"] is None
    assert health["tools"] == health["declared_tools"] == len(chosen)

    # 6. Re-picking starts from the current choice.
    again = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    assert sorted(t["name"] for t in again["tools"] if t["selected"]) == sorted(chosen)


async def test_the_test_server_linear_installation_works_after_deploy(store: Store) -> None:
    """A custom installation consented to all 76 tools before M15-R1 loads as is."""
    user = await store.login("bob")
    ctx = await _context(store, user)
    created = _json(await store.post(user, "/v1/installations", {"custom_mcp_url": BIG_URL}), 201)
    iid, plugin_id = created["id"], created["plugin_id"]
    found = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    _json(await _consent(store, user, iid, found, None), 200)  # no selection: every usable tool
    offered = await _plugin_tools(store, ctx, plugin_id)
    others = sum(1 for d in await store.router.list_tools(ctx) if d.plugin_id != plugin_id)
    assert len(offered) == 76, others
    health = _json(await store.get(user, f"/v1/installations/{iid}/health"), 200)
    assert health["state"] == "ok" and health["tools"] == 76


async def test_an_unloadable_installation_is_reported_not_dropped(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    user = await store.login("cat")
    ctx = await _context(store, user)
    created = _json(await store.post(user, "/v1/installations", {"custom_mcp_url": BIG_URL}), 201)
    iid, plugin_id = created["id"], created["plugin_id"]
    found = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    active = _json(await _consent(store, user, iid, found, ["list_issues", "get_issue"]), 200)

    # The tools of the server changed after consent: visible, not silent.
    await _set_row(store, iid, tools_digest="sha256:" + "0" * 64)
    changed = _json(await store.get(user, f"/v1/installations/{iid}/health"), 200)
    assert (changed["state"], changed["problem"]) == ("needs_attention", "tools_changed")
    await _set_row(store, iid, tools_digest=active["tools_digest"])

    # A stored manifest the registry cannot load (here: over the limit).
    async with store.factory() as session:
        row = await session.get(Installation, uuid.UUID(iid))
        assert row is not None and row.manifest is not None
        manifest = dict(row.manifest)
    manifest["tools"] = {f"list_x_{i:03d}": {"risk": "write_external"} for i in range(100)}
    manifest["tools"]["list_issues"] = {"risk": "write_external"}
    await _set_row(store, iid, manifest=manifest)

    logger = logging.getLogger(REGISTRY_LOGGER)
    disabled, logger.disabled = logger.disabled, False
    try:
        with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
            assert await _plugin_tools(store, ctx, plugin_id) == []
            # A call by name gets a clear refusal instead of «unknown tool».
            refused = await store.router.call(
                ctx, ToolCallRequest("c1", f"{plugin_id}.list_issues", {})
            )
    finally:
        logger.disabled = disabled
    assert refused.status == "failed" and refused.error_code == "tool_not_active"
    health = _json(await store.get(user, f"/v1/installations/{iid}/health"), 200)
    assert health["state"] == "needs_attention"
    assert (health["problem"], health["reason"]) == ("manifest_invalid", "too_many_tools")
    # Still listed in «Мои плагины» with its stored status.
    listed = _json(await store.get(user, "/v1/installations"), 200)["items"]
    assert iid in {item["id"] for item in listed}

    # The log line names the event and the plugin.
    record = next(r for r in caplog.records if r.msg == "plugin_manifest_invalid")
    line = json.loads(JsonFormatter().format(record))
    assert line["event"] == "plugin_manifest_invalid"
    assert (line["plugin_id"], line["reason"]) == (plugin_id, "too_many_tools")
    assert line["installation_id"] == iid

    # Re-picking the tools repairs it.
    found = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    _json(await _consent(store, user, iid, found, ["list_issues"]), 200)
    assert await _plugin_tools(store, ctx, plugin_id) == ["list_issues"]


async def test_two_large_servers_share_the_plugin_tool_budget(store: Store) -> None:
    user = await store.login("dan")
    ctx = await _context(store, user)
    first = _json(await store.post(user, "/v1/installations", {"custom_mcp_url": BIG_URL}), 201)
    found = _json(await store.get(user, f"/v1/installations/{first['id']}/discovery"), 200)
    _json(await _consent(store, user, first["id"], found, None), 200)

    second_url = f"{BIG_URL}?workspace=b"
    second = _json(await store.post(user, "/v1/installations", {"custom_mcp_url": second_url}), 201)
    iid = second["id"]
    found = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    limit = found["tool_limit"]
    assert 0 < limit < 76 and sum(t["selected"] for t in found["tools"]) == limit
    refused = _json(await _consent(store, user, iid, found, None), 422)
    assert refused["error"]["details"]["tool_limit"] == limit
    _json(await _consent(store, user, iid, found, ["list_issues"]), 200)
    assert await _plugin_tools(store, ctx, second["plugin_id"]) == ["list_issues"]

    # Consented to all 76 before the budget existed: trimmed visibly, reads kept first.
    async with store.factory() as session:
        row = await session.get(Installation, uuid.UUID(first["id"]))
        assert row is not None and row.manifest is not None
        full = dict(row.manifest)
    legacy = dict(full, id=second["plugin_id"])
    legacy["server"] = dict(full["server"], url=second_url)
    await _set_row(store, iid, manifest=legacy)
    offered = await _plugin_tools(store, ctx, second["plugin_id"])
    assert len(offered) == limit and "list_issues" in offered
    assert not any(name.startswith("delete_") for name in offered)
    health = _json(await store.get(user, f"/v1/installations/{iid}/health"), 200)
    assert (health["state"], health["problem"]) == ("limited", "tool_budget")
    assert (health["tools"], health["declared_tools"]) == (limit, 76)
