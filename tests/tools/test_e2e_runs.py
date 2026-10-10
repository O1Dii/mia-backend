"""End to end through the run engine: model → tool call → router → MCP → result → final.

Runs on the in-memory and the Postgres run store (``world`` fixture of M12);
every emitted event is validated against ``run-events.schema.json``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from app.modules.runs import NewRun, RunService

from tests.fakes import text_step, tool_step
from tests.runs.conftest import RunWorld, assert_well_formed, collect, payload_of, types, world
from tests.runs.gateways import answer, scripted
from tests.tools.helpers import INJECTION, PLUGIN_ID, build_world
from tests.tools.test_remote_http import PLUGIN as REMOTE_PLUGIN
from tests.tools.test_remote_http import remote_world

__all__ = ["world"]


async def run_to_end(
    service: RunService, user: uuid.UUID, profile: Any = "chat"
) -> list[dict[str, Any]]:
    new = NewRun(user_id=user, input_text="Поставь таймер", profile=profile)
    record, _ = await service.start(new)
    events = await collect(service, user, record.id)
    await service.wait_finished(record.id)
    assert_well_formed(events)
    return events


async def test_in_process_timer_through_the_engine(world: RunWorld, tmp_path: Path) -> None:
    tools = await build_world(tmp_path, user_id=str(world.user))
    gateway = scripted(
        tool_step("call_t1", f"{PLUGIN_ID}.start", {"seconds": 300}),
        text_step(answer("Таймер на пять минут запущен.")),
    )
    service = world.service(gateway, router=tools.router, approvals=tools.approvals)

    events = await run_to_end(service, world.user, profile="voice")

    assert types(events) == [
        "run.started",
        "tool.call.started",
        "tool.call.completed",
        "response.final",
        "run.completed",
    ]
    started = payload_of(events, "tool.call.started")
    assert started == {
        "tool_call_id": "call_t1",
        "tool": f"{PLUGIN_ID}.start",
        "risk": "write_reversible",
        "executor": "server",
        "plugin_id": PLUGIN_ID,
        "arguments": {"seconds": 300},
    }
    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "succeeded"
    assert completed["structured_content"] == {"timer": {"seconds": 300, "state": "running"}}
    assert completed["ui_resource_uri"] == "ui://test.timer/tile"
    final = payload_of(events, "response.final")["response"]
    assert final["speech"] == "Таймер на пять минут запущен."
    # The model saw the framed content only, never structured_content.
    tool_message = gateway.requests[1].messages[-1]
    body = json.loads(tool_message.content)
    assert body["status"] == "succeeded"
    assert body["content"].startswith('<tool_result tool="test.timer.start" trust="untrusted">')
    assert "running" not in tool_message.content
    assert tools.recorder.calls[0][2]["mia/user_id"] == str(world.user)
    # Voice offered only voice tools to the model.
    offered = {spec.name for spec in gateway.requests[0].tools}
    assert offered == {f"{PLUGIN_ID}.start", f"{PLUGIN_ID}.get"}


async def test_approval_round_trip_through_the_engine(world: RunWorld, tmp_path: Path) -> None:
    tools = await build_world(tmp_path, user_id=str(world.user))
    gateway = scripted(
        tool_step("call_s1", f"{PLUGIN_ID}.send", {"text": "Надо купить молоко"}),
        text_step(answer("Отправила.")),
    )
    service = world.service(gateway, router=tools.router, approvals=tools.approvals)
    record, _ = await service.start(NewRun(user_id=world.user, input_text="x", profile="chat"))

    async def approve_when_asked() -> None:
        async for event in service.events(user_id=world.user, run_id=record.id):
            if event is not None and event.type == "approval.requested":
                approval = event.to_wire()["payload"]["approval"]
                await tools.approvals.decide(
                    user_id=str(world.user),
                    approval_id=approval["id"],
                    decision="approve",
                    args_hash=approval["args_hash"],
                    channel="web",
                )
                return

    approver = asyncio.create_task(approve_when_asked())
    events = await collect(service, world.user, record.id)
    await asyncio.wait_for(approver, 5)
    await service.wait_finished(record.id)
    assert_well_formed(events)
    assert types(events) == [
        "run.started",
        "tool.call.started",
        "approval.requested",
        "approval.resolved",
        "tool.call.completed",
        "response.final",
        "run.completed",
    ]
    assert payload_of(events, "approval.resolved")["status"] == "approved"
    assert payload_of(events, "tool.call.completed")["status"] == "succeeded"
    assert tools.recorder.names() == ["send"]


async def test_prompt_injection_cannot_trigger_a_write(world: RunWorld, tmp_path: Path) -> None:
    tools = await build_world(tmp_path, user_id=str(world.user))
    gateway = scripted(
        tool_step("call_p1", f"{PLUGIN_ID}.poison", {}),
        text_step(answer("Готово.")),
    )
    service = world.service(gateway, router=tools.router, approvals=tools.approvals)

    events = await run_to_end(service, world.user)

    assert "approval.requested" not in types(events)
    assert tools.recorder.names() == ["poison"]
    content = json.loads(gateway.requests[1].messages[-1].content)["content"]
    assert content.count("</tool_result>") == 1 and INJECTION.splitlines()[-1] not in content


async def test_remote_http_plugin_through_the_engine(world: RunWorld) -> None:
    async with remote_world(user_id=str(world.user)) as remote:
        gateway = scripted(
            tool_step("call_r1", f"{REMOTE_PLUGIN}.start", {"seconds": 10}),
            text_step(answer("Готово.")),
        )
        service = world.service(gateway, router=remote.router, approvals=remote.approvals)
        events = await run_to_end(service, world.user)

    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "succeeded"
    assert completed["structured_content"] == {"timer": {"seconds": 10, "state": "running"}}
    assert remote.recorder.names() == ["start"]


async def test_uncertain_write_is_reported_and_not_retried(world: RunWorld, tmp_path: Path) -> None:
    tools = await build_world(tmp_path, user_id=str(world.user))
    gateway = scripted(
        tool_step("call_w1", f"{PLUGIN_ID}.slow_write", {}),
        text_step(answer("Не уверена, что получилось.")),
    )
    service = world.service(gateway, router=tools.router, approvals=tools.approvals)

    events = await run_to_end(service, world.user)

    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "uncertain"
    assert completed["error"]["code"] == "tool_timeout"
    assert tools.recorder.names() == ["slow_write"]
