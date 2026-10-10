"""In-process ``DeviceToolChannel``: run event out, client result back, deadline.

Flow (``device-tools.schema.json``): the router calls :meth:`invoke`; the
channel publishes a ``device_tool.call`` run event (``RunService.publish``),
the Android client executes the tool and posts the result to
``POST /v1/runs/{run_id}/device-tool-results`` (M23), whose handler calls
:meth:`deliver_result`. A missed deadline yields ``failed``/``timeout`` for
``read`` tools and ``uncertain``/``timeout`` for writes; late results are
dropped. Pending calls live in this process (single instance, OI-S18-002).

M70 (ADR-0016): a call for a device that does not follow the run's stream (a run from the
web, the app's WebView or another device) has ``push`` delivery: it is also listed by
``GET /v1/devices/{device_id}/tool-calls`` and announced to the device by a push; an
unreachable device fails at once with ``offline``. Declarations are persisted through
:class:`~app.modules.tools.device_access.DeviceAccess` so other clients see the phone's
tools after a restart. Results lose phone numbers before anyone else sees them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from app.core.ports.device_tool_channel import (
    DeviceDelivery,
    DeviceToolCall,
    DeviceToolDeclaration,
    DeviceToolResult,
)
from app.modules.tools.device_policy import minimize_device_result

if TYPE_CHECKING:
    from app.modules.tools.device_access import DeviceAccess

logger = logging.getLogger(__name__)

MAX_DEVICE_TOOLS = 64
PUSH_DELIVERY_SLACK = timedelta(seconds=20)
"""Extra time for a push-delivered call: the phone wakes, fetches the call, runs it."""


class RunEventPublisher(Protocol):
    """``RunService.publish`` (M12)."""

    async def __call__(
        self, *, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
    ) -> Any: ...


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def call_body(call: DeviceToolCall) -> dict[str, Any]:
    """``DeviceToolCall`` (``device-tools.schema.json``)."""
    body: dict[str, Any] = {
        "call_id": call.call_id,
        "run_id": call.run_id,
        "device_id": call.device_id,
        "tool": call.tool,
        "arguments": dict(call.arguments),
        "deadline_at": _isoformat(call.deadline_at),
    }
    if call.approval_id is not None:
        body["approval_id"] = call.approval_id
    return body


def call_payload(call: DeviceToolCall) -> dict[str, Any]:
    """``device_tool.call`` event payload (``run-events.schema.json``)."""
    return {"call": call_body(call)}


@dataclass(slots=True)
class _Pending:
    run_id: str
    call: DeviceToolCall
    delivery: DeviceDelivery
    future: asyncio.Future[DeviceToolResult]


@dataclass(frozen=True, slots=True)
class PendingCall:
    run_id: str
    device_id: str
    delivery: DeviceDelivery


def _minimized(tool: str, result: DeviceToolResult) -> DeviceToolResult:
    content, structured = minimize_device_result(tool, result.content, result.structured_content)
    message = result.error_message
    if message is not None:
        message, _ = minimize_device_result(tool, message, None)
    return dataclasses.replace(
        result, content=content, structured_content=structured, error_message=message
    )


class InMemoryDeviceToolChannel:
    def __init__(
        self,
        publisher: RunEventPublisher | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        access: DeviceAccess | None = None,
    ) -> None:
        self._publisher = publisher
        self._clock = clock
        self._access = access
        self._tools: dict[tuple[str, str], dict[str, DeviceToolDeclaration]] = {}
        self._pending: dict[tuple[str, str, str], _Pending] = {}

    def bind_publisher(self, publisher: RunEventPublisher) -> None:
        """Late binding: the run service is created after the router (composition)."""
        self._publisher = publisher

    def bind_access(self, access: DeviceAccess) -> None:
        self._access = access

    async def register(
        self, *, user_id: str, device_id: str, tools: Sequence[DeviceToolDeclaration]
    ) -> None:
        if len(tools) > MAX_DEVICE_TOOLS:
            raise ValueError("too many device tools")
        self._tools[(user_id, device_id)] = {tool.name: tool for tool in tools}
        access = self._access
        if access is None:
            return
        access.seen(user_id=user_id, device_id=device_id)
        try:
            await access.store.save_declarations(
                user_id=user_id, device_id=device_id, tools=tools, now=self._clock()
            )
        except Exception:
            logger.warning("device_declarations_save_failed")

    async def available_tools(
        self, *, user_id: str, device_id: str
    ) -> Sequence[DeviceToolDeclaration]:
        known = self._tools.get((user_id, device_id))
        if known is not None:
            return list(known.values())
        access = self._access
        if access is None:
            return []
        try:
            stored = await access.store.declarations(user_id=user_id)
        except Exception:
            logger.warning("device_declarations_lookup_failed")
            return []
        for item in stored:
            if item.device_id == device_id:
                self._tools[(user_id, device_id)] = {tool.name: tool for tool in item.tools}
                return list(item.tools)
        return []

    async def _delivery(self, user_id: str, call: DeviceToolCall) -> DeviceDelivery:
        if call.delivery is not None:
            return call.delivery
        access = self._access
        if access is None:
            return "stream"
        return await access.delivery(user_id=user_id, run_id=call.run_id, device_id=call.device_id)

    async def invoke(self, *, user_id: str, call: DeviceToolCall) -> DeviceToolResult:
        declared = {
            tool.name: tool
            for tool in await self.available_tools(user_id=user_id, device_id=call.device_id)
        }
        declaration = declared.get(call.tool)
        if declaration is None:
            return DeviceToolResult(
                call_id=call.call_id,
                status="failed",
                error_code="unsupported_tool",
                error_message="The device does not declare this tool.",
            )
        if self._publisher is None:
            return DeviceToolResult(
                call_id=call.call_id,
                status="failed",
                error_code="execution_failed",
                error_message="No run stream is available.",
            )
        delivery = await self._delivery(user_id, call)
        access = self._access
        if delivery == "push":
            if access is None or not await access.reachable(
                user_id=user_id, device_id=call.device_id
            ):
                return DeviceToolResult(
                    call_id=call.call_id,
                    status="failed",
                    error_code="offline",
                    error_message="The phone is not reachable.",
                )
            call = dataclasses.replace(
                call, delivery="push", deadline_at=call.deadline_at + PUSH_DELIVERY_SLACK
            )
        key = (user_id, call.device_id, call.call_id)
        if key in self._pending:
            return DeviceToolResult(
                call_id=call.call_id,
                status="failed",
                error_code="execution_failed",
                error_message="The call is already in flight.",
            )
        future: asyncio.Future[DeviceToolResult] = asyncio.get_running_loop().create_future()
        self._pending[key] = _Pending(
            run_id=call.run_id, call=call, delivery=delivery, future=future
        )
        try:
            await self._publisher(
                run_id=uuid.UUID(call.run_id),
                event_type="device_tool.call",
                payload=call_payload(call),
            )
            if delivery == "push" and access is not None:
                await access.call_waiting(user_id=user_id, call=call)
            timeout = max(0.0, (call.deadline_at - self._clock()).total_seconds())
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except TimeoutError:
                return DeviceToolResult(
                    call_id=call.call_id,
                    status="failed" if declaration.risk == "read" else "uncertain",
                    error_code="timeout",
                    error_message="The device did not answer in time.",
                )
            return _minimized(call.tool, result)
        finally:
            self._pending.pop(key, None)
            if not future.done():
                future.cancel()

    async def deliver_result(
        self, *, user_id: str, device_id: str, result: DeviceToolResult
    ) -> bool:
        pending = self._pending.get((user_id, device_id, result.call_id))
        if pending is None or pending.future.done():
            logger.info("device_tool_result_dropped")
            return False
        if self._access is not None:
            self._access.seen(user_id=user_id, device_id=device_id)
        pending.future.set_result(result)
        return True

    def pending_run(self, *, user_id: str, device_id: str, call_id: str) -> str | None:
        """Run id of a pending call (M23 checks the URL's run id against it)."""
        pending = self._pending.get((user_id, device_id, call_id))
        return pending.run_id if pending is not None else None

    def pending_call(self, *, user_id: str, call_id: str) -> PendingCall | None:
        """The pending call with this id for any of the user's devices."""
        for (owner, device_id, pending_id), pending in self._pending.items():
            if owner == user_id and pending_id == call_id and not pending.future.done():
                return PendingCall(pending.run_id, device_id, pending.delivery)
        return None

    def pending_for_device(self, *, user_id: str, device_id: str) -> list[DeviceToolCall]:
        """Push-delivered calls waiting for this device (``GET /v1/devices/{id}/tool-calls``)."""
        if self._access is not None:
            self._access.seen(user_id=user_id, device_id=device_id)
        return [
            pending.call
            for (owner, device, _), pending in self._pending.items()
            if owner == user_id
            and device == device_id
            and pending.delivery == "push"
            and not pending.future.done()
        ]
