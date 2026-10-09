"""In-process ``DeviceToolChannel``: run event out, client result back, deadline.

Flow (``device-tools.schema.json``): the router calls :meth:`invoke`; the
channel publishes a ``device_tool.call`` run event (``RunService.publish``),
the Android client executes the tool and posts the result to
``POST /v1/runs/{run_id}/device-tool-results`` (M23), whose handler calls
:meth:`deliver_result`. A missed deadline yields ``failed``/``timeout`` for
``read`` tools and ``uncertain``/``timeout`` for writes; late results are
dropped. Pending calls live in this process (single instance, OI-S18-002).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.ports.device_tool_channel import (
    DeviceToolCall,
    DeviceToolDeclaration,
    DeviceToolResult,
)

logger = logging.getLogger(__name__)

MAX_DEVICE_TOOLS = 64


class RunEventPublisher(Protocol):
    """``RunService.publish`` (M12)."""

    async def __call__(
        self, *, run_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
    ) -> Any: ...


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def call_payload(call: DeviceToolCall) -> dict[str, Any]:
    """``device_tool.call`` event payload (``run-events.schema.json``)."""
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
    return {"call": body}


@dataclass(slots=True)
class _Pending:
    run_id: str
    future: asyncio.Future[DeviceToolResult]


class InMemoryDeviceToolChannel:
    def __init__(
        self,
        publisher: RunEventPublisher | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._publisher = publisher
        self._clock = clock
        self._tools: dict[tuple[str, str], dict[str, DeviceToolDeclaration]] = {}
        self._pending: dict[tuple[str, str, str], _Pending] = {}

    def bind_publisher(self, publisher: RunEventPublisher) -> None:
        """Late binding: the run service is created after the router (composition)."""
        self._publisher = publisher

    async def register(
        self, *, user_id: str, device_id: str, tools: Sequence[DeviceToolDeclaration]
    ) -> None:
        if len(tools) > MAX_DEVICE_TOOLS:
            raise ValueError("too many device tools")
        self._tools[(user_id, device_id)] = {tool.name: tool for tool in tools}

    async def available_tools(
        self, *, user_id: str, device_id: str
    ) -> Sequence[DeviceToolDeclaration]:
        return list(self._tools.get((user_id, device_id), {}).values())

    async def invoke(self, *, user_id: str, call: DeviceToolCall) -> DeviceToolResult:
        declaration = self._tools.get((user_id, call.device_id), {}).get(call.tool)
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
        key = (user_id, call.device_id, call.call_id)
        if key in self._pending:
            return DeviceToolResult(
                call_id=call.call_id,
                status="failed",
                error_code="execution_failed",
                error_message="The call is already in flight.",
            )
        future: asyncio.Future[DeviceToolResult] = asyncio.get_running_loop().create_future()
        self._pending[key] = _Pending(run_id=call.run_id, future=future)
        try:
            await self._publisher(
                run_id=uuid.UUID(call.run_id),
                event_type="device_tool.call",
                payload=call_payload(call),
            )
            timeout = max(0.0, (call.deadline_at - self._clock()).total_seconds())
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except TimeoutError:
                return DeviceToolResult(
                    call_id=call.call_id,
                    status="failed" if declaration.risk == "read" else "uncertain",
                    error_code="timeout",
                    error_message="The device did not answer in time.",
                )
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
        pending.future.set_result(result)
        return True

    def pending_run(self, *, user_id: str, device_id: str, call_id: str) -> str | None:
        """Run id of a pending call (M23 checks the URL's run id against it)."""
        pending = self._pending.get((user_id, device_id, call_id))
        return pending.run_id if pending is not None else None
