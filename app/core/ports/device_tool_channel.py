"""DeviceToolChannel port: tools executed on the user's phone (Review §5.4.2, §7.5).

Schema: ``contracts/mvp/v1/device-tools.schema.json``. Calls travel to the
device as ``device_tool.call`` run events; results return through
``POST /v1/runs/{run_id}/device-tool-results`` and ``deliver_result``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from app.core.ports.types import JsonObject, Risk

DeviceToolStatus = Literal["succeeded", "failed", "uncertain"]
DeviceToolErrorCode = Literal[
    "unsupported_tool",
    "invalid_arguments",
    "os_permission_denied",
    "device_locked",
    "user_cancelled",
    "timeout",
    "execution_failed",
]


@dataclass(frozen=True, slots=True)
class DeviceToolDeclaration:
    name: str
    """Namespaced name, e.g. ``device.contacts.search``, ``telegram.handoff``."""
    description: str
    input_schema: JsonObject
    risk: Risk
    voice: bool
    requires_unlocked: bool
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class DeviceToolCall:
    call_id: str
    run_id: str
    device_id: str
    tool: str
    arguments: JsonObject
    deadline_at: datetime
    approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceToolResult:
    call_id: str
    status: DeviceToolStatus
    content: str = ""
    structured_content: JsonObject | None = None
    error_code: DeviceToolErrorCode | None = None
    error_message: str | None = None
    retryable: bool = False


@runtime_checkable
class DeviceToolChannel(Protocol):
    async def register(
        self, *, user_id: str, device_id: str, tools: Sequence[DeviceToolDeclaration]
    ) -> None:
        """Replace the device's declared tool set."""
        ...

    async def available_tools(
        self, *, user_id: str, device_id: str
    ) -> Sequence[DeviceToolDeclaration]: ...

    async def invoke(self, *, user_id: str, call: DeviceToolCall) -> DeviceToolResult:
        """Send the call to the device and wait until ``deadline_at``.

        An undeclared tool returns ``failed``/``unsupported_tool`` without
        sending. A missed deadline returns ``failed``/``timeout`` for ``read``
        tools and ``uncertain``/``timeout`` for write tools."""
        ...

    async def deliver_result(
        self, *, user_id: str, device_id: str, result: DeviceToolResult
    ) -> bool:
        """Complete a pending call. Returns False for unknown, foreign or
        already completed calls (late results are dropped, not applied)."""
        ...
