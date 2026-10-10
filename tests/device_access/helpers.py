"""A router world with device access (M70): in-memory store, fake push, a fake phone."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.ports.approval_service import Approval
from app.core.ports.device_tool_channel import (
    DeviceToolCall,
    DeviceToolDeclaration,
    DeviceToolResult,
)
from app.modules.tools.device_access import DeviceAccess
from app.modules.tools.device_store import InMemoryDeviceAccessStore, RunOrigin

from tests.tools.helpers import World, build_world

PHONE_NAME = "Galaxy S25"
CONTACTS = DeviceToolDeclaration(
    name="device.contacts.search",
    description="Search the phone's contacts by name.",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1}},
        "required": ["query"],
        "additionalProperties": False,
    },
    risk="read",
    voice=True,
    requires_unlocked=True,
    timeout_ms=2000,
)
ALARM = DeviceToolDeclaration(
    name="device.alarm.set",
    description="Set an alarm in the phone's Clock app.",
    input_schema={
        "type": "object",
        "properties": {"at": {"type": "string"}, "label": {"type": "string"}},
        "required": ["at"],
    },
    risk="write_reversible",
    voice=True,
    requires_unlocked=False,
    timeout_ms=2000,
)
HANDOFF = DeviceToolDeclaration(
    name="telegram.handoff",
    description="Open Telegram with a ready text.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}, "contact_name": {"type": "string"}},
        "required": ["text"],
    },
    risk="write_reversible",
    voice=False,
    requires_unlocked=True,
    timeout_ms=2000,
)
DECLARED = (CONTACTS, ALARM, HANDOFF)
LEAKY_NUMBERS = ("+375 29 123-45-67", "80291234567", "+375297654321")


class FakePush:
    """``DeviceNotifier``: records pushes, reachable unless told otherwise."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.is_reachable = reachable
        self.consents: asyncio.Queue[Approval] = asyncio.Queue()
        self.calls: asyncio.Queue[DeviceToolCall] = asyncio.Queue()
        self.sent: list[tuple[str, str]] = []

    async def reachable(self, *, user_id: str, device_id: str) -> bool:
        return self.is_reachable

    async def consent_requested(self, *, user_id: str, device_id: str, approval: Approval) -> bool:
        self.sent.append(("device_consent", device_id))
        await self.consents.put(approval)
        return True

    async def call_waiting(self, *, user_id: str, device_id: str, call: DeviceToolCall) -> bool:
        self.sent.append(("device_tool_call", device_id))
        await self.calls.put(call)
        return True


class Events:
    """``RunService.publish`` stand-in."""

    def __init__(self) -> None:
        self.items: list[tuple[uuid.UUID, str, Mapping[str, Any]]] = []

    async def __call__(
        self, *, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        self.items.append((run_id, event_type, payload))


@dataclass
class DeviceWorld:
    world: World
    store: InMemoryDeviceAccessStore
    access: DeviceAccess
    push: FakePush
    events: Events
    phone: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def user_id(self) -> str:
        return self.world.user_id

    def origin(
        self,
        run_id: str,
        kind: str = "web",
        *,
        device_id: str | None = None,
        session: str | None = None,
        label: str = "браузер Chrome на macOS",
    ) -> RunOrigin:
        session = session or (str(uuid.uuid4()) if kind in ("web", "device_webview") else None)
        if session is not None:
            self.store.web_sessions[session] = (self.user_id, device_id, True)
        origin = RunOrigin(run_id, self.user_id, kind, label, device_id, session)  # type: ignore[arg-type]
        self.access.remember(origin)
        return origin


async def device_world(
    tmp_path: Path,
    *,
    user_id: str | None = None,
    reachable: bool = True,
    consent_wait_s: int = 120,
    live: bool = False,
) -> DeviceWorld:
    world = await build_world(tmp_path, user_id=user_id)
    store = InMemoryDeviceAccessStore()
    push = FakePush(reachable=reachable)
    access = DeviceAccess(store, notifier=push, clock=world.clock, consent_wait_s=consent_wait_s)
    events = Events()
    world.channel.bind_access(access)
    world.channel.bind_publisher(events)
    world.router.bind_device_access(access)
    result = DeviceWorld(world, store, access, push, events)
    store.add_device(world.user_id, result.phone, PHONE_NAME)
    await store.save_declarations(
        user_id=world.user_id, device_id=result.phone, tools=DECLARED, now=world.clock()
    )
    if live:
        access.seen(user_id=world.user_id, device_id=result.phone)
    return result


async def answer_next_call(
    dw: DeviceWorld,
    *,
    content: str = "Found 1 contact: Яна Петрова",
    structured: dict[str, Any] | None = None,
    status: str = "succeeded",
    error: str | None = None,
) -> DeviceToolCall:
    """The phone: woken by the push, it reads its queue and posts the result."""
    announced = await asyncio.wait_for(dw.push.calls.get(), 5)
    queued = dw.world.channel.pending_for_device(user_id=dw.user_id, device_id=dw.phone)
    assert [call.call_id for call in queued] == [announced.call_id]
    call = queued[0]
    result = DeviceToolResult(
        call.call_id,
        status,  # type: ignore[arg-type]
        content=content if status == "succeeded" else "",
        structured_content=structured,
        error_code=error,  # type: ignore[arg-type]
        error_message="x" if error else None,
    )
    assert await dw.world.channel.deliver_result(
        user_id=dw.user_id, device_id=dw.phone, result=result
    )
    return call
