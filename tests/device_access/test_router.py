"""Router + device access (ADR-0016, M70): who sees which device tools, consent, grants."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import timedelta
from pathlib import Path

from app.core.ports.approval_service import Approval
from app.core.ports.tool_router import ToolCallRequest, ToolContext, ToolOutcome
from app.modules.tools.device_policy import PHONE_PLACEHOLDER

from tests.device_access.helpers import (
    LEAKY_NUMBERS,
    PHONE_NAME,
    DeviceWorld,
    answer_next_call,
    device_world,
)


def browser_context(dw: DeviceWorld, *, session: str | None = None) -> ToolContext:
    run_id = str(uuid.uuid4())
    dw.origin(run_id, "web", session=session)
    return dw.world.context("chat", run_id=run_id)


def search(call_id: str = "call_c1", query: str = "Яна") -> ToolCallRequest:
    return ToolCallRequest(call_id, "device.contacts.search", {"query": query})


async def decide(
    dw: DeviceWorld, approval: Approval, *, decision: str = "approve", scope: str = "run"
) -> None:
    """What ``POST /v1/approvals/{id}/decision`` does for the phone."""
    decided = await dw.world.approvals.decide(
        user_id=dw.user_id,
        approval_id=approval.id,
        decision=decision,  # type: ignore[arg-type]
        args_hash=approval.args_hash,
        channel="android",
        device_id=dw.phone,
    )
    await dw.access.decided(
        user_id=dw.user_id,
        approval=decided,
        decision=decision,
        scope=scope,  # type: ignore[arg-type]
    )


async def run_approved(
    dw: DeviceWorld, context: ToolContext, request: ToolCallRequest, **answer: object
) -> ToolOutcome:
    """Engine re-call with the approval while the phone answers the queued call."""
    phone = asyncio.create_task(answer_next_call(dw, **answer))  # type: ignore[arg-type]
    outcome = await dw.world.router.call(context, request)
    await asyncio.wait_for(phone, 5)
    return outcome


async def test_browser_run_sees_the_phone_tools_with_the_consent_note(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    tools = {t.spec.name: t for t in await dw.world.router.list_tools(browser_context(dw))}
    contacts = tools["device.contacts.search"]
    assert contacts.executor == "device"
    assert "must confirm on the phone" in contacts.spec.description
    assert (
        PHONE_NAME in contacts.spec.description and "approval_expired" in contacts.spec.description
    )
    assert {"device.alarm.set", "telegram.handoff"} <= set(tools)


async def test_the_phone_itself_sees_its_tools_without_consent(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    await dw.world.channel.register(
        user_id=dw.user_id,
        device_id=dw.phone,
        tools=list((await dw.access.target(dw.user_id)).tools),  # type: ignore[union-attr]
    )
    run_id = str(uuid.uuid4())
    dw.origin(run_id, "device_app", device_id=dw.phone, label="Mia на телефоне")
    context = dw.world.context("voice", run_id=run_id, device_id=dw.phone)
    tools = {t.spec.name: t for t in await dw.world.router.list_tools(context)}
    assert "use it only when the user asked" in tools["device.contacts.search"].spec.description
    assert "must confirm" not in tools["device.contacts.search"].spec.description
    assert "telegram.handoff" not in tools  # not a voice tool


async def test_jobs_and_apps_get_no_device_tools(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    for profile in ("job", "app"):
        context = dw.world.context(profile, scopes=frozenset({"tools.use"}))
        names = {t.spec.name for t in await dw.world.router.list_tools(context)}
        assert not any(name.startswith(("device.", "telegram.")) for name in names)


async def test_browser_call_waits_for_the_phone_and_is_logged(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    context = browser_context(dw)
    outcome = await dw.world.router.call(context, search())
    assert outcome.status == "approval_required" and outcome.approval is not None
    approval = outcome.approval
    consent = approval.device_consent
    assert consent is not None
    assert consent["waiting_for"] == "device" and consent["target_device_id"] == dw.phone
    assert consent["capability"] == "contacts.read" and consent["scopes"] == ["run", "persistent"]
    assert consent["requester"] == {"kind": "web", "label": "браузер Chrome на macOS"}
    assert "Подтвердите на телефоне" in approval.summary and "«Яна»" in approval.summary
    assert (approval.expires_at - approval.created_at) == timedelta(seconds=120)
    assert dw.push.sent == [("device_consent", dw.phone)]
    assert [e.decision for e in dw.store.entries] == ["pending"]
    assert not dw.events.items  # nothing reached the phone's tools yet


async def test_offline_phone_fails_at_once(tmp_path: Path) -> None:
    dw = await device_world(tmp_path, reachable=False)
    outcome = await dw.world.router.call(browser_context(dw), search())
    assert (outcome.status, outcome.error_code) == ("failed", "device_offline")
    assert [e.decision for e in dw.store.entries] == ["offline"]
    assert dw.push.sent == []


async def test_capability_turned_off_on_the_phone(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    await dw.store.set_remote(
        user_id=dw.user_id,
        device_id=dw.phone,
        capability="contacts.read",
        remote="deny",
        now=dw.world.clock(),
    )
    context = browser_context(dw)
    names = {t.spec.name for t in await dw.world.router.list_tools(context)}
    assert "device.contacts.search" not in names and "device.alarm.set" in names
    outcome = await dw.world.router.call(context, search())
    assert (outcome.status, outcome.error_code) == ("denied", "device_capability_disabled")


async def test_consent_covers_the_run_and_results_lose_phone_numbers(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    context = browser_context(dw)
    asked = await dw.world.router.call(context, search())
    assert asked.approval is not None
    await decide(dw, asked.approval)

    outcome = await run_approved(
        dw,
        context,
        dataclasses.replace(search(), approval_id=asked.approval.id),
        content=f"Found 1 contact: Яна Петрова {LEAKY_NUMBERS[0]}",
        structured={"matches": [{"name": "Яна Петрова", "phones": [LEAKY_NUMBERS[1]]}]},
    )
    assert outcome.status == "succeeded"
    dumped = json.dumps([outcome.content, outcome.structured_content], ensure_ascii=False)
    assert not any(number in dumped for number in LEAKY_NUMBERS)
    assert PHONE_PLACEHOLDER in outcome.content
    assert outcome.structured_content == {"matches": [{"name": "Яна Петрова", "phone_count": 1}]}
    (run_id, event_type, payload) = dw.events.items[0]
    assert event_type == "device_tool.call" and payload["call"]["device_id"] == dw.phone

    # Repeated reads in the same run need no new consent.
    again = await run_approved(dw, context, search("call_c2", "Олег"))
    assert again.status == "succeeded"
    assert [e.decision for e in dw.store.entries] == ["approved", "allowed"]
    # Another run of the same browser asks again (scope "this request").
    other = browser_context(dw)
    assert (await dw.world.router.call(other, search("call_c3"))).status == "approval_required"


async def test_denied_consent_is_logged(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    asked = await dw.world.router.call(browser_context(dw), search())
    assert asked.approval is not None
    await decide(dw, asked.approval, decision="deny")
    assert [e.decision for e in dw.store.entries] == ["denied"]
    current = await dw.world.approvals.get(user_id=dw.user_id, approval_id=asked.approval.id)
    assert current is not None and current.status == "denied"


async def test_persistent_grant_per_requester_expires_and_is_revoked(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    session = str(uuid.uuid4())
    asked = await dw.world.router.call(browser_context(dw, session=session), search())
    assert asked.approval is not None
    await decide(dw, asked.approval, scope="persistent")
    (grant,) = await dw.store.list_grants(
        user_id=dw.user_id, device_id=dw.phone, now=dw.world.clock()
    )
    assert grant.capability == "contacts.read" and grant.requester_key == f"web:{session}"
    assert grant.expires_at - grant.created_at == timedelta(days=30)

    # The same browser, a new run: allowed without asking.
    same = browser_context(dw, session=session)
    assert (await run_approved(dw, same, search("call_p1"))).status == "succeeded"
    # Another browser still has to ask.
    other = browser_context(dw)
    assert (await dw.world.router.call(other, search("call_p2"))).status == "approval_required"
    # The browser session ends (sign-out): the grant no longer counts.
    later = browser_context(dw, session=session)
    dw.store.web_sessions[session] = (dw.user_id, None, False)
    assert (await dw.world.router.call(later, search("call_p3"))).status == "approval_required"
    dw.store.web_sessions[session] = (dw.user_id, None, True)
    # 30 days later it has expired.
    dw.world.clock.advance(timedelta(days=30, seconds=1).total_seconds())
    expired = browser_context(dw, session=session)
    assert (await dw.world.router.call(expired, search("call_p4"))).status == "approval_required"


async def test_revoked_grant_asks_again(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    session = str(uuid.uuid4())
    asked = await dw.world.router.call(browser_context(dw, session=session), search())
    assert asked.approval is not None
    await decide(dw, asked.approval, scope="persistent")
    (grant,) = await dw.store.list_grants(
        user_id=dw.user_id, device_id=dw.phone, now=dw.world.clock()
    )
    assert await dw.store.revoke_grant(
        user_id=dw.user_id, device_id=dw.phone, grant_id=grant.id, now=dw.world.clock()
    )
    assert not await dw.store.revoke_grant(
        user_id=dw.user_id, device_id=dw.phone, grant_id=grant.id, now=dw.world.clock()
    )
    context = browser_context(dw, session=session)
    assert (await dw.world.router.call(context, search("call_r1"))).status == "approval_required"


async def test_external_effects_are_asked_per_action(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    context = browser_context(dw)
    handoff = ToolCallRequest(
        "call_t1", "telegram.handoff", {"text": "Привет", "contact_name": "Яна"}
    )
    asked = await dw.world.router.call(context, handoff)
    assert asked.approval is not None and asked.approval.device_consent is not None
    assert asked.approval.device_consent["scopes"] == ["run"]
    await decide(dw, asked.approval)
    done = await run_approved(
        dw,
        context,
        dataclasses.replace(handoff, approval_id=asked.approval.id),
        content="Telegram opened",
    )
    assert done.status == "succeeded"
    second = ToolCallRequest("call_t2", "telegram.handoff", {"text": "Ещё", "contact_name": "Яна"})
    assert (await dw.world.router.call(context, second)).status == "approval_required"


async def test_webview_run_is_the_phone_itself(tmp_path: Path) -> None:
    dw = await device_world(tmp_path, live=True)
    run_id = str(uuid.uuid4())
    dw.origin(run_id, "device_webview", device_id=dw.phone, label="Mia на телефоне")
    context = dw.world.context("chat", run_id=run_id, device_id=dw.phone)
    outcome = await run_approved(dw, context, search())
    assert outcome.status == "succeeded" and outcome.approval is None
    assert dw.push.sent == [("device_tool_call", dw.phone)]  # push delivery, no consent
    assert dw.store.entries == []


async def test_a_reused_approval_is_refused(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    context = browser_context(dw)
    asked = await dw.world.router.call(context, search())
    assert asked.approval is not None
    await decide(dw, asked.approval)
    request = dataclasses.replace(search(), approval_id=asked.approval.id)
    assert (await run_approved(dw, context, request)).status == "succeeded"
    replay = await dw.world.router.call(context, dataclasses.replace(request, call_id="call_x"))
    assert (replay.status, replay.error_code) == ("denied", "approval_already_used")


async def test_tool_list_is_capped_at_the_model_limit(tmp_path: Path) -> None:
    dw = await device_world(tmp_path)
    router = dw.world.router
    context = dw.world.context("chat")
    full = [t.spec.name for t in await router.list_tools(context)]
    router._config = dataclasses.replace(router._config, max_model_tools=len(full) - 1)  # noqa: SLF001
    capped = [t.spec.name for t in await router.list_tools(context)]
    assert len(capped) == len(full) - 1
    (dropped,) = set(full) - set(capped)
    outcome = await router.call(context, ToolCallRequest("call_cap", dropped, {}))
    assert outcome.error_code == "tool_unavailable"


async def test_a_web_approval_cannot_run_a_phone_call(tmp_path: Path) -> None:
    from app.core.ports.approval_service import ApprovalRequest

    dw = await device_world(tmp_path)
    context = browser_context(dw)
    forged = await dw.world.approvals.request(
        ApprovalRequest(
            user_id=dw.user_id,
            run_id=context.run_id,
            tool_call_id="call_f1",
            tool="device.contacts.search",
            risk="read",
            summary="x",
            arguments={"query": "Яна"},
        )
    )
    await dw.world.approvals.decide(
        user_id=dw.user_id,
        approval_id=forged.id,
        decision="approve",
        args_hash=forged.args_hash,
        channel="web",
    )
    outcome = await dw.world.router.call(
        context, dataclasses.replace(search("call_f1"), approval_id=forged.id)
    )
    assert (outcome.status, outcome.error_code) == ("denied", "approval_mismatch")
    assert dw.events.items == []
