"""DoD (M70): «найди Яну в контактах» from the browser waits for the phone.

Through the run engine (memory and Postgres run stores): model → device.contacts.search →
approval only the phone can decide → push → the fake phone approves/denies/ignores → the
call reaches the phone's queue → result without phone numbers → answer. Every event is
validated against ``run-events.schema.json`` by ``collect``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from app.modules.runs import NewRun, RunService

from tests.device_access.helpers import LEAKY_NUMBERS, DeviceWorld, answer_next_call, device_world
from tests.fakes import text_step, tool_step
from tests.runs.conftest import RunWorld, assert_well_formed, collect, payload_of, types, world
from tests.runs.gateways import answer, scripted

__all__ = ["world"]

ASK = "найди Яну в контактах"
DIGITS = ("1234567", "2912345", "7654321")


async def start(
    world: RunWorld, dw: DeviceWorld, *, origin: str = "web", device: bool = False
) -> tuple[RunService, uuid.UUID]:
    gateway = scripted(
        tool_step("call_c1", "device.contacts.search", {"query": "Яна"}),
        text_step(answer("Нашла: Яна Петрова.")),
    )
    service = world.service(gateway, router=dw.world.router, approvals=dw.world.approvals)
    dw.world.channel.bind_publisher(service.publish)
    new = NewRun(
        user_id=world.user,
        profile="chat",
        input_text=ASK,
        device_id=uuid.UUID(dw.phone) if device else None,
    )
    dw.origin(str(new.id), origin, device_id=dw.phone if device else None)
    record, _ = await service.start(new)
    return service, record.id


async def phone_decides(dw: DeviceWorld, decision: str) -> None:
    approval = await asyncio.wait_for(dw.push.consents.get(), 5)
    decided = await dw.world.approvals.decide(
        user_id=dw.user_id,
        approval_id=approval.id,
        decision=decision,  # type: ignore[arg-type]
        args_hash=approval.args_hash,
        channel="android",
        device_id=dw.phone,
    )
    await dw.access.decided(user_id=dw.user_id, approval=decided, decision=decision, scope="run")


def no_phone_numbers(events: list[dict[str, Any]]) -> None:
    dumped = json.dumps(events, ensure_ascii=False)
    assert not any(number in dumped for number in LEAKY_NUMBERS)
    assert not any(digits in dumped for digits in DIGITS)


async def test_browser_request_waits_for_the_phone_and_gets_names_only(
    world: RunWorld, tmp_path: Path
) -> None:
    dw = await device_world(tmp_path, user_id=str(world.user))
    service, run_id = await start(world, dw)

    async def phone() -> None:
        await phone_decides(dw, "approve")
        await answer_next_call(
            dw,
            content=f"Found 2 contacts: Яна Петрова {LEAKY_NUMBERS[0]}; Яна {LEAKY_NUMBERS[2]}",
            structured={
                "matches": [
                    {"name": "Яна Петрова", "phone_count": 1, "phone": LEAKY_NUMBERS[1]},
                    {"name": "Яна", "phones": [LEAKY_NUMBERS[2]]},
                ]
            },
        )

    device = asyncio.create_task(phone())
    events = await collect(service, world.user, run_id)
    await asyncio.wait_for(device, 5)
    await service.wait_finished(run_id)
    assert_well_formed(events)
    assert types(events) == [
        "run.started",
        "tool.call.started",
        "approval.requested",
        "approval.resolved",
        "device_tool.call",
        "tool.call.completed",
        "response.final",
        "run.completed",
    ]
    requested = payload_of(events, "approval.requested")["approval"]
    assert requested["tool"] == "device.contacts.search"
    assert requested["summary"].startswith("Подтвердите на телефоне")
    assert payload_of(events, "approval.resolved")["status"] == "approved"
    assert payload_of(events, "device_tool.call")["call"]["device_id"] == dw.phone
    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "succeeded"
    assert completed["structured_content"] == {
        "matches": [{"name": "Яна Петрова", "phone_count": 1}, {"name": "Яна", "phone_count": 1}]
    }
    no_phone_numbers(events)
    assert [entry.decision for entry in dw.store.entries] == ["approved"]


async def test_denial_on_the_phone_gives_a_clear_refusal(world: RunWorld, tmp_path: Path) -> None:
    dw = await device_world(tmp_path, user_id=str(world.user))
    service, run_id = await start(world, dw)
    device = asyncio.create_task(phone_decides(dw, "deny"))
    events = await collect(service, world.user, run_id)
    await asyncio.wait_for(device, 5)
    await service.wait_finished(run_id)
    assert payload_of(events, "approval.resolved")["status"] == "denied"
    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "denied" and completed["error"]["code"] == "approval_denied"
    assert "device_tool.call" not in types(events)
    assert events[-1]["type"] == "run.completed"
    assert dw.push.calls.empty()


async def test_no_answer_from_the_phone_expires(world: RunWorld, tmp_path: Path) -> None:
    dw = await device_world(tmp_path, user_id=str(world.user), consent_wait_s=1)
    service, run_id = await start(world, dw)
    events = await collect(service, world.user, run_id)
    await service.wait_finished(run_id)
    assert payload_of(events, "approval.resolved")["status"] == "expired"
    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "expired" and completed["error"]["code"] == "approval_expired"
    assert events[-1]["type"] == "run.completed"


async def test_offline_phone_fails_without_waiting(world: RunWorld, tmp_path: Path) -> None:
    dw = await device_world(tmp_path, user_id=str(world.user), reachable=False)
    service, run_id = await start(world, dw)
    events = await collect(service, world.user, run_id)
    await service.wait_finished(run_id)
    assert "approval.requested" not in types(events)
    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "failed" and completed["error"]["code"] == "device_offline"


async def test_webview_request_behaves_as_from_the_phone(world: RunWorld, tmp_path: Path) -> None:
    dw = await device_world(tmp_path, user_id=str(world.user), live=True)
    service, run_id = await start(world, dw, origin="device_webview", device=True)
    device = asyncio.create_task(
        answer_next_call(
            dw, structured={"matches": [{"name": "Яна Петрова", "phone": LEAKY_NUMBERS[1]}]}
        )
    )
    events = await collect(service, world.user, run_id)
    await asyncio.wait_for(device, 5)
    await service.wait_finished(run_id)
    assert "approval.requested" not in types(events)
    assert payload_of(events, "tool.call.completed")["status"] == "succeeded"
    assert dw.push.sent == [("device_tool_call", dw.phone)]
    no_phone_numbers(events)
