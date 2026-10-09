"""M41 DoD through the run engine (Postgres run store, real router, real plugin server,
memory tools, fake phone): a name → clarification for two «Яна» → approval → hand-off;
denial or expiry sends nothing."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.ports.device_tool_channel import DeviceToolResult
from app.core.ports.tool_router import ToolContext
from app.modules.conversations.feed import FeedService
from app.modules.runs import EngineConfig, RunEngine, RunNotifier, RunService, SqlRunStore

from tests.fakes import FakeModelGateway, text_step, tool_step
from tests.plugins.telegram.conftest import (
    PLUGIN_ID,
    TelegramWorld,
    approve_when_asked,
    requires_postgres,
)
from tests.runs.conftest import TEST_LIMITS, assert_well_formed, collect, types
from tests.runs.gateways import answer, scripted

pytestmark = requires_postgres

SEND = f"{PLUGIN_ID}.send_message"
TEXT = "Надо купить молоко"


def service_for(tg: TelegramWorld, gateway: FakeModelGateway) -> RunService:
    engine = RunEngine(
        store=SqlRunStore(tg.factory),
        notifier=RunNotifier(),
        gateway=gateway,
        config=EngineConfig(limits=TEST_LIMITS),
        router=tg.router,
        approvals=tg.approvals,
        memory=tg.service,
        feed=FeedService(tg.factory),
    )
    return RunService(engine, keepalive_s=0.2)


async def run(
    tg: TelegramWorld,
    gateway: FakeModelGateway,
    *,
    decision: str | None = "approve",
    device: bool = True,
    text: str = "Напиши Яне в Телеграм, что надо купить молоко",
) -> list[dict[str, Any]]:
    service = service_for(tg, gateway)
    record, _ = await service.start(tg.new_run(text, device=device))
    approver = (
        asyncio.create_task(approve_when_asked(service, tg, record.id, decision))
        if decision is not None
        else None
    )
    events = await collect(service, tg.user, record.id)
    if approver is not None:
        approver.cancel()
        await asyncio.gather(approver, return_exceptions=True)
    await service.wait_finished(record.id)
    assert_well_formed(events)
    await service.aclose()
    return events


def completed(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["payload"] for e in events if e["type"] == "tool.call.completed"]


def tool_texts(gateway: FakeModelGateway) -> list[dict[str, Any]]:
    return [
        json.loads(m.content)
        for request in gateway.requests
        for m in request.messages
        if m.role == "tool"
    ]


async def test_name_then_approval_then_handoff(tg: TelegramWorld) -> None:
    await tg.add_person("Яна Петрова", relation="жена")
    gateway = scripted(
        tool_step("c1", "people.resolve", {"name": "Яна"}),
        tool_step("c2", SEND, {"to": "Яна Петрова", "text": TEXT}),
        text_step(answer("Открыла чат с Яной, нажмите «Отправить».")),
    )

    events = await run(tg, gateway)

    assert types(events) == [
        "run.started",
        "tool.call.started",
        "tool.call.completed",
        "tool.call.started",
        "approval.requested",
        "approval.resolved",
        "tool.call.completed",
        "response.final",
        "run.completed",
    ]
    approval = next(e["payload"]["approval"] for e in events if e["type"] == "approval.requested")
    assert approval["tool"] == SEND and approval["risk"] == "write_external"
    assert approval["arguments"] == {"to": "Яна Петрова", "text": TEXT}
    assert TEXT in approval["summary"] and "Яна Петрова" in approval["summary"]
    [handoff] = tg.phone.handoffs
    assert handoff.tool == "telegram.handoff"
    assert handoff.arguments == {"text": TEXT, "contact_name": "Яна Петрова"}
    assert handoff.device_id == str(tg.device_id)
    send_done = completed(events)[1]
    assert send_done["status"] == "succeeded"
    assert send_done["structured_content"] == {
        "recipient": "Яна Петрова",
        "opened": True,
        "sent": False,
        "direct_chat": True,
    }
    framed = tool_texts(gateway)[-1]["content"]
    assert "НЕ отправлено" in framed and "нажимает «Отправить»" in framed


async def test_two_yanas_are_clarified_before_anything_else(tg: TelegramWorld) -> None:
    petrova = await tg.add_person("Яна Петрова", relation="жена")
    await tg.add_person("Яна Коваль", relation="коллега")

    first = scripted(
        tool_step("c1", "people.resolve", {"name": "Яна"}),
        text_step(answer("Яне Петровой или Яне Коваль?")),
    )
    events = await run(tg, first)

    candidates = completed(events)[0]["structured_content"]["candidates"]
    assert {c["name"] for c in candidates} == {"Яна Петрова", "Яна Коваль"}
    assert "approval.requested" not in types(events)
    assert tg.phone.handoffs == [] and tg.phone.searches == []

    # The user answers «Петровой»: the model sends to that person's ref.
    second = scripted(
        tool_step("c2", SEND, {"to": f"person:{petrova}", "text": TEXT}),
        text_step(answer("Открыла чат, нажмите «Отправить».")),
    )
    events = await run(tg, second, text="Петровой")

    assert "approval.resolved" in types(events)
    [handoff] = tg.phone.handoffs
    assert handoff.arguments == {"text": TEXT, "contact_name": "Яна Петрова"}


async def test_ambiguous_name_in_send_message_fails_without_touching_the_phone(
    tg: TelegramWorld,
) -> None:
    await tg.add_person("Яна Петрова")
    await tg.add_person("Яна Коваль")
    gateway = scripted(
        tool_step("c1", SEND, {"to": "Яна", "text": TEXT}),
        text_step(answer("Уточните, какой Яне.")),
    )

    events = await run(tg, gateway)

    assert completed(events)[0]["status"] == "failed"
    framed = tool_texts(gateway)[-1]["content"]
    assert "Яна Петрова" in framed and "Яна Коваль" in framed and "Ничего не отправлено" in framed
    assert tg.phone.handoffs == []


async def test_denied_approval_sends_nothing(tg: TelegramWorld) -> None:
    await tg.add_person("Яна Петрова")
    gateway = scripted(
        tool_step("c1", SEND, {"to": "Яна Петрова", "text": TEXT}),
        text_step(answer("Хорошо, не отправляю.")),
    )

    events = await run(tg, gateway, decision="deny")

    [resolved] = [e["payload"] for e in events if e["type"] == "approval.resolved"]
    assert resolved["status"] == "denied"
    assert completed(events)[0]["status"] == "denied"
    assert tg.phone.handoffs == [] and tg.phone.searches == []


async def test_expired_approval_sends_nothing(tg: TelegramWorld) -> None:
    await tg.add_person("Яна Петрова")
    gateway = scripted(
        tool_step("c1", SEND, {"to": "Яна Петрова", "text": TEXT}),
        text_step(answer("Время подтверждения вышло.")),
    )

    events = await run(tg, gateway, decision=None)

    [resolved] = [e["payload"] for e in events if e["type"] == "approval.resolved"]
    assert resolved["status"] == "expired"
    assert completed(events)[0]["status"] == "expired"
    assert tg.phone.handoffs == []


async def test_contact_only_on_the_phone_is_searched_and_handed_off(tg: TelegramWorld) -> None:
    tg.phone.contacts = ["Яна Гейтова", "Пётр Иванов"]
    gateway = scripted(
        tool_step("c1", "people.resolve", {"name": "Яна"}),
        tool_step("c2", SEND, {"to": "contact:1", "text": TEXT}),
        text_step(answer("Открыла чат.")),
    )

    events = await run(tg, gateway)

    assert completed(events)[0]["structured_content"]["candidates"][0]["ref"] == "contact:1"
    [handoff] = tg.phone.handoffs
    assert handoff.arguments == {"text": TEXT, "contact_name": "Яна Гейтова"}
    assert "phone" not in json.dumps(dict(handoff.arguments))


async def test_two_phone_contacts_with_one_name_stop_before_the_phone(tg: TelegramWorld) -> None:
    tg.phone.contacts = ["Яна Гейтова", "Яна Смирнова"]
    gateway = scripted(
        tool_step("c1", SEND, {"to": "Яна", "text": TEXT}),
        text_step(answer("Какую Яну?")),
    )

    events = await run(tg, gateway)

    assert completed(events)[0]["status"] == "failed"
    assert "contact:1" in tool_texts(gateway)[-1]["content"]
    assert tg.phone.handoffs == []


async def test_known_telegram_username_is_used_instead_of_a_contact_name(
    tg: TelegramWorld,
) -> None:
    await tg.add_person("Яна Петрова", username="@yana_p")
    gateway = scripted(
        tool_step("c1", SEND, {"to": "Яна Петрова", "text": TEXT}),
        text_step(answer("Открыла чат.")),
    )

    await run(tg, gateway)

    [handoff] = tg.phone.handoffs
    assert handoff.arguments == {"text": TEXT, "username": "yana_p"}


async def test_run_without_a_phone_cannot_hand_off(tg: TelegramWorld) -> None:
    await tg.add_person("Яна Петрова")
    gateway = scripted(
        tool_step("c1", SEND, {"to": "Яна Петрова", "text": TEXT}),
        text_step(answer("Нужен телефон.")),
    )

    events = await run(tg, gateway, device=False)

    assert completed(events)[0]["status"] == "failed"
    assert "телефон" in tool_texts(gateway)[-1]["content"]
    assert tg.phone.handoffs == []


async def test_device_failures_are_reported_and_never_claim_a_send(tg: TelegramWorld) -> None:
    await tg.add_person("Яна Петрова")
    cases = [
        (DeviceToolResult("x", "failed", error_code="device_locked"), "заблокирован"),
        (DeviceToolResult("x", "failed", error_code="os_permission_denied"), "контактам"),
        (DeviceToolResult("x", "uncertain", error_code="timeout"), "не ответил"),
        (
            DeviceToolResult(
                "x", "failed", error_code="invalid_arguments", error_message="Several contacts."
            ),
            "Several contacts",
        ),
    ]
    for index, (result, expected) in enumerate(cases):
        tg.phone.handoff_result = result
        gateway = scripted(
            tool_step(f"c{index}", SEND, {"to": "Яна Петрова", "text": TEXT}),
            text_step(answer("Не вышло.")),
        )
        events = await run(tg, gateway)
        assert completed(events)[0]["status"] == "failed"
        framed = tool_texts(gateway)[-1]["content"]
        assert expected in framed and "Ничего не отправлено" in framed
        assert "sent" not in (completed(events)[0].get("structured_content") or {})


async def test_old_app_without_the_handoff_tool_degrades_with_a_message(
    tg: TelegramWorld,
) -> None:
    await tg.add_person("Яна Петрова")
    tg.phone.declared = [d for d in tg.phone.declared if d.name != "telegram.handoff"]
    gateway = scripted(
        tool_step("c1", SEND, {"to": "Яна Петрова", "text": TEXT}),
        text_step(answer("Обновите приложение.")),
    )

    events = await run(tg, gateway)

    assert completed(events)[0]["status"] == "failed"
    assert "обновить" in tool_texts(gateway)[-1]["content"]


async def test_send_message_is_a_voice_tool_that_needs_approval(tg: TelegramWorld) -> None:
    context = ToolContext(
        user_id=str(tg.user),
        run_id=str(tg.device_id),
        profile="voice",
        workspace_id=tg.installation.workspace_id,
        device_id=str(tg.device_id),
    )
    tools = {t.spec.name: t for t in await tg.router.list_tools(context)}
    assert tools[SEND].risk == "write_external" and tools[SEND].voice
    assert f"{PLUGIN_ID}.connect_bot" not in tools  # not a voice tool
