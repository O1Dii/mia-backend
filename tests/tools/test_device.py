"""Device tools: router → DeviceToolChannel → run event → client result → deadline."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from app.core.ports.device_tool_channel import DeviceToolDeclaration, DeviceToolResult
from app.core.ports.tool_router import ToolCallRequest

from tests.fakes import assert_mvp_valid
from tests.tools.helpers import World, build_world

DEVICE = str(uuid.uuid4())


def declaration(
    name: str, *, risk: Any = "read", voice: bool = True, timeout_ms: int = 300
) -> DeviceToolDeclaration:
    return DeviceToolDeclaration(
        name=name,
        description="Search the phone contacts.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 80}},
            "required": ["query"],
        },
        risk=risk,
        voice=voice,
        requires_unlocked=False,
        timeout_ms=timeout_ms,
    )


class Publisher:
    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, str, dict[str, Any]]] = []
        self.published = asyncio.Event()

    async def __call__(
        self, *, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        self.events.append((run_id, event_type, dict(payload)))
        self.published.set()


@pytest.fixture
async def world(tmp_path: Path) -> World:
    world = await build_world(tmp_path)
    await world.channel.register(
        user_id=world.user_id,
        device_id=DEVICE,
        tools=[
            declaration("device.contacts.search"),
            declaration("telegram.share", risk="write_external", voice=False),
            declaration("device.alarm.set", risk="write_reversible"),
        ],
    )
    return world


async def test_device_tools_are_listed_for_the_runs_device_only(world: World) -> None:
    with_device = {
        d.spec.name: d for d in await world.router.list_tools(world.context(device_id=DEVICE))
    }
    assert with_device["device.contacts.search"].executor == "device"
    assert "telegram.share" in with_device
    voice = {
        d.spec.name for d in await world.router.list_tools(world.context("voice", device_id=DEVICE))
    }
    assert "telegram.share" not in voice and "device.contacts.search" in voice
    without = {d.spec.name for d in await world.router.list_tools(world.context())}
    assert not any(name.startswith("device.") for name in without)
    job = {d.spec.name for d in await world.router.list_tools(world.context("job"))}
    assert not any(name.startswith("device.") for name in job)


async def test_call_goes_out_as_run_event_and_returns_the_client_result(world: World) -> None:
    publisher = Publisher()
    world.channel.bind_publisher(publisher)
    context = world.context(device_id=DEVICE)
    request = ToolCallRequest("call_dev1", "device.contacts.search", {"query": "Яна"})
    task = asyncio.create_task(world.router.call(context, request))
    await asyncio.wait_for(publisher.published.wait(), 2)

    run_id, event_type, payload = publisher.events[0]
    assert (str(run_id), event_type) == (context.run_id, "device_tool.call")
    assert payload["call"]["device_id"] == DEVICE and payload["call"]["arguments"] == {
        "query": "Яна"
    }
    assert_mvp_valid(
        {
            "event_id": str(uuid.uuid4()),
            "run_id": context.run_id,
            "sequence": 2,
            "cursor": "c2",
            "type": "device_tool.call",
            "at": "2026-10-09T12:00:00Z",
            "payload": payload,
        },
        "run-events",
    )
    assert world.channel.pending_run(user_id=world.user_id, device_id=DEVICE, call_id="call_dev1")
    # A result for another user or device is not applied.
    assert not await world.channel.deliver_result(
        user_id=str(uuid.uuid4()),
        device_id=DEVICE,
        result=DeviceToolResult("call_dev1", "succeeded", content="x"),
    )
    assert await world.channel.deliver_result(
        user_id=world.user_id,
        device_id=DEVICE,
        result=DeviceToolResult(
            "call_dev1",
            "succeeded",
            content="Яна Петрова",
            structured_content={"matches": [{"name": "Яна Петрова", "phone_count": 1}]},
        ),
    )
    outcome = await asyncio.wait_for(task, 2)
    assert outcome.status == "succeeded" and "Яна Петрова" in outcome.content
    assert outcome.content.startswith("<tool_result") and outcome.structured_content == {
        "matches": [{"name": "Яна Петрова", "phone_count": 1}]
    }
    # Late duplicate results are dropped.
    assert not await world.channel.deliver_result(
        user_id=world.user_id,
        device_id=DEVICE,
        result=DeviceToolResult("call_dev1", "succeeded", content="again"),
    )


async def test_deadline_fails_reads_and_makes_writes_uncertain(world: World) -> None:
    world.channel.bind_publisher(Publisher())
    context = world.context(device_id=DEVICE)
    read = await world.router.call(
        context, ToolCallRequest("c_read", "device.contacts.search", {"query": "a"})
    )
    assert (read.status, read.error_code) == ("failed", "device_timeout")
    write = await world.router.call(
        context, ToolCallRequest("c_write", "device.alarm.set", {"query": "7:00"})
    )
    assert (write.status, write.error_code) == ("uncertain", "device_timeout")


async def test_external_device_write_needs_approval_and_device_errors_map(world: World) -> None:
    publisher = Publisher()
    world.channel.bind_publisher(publisher)
    context = world.context(device_id=DEVICE)
    pending = await world.router.call(
        context, ToolCallRequest("c_tg", "telegram.share", {"query": "hi"})
    )
    assert pending.status == "approval_required" and publisher.events == []
    assert pending.approval is not None
    await world.approvals.decide(
        user_id=world.user_id,
        approval_id=pending.approval.id,
        decision="approve",
        args_hash=pending.approval.args_hash,
        channel="android",
    )
    task = asyncio.create_task(
        world.router.call(
            context,
            ToolCallRequest("c_tg", "telegram.share", {"query": "hi"}, pending.approval.id),
        )
    )
    await asyncio.wait_for(publisher.published.wait(), 2)
    assert publisher.events[0][2]["call"]["approval_id"] == pending.approval.id
    await world.channel.deliver_result(
        user_id=world.user_id,
        device_id=DEVICE,
        result=DeviceToolResult("c_tg", "failed", error_code="device_locked"),
    )
    outcome = await asyncio.wait_for(task, 2)
    assert (outcome.status, outcome.error_code) == ("failed", "device_locked")


async def test_undeclared_tool_is_not_sent(world: World) -> None:
    publisher = Publisher()
    world.channel.bind_publisher(publisher)
    outcome = await world.router.call(
        world.context(device_id=DEVICE), ToolCallRequest("c", "device.camera.open", {})
    )
    assert outcome.error_code == "tool_not_found" and publisher.events == []
