"""In-memory DeviceToolChannel: calls wait for ``deliver_result`` or the deadline."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from app.core.ports.device_tool_channel import (
    DeviceToolCall,
    DeviceToolDeclaration,
    DeviceToolResult,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class FakeDeviceToolChannel:
    def __init__(self, *, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now
        self._tools: dict[tuple[str, str], tuple[DeviceToolDeclaration, ...]] = {}
        self._pending: dict[str, tuple[str, str, asyncio.Future[DeviceToolResult]]] = {}
        self.sent: list[DeviceToolCall] = []
        """Calls that would have been emitted as ``device_tool.call`` events."""

    async def register(
        self, *, user_id: str, device_id: str, tools: Sequence[DeviceToolDeclaration]
    ) -> None:
        self._tools[(user_id, device_id)] = tuple(tools)

    async def available_tools(
        self, *, user_id: str, device_id: str
    ) -> Sequence[DeviceToolDeclaration]:
        return self._tools.get((user_id, device_id), ())

    async def invoke(self, *, user_id: str, call: DeviceToolCall) -> DeviceToolResult:
        declared = {tool.name: tool for tool in self._tools.get((user_id, call.device_id), ())}
        declaration = declared.get(call.tool)
        if declaration is None:
            return DeviceToolResult(
                call_id=call.call_id,
                status="failed",
                error_code="unsupported_tool",
                error_message="Tool is not declared by this device",
            )
        future: asyncio.Future[DeviceToolResult] = asyncio.get_running_loop().create_future()
        self._pending[call.call_id] = (user_id, call.device_id, future)
        self.sent.append(call)
        timeout = max(0.0, (call.deadline_at - self._now()).total_seconds())
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            return DeviceToolResult(
                call_id=call.call_id,
                status="failed" if declaration.risk == "read" else "uncertain",
                error_code="timeout",
                error_message="Device did not answer before the deadline",
            )
        finally:
            self._pending.pop(call.call_id, None)

    async def deliver_result(
        self, *, user_id: str, device_id: str, result: DeviceToolResult
    ) -> bool:
        pending = self._pending.get(result.call_id)
        if pending is None:
            return False
        owner, target_device, future = pending
        if owner != user_id or target_device != device_id or future.done():
            return False
        future.set_result(result)
        return True
