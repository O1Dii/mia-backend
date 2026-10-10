"""M72-R1: servers added by URL before M72 get their risks from the server's annotations.

``big.test.example`` serves 76 tools like Linear's MCP server: ``list_*``/``get_*``/``search_*``
say ``readOnlyHint``, ``delete_*``/``archive_*`` say ``destructiveHint``, the rest says nothing.
An installation "as before M72" stores every tool as ``write_external`` with no ``riskSource``.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import httpx
import pytest
from app.core.ports.tool_router import ToolCallRequest, ToolContext
from app.modules.plugins.registry import rederive_risks, risk_from_annotations
from app.modules.workspace.models import Installation
from sqlalchemy import update

from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import BIG_URL, big_tool_names
from tests.runs.conftest import requires_postgres

READ = {"readOnlyHint": True}
DESTRUCTIVE = {"destructiveHint": True}


# ------------------------------------------------------------------ pure


def test_risk_from_annotations() -> None:
    assert risk_from_annotations(READ) == ("read", "annotation")
    assert risk_from_annotations({"readOnlyHint": True, "destructiveHint": True})[0] == "read"
    assert risk_from_annotations({"destructiveHint": False}) == ("write_reversible", "annotation")
    assert risk_from_annotations(DESTRUCTIVE) == ("write_external", "annotation")
    assert risk_from_annotations(None) == ("write_external", "default")
    assert risk_from_annotations({"readOnlyHint": False}) == ("write_external", "default")


def _manifest(**tools: dict[str, str]) -> dict[str, Any]:
    return {"id": "custom.x", "tools": dict(tools)}


def test_rederive_unmarked_tools_follow_the_annotations_up_and_down() -> None:
    stored = _manifest(
        list_issues={"risk": "write_external"},
        create_issue={"risk": "write_external"},
        sweep={"risk": "read"},
    )
    served = {"list_issues": READ, "create_issue": None, "sweep": DESTRUCTIVE, "other": READ}
    upgraded = rederive_risks(stored, served)
    assert upgraded is not None
    tools = upgraded["tools"]
    assert tools["list_issues"] == {"risk": "read", "riskSource": "annotation"}
    assert tools["create_issue"] == {"risk": "write_external", "riskSource": "default"}
    assert tools["sweep"] == {"risk": "write_external", "riskSource": "annotation"}
    assert stored["tools"]["list_issues"] == {"risk": "write_external"}  # input untouched


def test_rederive_leaves_a_users_choice_and_unserved_tools_alone() -> None:
    stored = _manifest(
        a={"risk": "write_external", "riskSource": "user"},
        gone={"risk": "write_external"},
    )
    assert rederive_risks(stored, {"a": READ}) is None


def test_rederive_marked_tools_only_move_up_and_settle() -> None:
    stored = _manifest(
        a={"risk": "read", "riskSource": "annotation"},
        b={"risk": "write_external", "riskSource": "default"},
        c={"risk": "write_reversible", "riskSource": "annotation"},
    )
    # The server later calls `a` destructive (asks more) and `b` harmless (no change).
    upgraded = rederive_risks(
        stored, {"a": DESTRUCTIVE, "b": READ, "c": {"destructiveHint": False}}
    )
    assert upgraded is not None
    assert upgraded["tools"]["a"] == {"risk": "write_external", "riskSource": "annotation"}
    assert upgraded["tools"]["b"] == stored["tools"]["b"]
    assert upgraded["tools"]["c"] == stored["tools"]["c"]
    assert rederive_risks(upgraded, {"a": DESTRUCTIVE, "b": READ}) is None


# -------------------------------------------------- 76 tools (Postgres)


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


async def _add(store: Store, user: dict[str, str]) -> tuple[str, str, dict[str, Any]]:
    created = _json(await store.post(user, "/v1/installations", {"custom_mcp_url": BIG_URL}), 201)
    found = _json(await store.get(user, f"/v1/installations/{created['id']}/discovery"), 200)
    return created["id"], created["plugin_id"], found


async def _consent(
    store: Store, user: dict[str, str], iid: str, found: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    current = _json(await store.get(user, f"/v1/installations/{iid}"), 200)
    body = {
        "expected_version": current["state_version"],
        "tools_digest": found["tools_digest"],
        "grants": [g["id"] for g in found["grants"]] + [g["id"] for g in found["risk_grants"]],
        **extra,
    }
    done: dict[str, Any] = _json(
        await store.post(user, f"/v1/installations/{iid}/consent", body), 200
    )
    return done


async def _manifest_of(store: Store, iid: str) -> dict[str, Any]:
    async with store.factory() as session:
        row = await session.get(Installation, uuid.UUID(iid))
        assert row is not None and row.manifest is not None
        return copy.deepcopy(dict(row.manifest))


async def _write_manifest(store: Store, iid: str, manifest: dict[str, Any]) -> None:
    async with store.factory() as session, session.begin():
        await session.execute(
            update(Installation).where(Installation.id == uuid.UUID(iid)).values(manifest=manifest)
        )


async def _before_m72(store: Store, iid: str, **user_choices: str) -> None:
    """Rewrite the stored overlay as M15 wrote it: every tool ``write_external``, no source."""
    manifest = await _manifest_of(store, iid)
    manifest["tools"] = {
        name: (
            {"risk": user_choices[name], "riskSource": "user"}
            if name in user_choices
            else {"risk": "write_external"}
        )
        for name in manifest["tools"]
    }
    await _write_manifest(store, iid, manifest)


async def _call(store: Store, ctx: ToolContext, plugin_id: str, tool: str) -> Any:
    return await store.router.call(ctx, ToolCallRequest(f"c-{tool}", f"{plugin_id}.{tool}", {}))


@requires_postgres
async def test_an_installation_from_before_m72_is_rederived_at_the_next_listing(
    store: Store,
) -> None:
    user = await store.login("ann")
    ctx = await _context(store, user)
    iid, plugin_id, found = await _add(store, user)
    assert len(found["tools"]) == len(big_tool_names()) == 76
    await _consent(store, user, iid, found)  # every usable tool (76)
    await _before_m72(store, iid)
    assert {p["risk"] for p in (await _manifest_of(store, iid))["tools"].values()} == {
        "write_external"
    }

    # The run lists the tools: reads need no approval, a create still asks.
    offered = await store.router.list_tools(ctx)
    risks = {d.spec.name.removeprefix(f"{plugin_id}."): d.risk for d in offered if d.plugin_id}
    assert risks["list_issues"] == risks["get_project"] == risks["search_issues"] == "read"
    assert risks["create_issue"] == "write_external"
    assert risks["delete_comment"] == "write_external"
    done = await _call(store, ctx, plugin_id, "list_projects")
    assert done.status == "succeeded"
    pending = await _call(store, ctx, plugin_id, "create_issue")
    assert pending.status == "approval_required"
    assert [name for name, _ in store.remote.big_calls] == ["list_projects"]

    # The result is frozen in the row, with its source; the next listing has nothing to do.
    stored = (await _manifest_of(store, iid))["tools"]
    assert stored["list_issues"] == {"risk": "read", "riskSource": "annotation"}
    assert stored["create_issue"] == {"risk": "write_external", "riskSource": "default"}
    assert stored["delete_issue"] == {"risk": "write_external", "riskSource": "annotation"}
    assert all(policy.get("riskSource") for policy in stored.values())
    await store.router.list_tools(ctx)
    assert (await _manifest_of(store, iid))["tools"] == stored


@requires_postgres
async def test_a_users_choice_survives_the_rederivation(store: Store) -> None:
    user = await store.login("bob")
    ctx = await _context(store, user)
    iid, plugin_id, found = await _add(store, user)
    await _consent(store, user, iid, found)
    # The user had kept a read tool asking and made a delete run without asking.
    await _before_m72(store, iid, list_issues="write_external", delete_comment="read")

    assert (await _call(store, ctx, plugin_id, "list_issues")).status == "approval_required"
    assert (await _call(store, ctx, plugin_id, "get_issue")).status == "succeeded"
    assert (await _call(store, ctx, plugin_id, "delete_comment")).status == "succeeded"
    stored = (await _manifest_of(store, iid))["tools"]
    assert stored["list_issues"] == {"risk": "write_external", "riskSource": "user"}
    assert stored["delete_comment"] == {"risk": "read", "riskSource": "user"}
    assert stored["get_issue"] == {"risk": "read", "riskSource": "annotation"}


@requires_postgres
async def test_a_fresh_add_stores_sources_and_keeps_the_choice_on_reconsent(
    store: Store,
) -> None:
    user = await store.login("cat")
    ctx = await _context(store, user)
    iid, plugin_id, found = await _add(store, user)
    sources = {t["name"]: (t["risk"], t["risk_source"]) for t in found["tools"]}
    assert sources["list_issues"] == ("read", "annotation")
    assert sources["create_issue"] == ("write_external", "default")
    assert sources["delete_issue"] == ("write_external", "annotation")

    await _consent(store, user, iid, found, risks={"get_issue": "write_external"})
    stored = (await _manifest_of(store, iid))["tools"]
    assert stored["list_issues"] == {"risk": "read", "riskSource": "annotation"}
    assert stored["create_issue"] == {"risk": "write_external", "riskSource": "default"}
    assert stored["get_issue"] == {"risk": "write_external", "riskSource": "user"}
    assert (await _call(store, ctx, plugin_id, "list_issues")).status == "succeeded"
    assert (await _call(store, ctx, plugin_id, "get_issue")).status == "approval_required"

    # Re-picking shows the choice as the user's and the rest as the server's; saving without
    # touching the risks keeps the choice.
    again = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    shown = {t["name"]: (t["risk"], t["risk_source"]) for t in again["tools"]}
    assert shown["get_issue"] == ("write_external", "saved")
    assert shown["list_issues"] == ("read", "annotation")
    await _consent(store, user, iid, again)
    stored = (await _manifest_of(store, iid))["tools"]
    assert stored["get_issue"] == {"risk": "write_external", "riskSource": "user"}
    assert stored["list_issues"] == {"risk": "read", "riskSource": "annotation"}


@requires_postgres
async def test_a_consent_in_between_wins_over_the_freeze(store: Store) -> None:
    user = await store.login("dan")
    iid, _, found = await _add(store, user)
    await _consent(store, user, iid, found)
    await _before_m72(store, iid)
    before = await _manifest_of(store, iid)
    other = copy.deepcopy(before)
    other["tools"]["get_issue"] = {"risk": "write_external", "riskSource": "user"}
    await _write_manifest(store, iid, other)
    # The freeze was computed from `before`; the row has moved on, so nothing is written.
    after = copy.deepcopy(before)
    after["tools"]["get_issue"] = {"risk": "read", "riskSource": "annotation"}
    await store.tools.router._risk_store.save(iid, before, after)  # type: ignore[union-attr]  # noqa: SLF001
    assert await _manifest_of(store, iid) == other
