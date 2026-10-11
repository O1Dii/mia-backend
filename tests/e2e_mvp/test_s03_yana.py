"""Scenario 3: «Напиши Яне…» — clarification, confirmation, hand-off to the phone.

Production composition over HTTP: two «Яна» in memory (``POST /v1/memory/people``, one with a
phone number that must never leave the server), ``builtin.telegram`` from the store, the phone
registers the M23 device tools. A voice run from the phone resolves the name (two
candidates → the model asks, nothing is sent), the user answers «Петровой» in the same chat,
the model calls ``builtin.telegram.send_message`` (``write_external``) → approval decided on
the phone over ``POST /v1/approvals/{id}/decision`` → ``device_tool.call``
``telegram.handoff`` reaches the phone, which answers over
``POST /v1/runs/{id}/device-tool-results``. Live: UC-M41-1, UC-MW3-3, UC-MW2-4 (locked screen).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from tests.e2e_mvp.harness import (
    Mvp,
    Person,
    call,
    final,
    payloads,
    requires_stack,
    say,
    types,
)

pytestmark = requires_stack

SEND = "builtin.telegram.send_message"
TEXT = "Надо купить молоко"
ASK = "Напиши Яне в Телеграм, что надо купить молоко"
PHONE_NUMBER = "+375291112233"
OPENED = {"opened": True, "direct_chat": True, "sent": False}


async def prepare(mvp: Mvp) -> tuple[Person, str, str]:
    await mvp.publish_builtin("telegram")
    phone = await mvp.phone("yana-owner")
    await phone.register_device_tools()
    installed = await mvp.install(phone, "builtin.telegram")
    assert installed["status"] == "active", installed
    ids = []
    for name, relation, identifiers in (
        ("Яна Петрова", "жена", {"phone": PHONE_NUMBER}),
        ("Яна Коваль", "коллега", None),
    ):
        body: dict[str, Any] = {"display_name": name, "relation": relation}
        if identifiers:
            body["identifiers"] = identifiers
        created = await phone.post("/v1/memory/people", body)
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    return phone, ids[0], ids[1]


async def phone_hands_off(mvp: Mvp, phone: Person, run_id: str) -> dict[str, Any]:
    """The app receives ``telegram.handoff``, opens Telegram with the draft, reports back."""
    event = await mvp.wait_event(phone.user_id, run_id, "device_tool.call", wait_s=15)
    device_call = event["payload"]["call"]
    response = await phone.answer_device_call(
        run_id,
        device_call["call_id"],
        "Telegram opened with the draft; the user must press send",
        OPENED,
    )
    assert response.status_code == 204, response.text
    return dict(device_call)


def no_number(events: list[dict[str, Any]], mvp: Mvp) -> None:
    dumped = json.dumps(events, ensure_ascii=False)
    assert PHONE_NUMBER not in dumped and "1112233" not in dumped
    for request in mvp.model.requests:
        for message in request.messages:
            assert "1112233" not in (message.content or "")


async def ask_and_clarify(mvp: Mvp, phone: Person) -> tuple[str, list[dict[str, Any]]]:
    mvp.model.then(
        call("people.resolve", {"name": "Яна"}, "c_resolve"),
        say("Яне Петровой или Яне Коваль?"),
    )
    run_id = await phone.start_run(ASK, profile="voice")
    events = await phone.events(run_id)
    assert types(events)[-1] == "run.completed", types(events)
    [resolved] = payloads(events, "tool.call.completed")
    candidates = resolved["structured_content"]["candidates"]
    assert {c["name"] for c in candidates} == {"Яна Петрова", "Яна Коваль"}
    assert all(c["ref"].startswith("person:") for c in candidates)
    assert "approval.requested" not in types(events)
    assert "device_tool.call" not in types(events)
    assert final(events)["speech"] == "Яне Петровой или Яне Коваль?"
    run = (await phone.get(f"/v1/runs/{run_id}")).json()
    return str(run["conversation_id"]), events


async def test_two_yanas_clarify_then_approve_on_the_phone_and_hand_off(mvp: Mvp) -> None:
    phone, petrova, _ = await prepare(mvp)
    conversation, first = await ask_and_clarify(mvp, phone)

    # «Петровой» in the same chat: the previous turn is in the model's context.
    mvp.model.then(
        call(SEND, {"to": f"person:{petrova}", "text": TEXT}, "c_send"),
        say("Открыла чат с Яной Петровой, нажмите «Отправить»."),
    )
    run_id = await phone.start_run("Петровой", profile="voice", conversation_id=conversation)
    handoff = asyncio.create_task(phone_hands_off(mvp, phone, run_id))
    approval = await phone.pending_approval(wait_s=10)
    assert approval["tool"] == SEND and approval["risk"] == "write_external"
    assert approval["arguments"] == {"to": f"person:{petrova}", "text": TEXT}
    assert TEXT in approval["summary"]  # the recipient: see the test at the end (M41-R2)

    # A decision for other arguments (tampered hash) is refused; the right one goes through.
    tampered = await phone.post(
        f"/v1/approvals/{approval['id']}/decision",
        {"decision": "approve", "args_hash": "sha256:" + "0" * 64},
    )
    assert tampered.status_code in (409, 422), tampered.text
    decided = await phone.decide(approval, "approve")
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    device_call = await asyncio.wait_for(handoff, 15)
    events = await phone.events(run_id)

    assert device_call["tool"] == "telegram.handoff"
    assert device_call["arguments"] == {"text": TEXT, "contact_name": "Яна Петрова"}
    assert types(events)[-1] == "run.completed", types(events)
    order = [t for t in types(events) if t != "speech.ack"]
    assert order.index("approval.requested") < order.index("approval.resolved")
    assert order.index("approval.resolved") < order.index("device_tool.call")
    [resolved] = payloads(events, "approval.resolved")
    assert resolved["status"] == "approved"
    [done] = payloads(events, "tool.call.completed")
    assert done["status"] == "succeeded"
    assert done["structured_content"]["sent"] is False  # the user presses «Отправить»
    history = "\n".join(m.content or "" for m in mvp.model.requests[2].messages)
    assert "Яне Петровой или Яне Коваль?" in history  # same chat, previous turn in context
    no_number(first + events, mvp)

    # The phone's feed of that chat keeps the step with «подтверждено на телефоне».
    feed = await phone.get("/v1/feed", conversation_id=conversation)
    assert feed.status_code == 200, feed.text
    steps = [s for item in feed.json()["items"] for s in item.get("steps", [])]
    sends = [s for s in steps if s["tool"] == SEND]
    assert sends and sends[-1]["approval"]["channel"] == "android"


async def test_denied_approval_sends_nothing(mvp: Mvp) -> None:
    phone, petrova, _ = await prepare(mvp)
    mvp.model.then(
        call(SEND, {"to": f"person:{petrova}", "text": TEXT}, "c_send"),
        say("Хорошо, не отправляю."),
    )
    run_id = await phone.start_run(ASK, profile="voice")
    approval = await phone.pending_approval(wait_s=10)
    denied = await phone.decide(approval, "deny")
    assert denied.status_code == 200, denied.text

    events = await phone.events(run_id)
    assert types(events)[-1] == "run.completed"
    assert "device_tool.call" not in types(events)
    [resolved] = payloads(events, "approval.resolved")
    assert resolved["status"] == "denied"
    assert payloads(events, "tool.call.completed")[0]["status"] == "denied"
    no_number(events, mvp)


async def test_another_account_cannot_decide_the_approval(mvp: Mvp) -> None:
    phone, petrova, _ = await prepare(mvp)
    stranger = await mvp.phone("stranger")
    mvp.model.then(
        call(SEND, {"to": f"person:{petrova}", "text": TEXT}, "c_send"),
        say("Хорошо, не отправляю."),
    )
    run_id = await phone.start_run(ASK, profile="voice")
    approval = await phone.pending_approval(wait_s=10)
    refused: httpx.Response = await stranger.decide(approval, "approve")
    assert refused.status_code == 404, refused.text
    assert (await stranger.get(f"/v1/approvals/{approval['id']}")).status_code == 404
    assert (await phone.decide(approval, "deny")).status_code == 200
    events = await phone.events(run_id)
    assert "device_tool.call" not in types(events)


async def test_confirmation_names_the_recipient_chosen_by_ref(mvp: Mvp) -> None:
    phone, petrova, _ = await prepare(mvp)
    mvp.model.then(
        call(SEND, {"to": f"person:{petrova}", "text": TEXT}, "c_send"),
        say("Хорошо, не отправляю."),
    )
    run_id = await phone.start_run(ASK, profile="voice")
    approval = await phone.pending_approval(wait_s=10)
    summary = approval["summary"]
    assert (await phone.decide(approval, "deny")).status_code == 200
    await phone.events(run_id)
    assert "Яна Петрова" in summary and "person:" not in summary
