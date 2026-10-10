"""M72: the «Где работает» block, device capabilities on the consent screen, annotation risks."""

from __future__ import annotations

import json
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.core.ports.tool_router import ToolCallRequest, ToolContext
from app.modules.plugins.catalog.validation import schema_problems
from app.modules.plugins.install.consent import build_consent
from app.modules.plugins.install.service import annotation_risk
from app.modules.tools.digest import ServerTool

from tests.catalog.harness import Store, build_store
from tests.catalog.remote_world import MCP_HOST, RESOURCE, browser_authorize
from tests.runs.conftest import requires_postgres

BUILTIN = Path(__file__).resolve().parents[2] / "app" / "plugins" / "builtin"


def manifest_of(plugin: str) -> dict[str, Any]:
    return dict(json.loads((BUILTIN / plugin / "mia.json").read_text(encoding="utf-8")))


def consent_of(plugin: str) -> Any:
    return build_consent(manifest_of(plugin), [], "sha256:x")


@pytest.mark.parametrize("plugin", ["alarm", "timer", "telegram"])
def test_phone_plugins_show_where_they_run(plugin: str) -> None:
    consent = consent_of(plugin)
    assert consent.where_it_runs.runs_on == "cloud_and_device"
    assert consent.where_it_runs.cloud and consent.where_it_runs.device
    assert consent.device_capabilities, "the consent screen lists the device capabilities"


def test_capabilities_carry_policy_labels_and_optionality() -> None:
    alarm = consent_of("alarm").device_capabilities[0]
    assert (alarm.capability, alarm.kind, alarm.optional) == ("alarm.clock", "action", True)
    assert "Часах" in alarm.label and alarm.description and alarm.model_sees
    telegram = consent_of("telegram").device_capabilities[0]
    assert (telegram.capability, telegram.kind, telegram.optional) == (
        "telegram.draft",
        "external",
        False,
    )


@pytest.mark.parametrize("plugin", ["notes", "focus", "news", "internet"])
def test_cloud_only_plugins_have_no_device_block(plugin: str) -> None:
    consent = consent_of(plugin)
    assert consent.where_it_runs.runs_on == "cloud" and consent.where_it_runs.device is None
    assert consent.device_capabilities == []


def test_without_declaration_the_cloud_line_comes_from_the_server_kind() -> None:
    manifest = manifest_of("notes")
    del manifest["whereItRuns"]
    assert build_consent(manifest, [], "d").where_it_runs.cloud == "Работает на сервере Mia."
    manifest["server"] = {"kind": "remote", "url": "https://mcp.example.com/", "auth": "none"}
    assert "стороннем" in build_consent(manifest, [], "d").where_it_runs.cloud


def test_undeclared_device_line_is_built_from_the_capabilities() -> None:
    manifest = manifest_of("alarm")
    del manifest["whereItRuns"]
    where = build_consent(manifest, [], "d").where_it_runs
    assert where.runs_on == "cloud_and_device" and "Часах" in (where.device or "")


def test_unknown_capability_is_external() -> None:
    manifest = manifest_of("alarm")
    manifest["requires"]["deviceCapabilities"] = [
        {"capability": "camera.shoot", "description": "Снимает фото на телефоне"}
    ]
    [capability] = build_consent(manifest, [], "d").device_capabilities
    assert capability.kind == "external" and capability.optional is True


def test_builtin_manifests_pass_the_schema() -> None:
    for plugin in ("alarm", "timer", "telegram", "notes", "focus", "news", "internet"):
        assert schema_problems(manifest_of(plugin)) == [], plugin


def test_annotations_decide_the_risk() -> None:
    def tool(**hints: bool) -> ServerTool:
        return ServerTool("t", "d", {}, annotations=hints or None)

    assert annotation_risk(tool(readOnlyHint=True)) == ("read", True)
    assert annotation_risk(tool(readOnlyHint=True, destructiveHint=True)) == ("read", True)
    assert annotation_risk(tool(destructiveHint=False)) == ("write_reversible", True)
    assert annotation_risk(tool(destructiveHint=True)) == ("write_external", True)
    assert annotation_risk(tool()) == ("write_external", False)
    assert annotation_risk(tool(readOnlyHint=False)) == ("write_external", False)


# ------------------------------------------------------- annotated server (Postgres)


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    async with build_store(annotated=True) as value:
        yield value


def _json(response: httpx.Response, status: int) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


async def _connected(store: Store, user: dict[str, str]) -> str:
    created = _json(await store.post(user, "/v1/installations", {"custom_mcp_url": RESOURCE}), 201)
    iid = str(created["id"])
    started = _json(await store.post(user, "/v1/connections", {"installation_id": iid}), 201)
    location = await browser_authorize(store.remote, started["authorization_url"])
    parts = urllib.parse.urlsplit(location)
    await store.client.get(f"{parts.path}?{parts.query}")
    return iid


async def _consent(
    store: Store, user: dict[str, str], iid: str, found: dict[str, Any], **extra: Any
) -> httpx.Response:
    current = _json(await store.get(user, f"/v1/installations/{iid}"), 200)
    grants = [g["id"] for g in found["grants"]] + [g["id"] for g in found["risk_grants"]]
    return await store.post(
        user,
        f"/v1/installations/{iid}/consent",
        {
            "expected_version": current["state_version"],
            "tools_digest": found["tools_digest"],
            "grants": grants,
            **extra,
        },
    )


@requires_postgres
async def test_server_by_url_reads_without_approval_and_risk_is_editable(store: Store) -> None:
    user = await store.login("ann")
    iid = await _connected(store, user)
    found = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    risks = {t["name"]: (t["risk"], t["risk_source"]) for t in found["tools"]}
    assert risks["list_issues"] == ("read", "annotation")
    assert risks["create_issue"] == ("write_reversible", "annotation")
    assert {g["id"] for g in found["risk_grants"]} == {
        "tools.read",
        "tools.write_reversible",
        "tools.write_external",
    }

    # Consent as proposed: the read runs at once, the create does not need an approval either.
    active = _json(await _consent(store, user, iid, found), 200)
    assert set(active["grants"]) == {"tools.read", "tools.write_reversible", f"network:{MCP_HOST}"}
    workspace = _json(await store.get(user, "/v1/workspace"), 200)
    ctx = ToolContext(
        user_id=workspace["owner_user_id"],
        run_id=str(uuid.uuid4()),
        profile="chat",
        workspace_id=workspace["id"],
    )
    plugin_id = active["plugin_id"]
    done = await store.router.call(ctx, ToolCallRequest("c1", f"{plugin_id}.list_issues", {}))
    assert done.status == "succeeded"

    # The user raises create_issue to «действие во внешнем сервисе»: it asks again.
    again = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    assert {t["name"]: t["risk_source"] for t in again["tools"]}["list_issues"] == "saved"
    raised = await _consent(store, user, iid, again, risks={"create_issue": "write_external"})
    assert _json(raised, 200)["status"] == "active"
    pending = await store.router.call(
        ctx, ToolCallRequest("c2", f"{plugin_id}.create_issue", {"title": "x"})
    )
    assert pending.status == "approval_required"


@requires_postgres
async def test_risks_for_unchosen_tools_are_refused(store: Store) -> None:
    user = await store.login("ann")
    iid = await _connected(store, user)
    found = _json(await store.get(user, f"/v1/installations/{iid}/discovery"), 200)
    response = await _consent(
        store, user, iid, found, tools=["list_issues"], risks={"create_issue": "read"}
    )
    assert response.status_code == 422
    bad = await _consent(store, user, iid, found, risks={"list_issues": "nuke"})
    assert bad.status_code == 422
