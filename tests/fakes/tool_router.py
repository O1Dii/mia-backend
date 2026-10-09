"""In-memory ToolRouter that applies the ToolRouter contract's policy rules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

from app.core.ports.approval_service import (
    ApprovalRequest,
    ApprovalService,
    canonical_args_hash,
)
from app.core.ports.device_tool_channel import DeviceToolCall, DeviceToolChannel
from app.core.ports.tool_router import (
    ToolCallRequest,
    ToolContext,
    ToolDescriptor,
    ToolOutcome,
)

ToolHandler = Callable[[ToolContext, ToolCallRequest], Awaitable[ToolOutcome]]


def utc_now() -> datetime:
    return datetime.now(UTC)


class FakeToolRouter:
    def __init__(
        self,
        approvals: ApprovalService,
        *,
        devices: DeviceToolChannel | None = None,
        device_timeout: timedelta = timedelta(seconds=5),
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._approvals = approvals
        self._devices = devices
        self._device_timeout = device_timeout
        self._now = now
        self._tools: dict[str, tuple[ToolDescriptor, ToolHandler | None]] = {}
        self._consumed_approvals: set[str] = set()
        self.executed: list[ToolCallRequest] = []

    def register(self, descriptor: ToolDescriptor, handler: ToolHandler | None = None) -> None:
        """Server tools need a handler; device tools are dispatched to ``devices``."""
        if descriptor.executor == "server" and handler is None:
            raise ValueError("server tools need a handler")
        self._tools[descriptor.spec.name] = (descriptor, handler)

    async def list_tools(self, context: ToolContext) -> Sequence[ToolDescriptor]:
        return [
            descriptor for descriptor, _ in self._tools.values() if _visible(context, descriptor)
        ]

    async def call(self, context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
        entry = self._tools.get(request.tool)
        if entry is None or not _visible(context, entry[0]):
            return _failed(request, "tool_not_found")
        descriptor, handler = entry
        required = descriptor.spec.input_schema.get("required", [])
        if isinstance(required, list) and any(key not in request.arguments for key in required):
            return _failed(request, "tool_invalid_arguments")

        if descriptor.risk == "write_external":
            gate = await self._approval_gate(context, request, descriptor)
            if gate is not None:
                return gate

        self.executed.append(request)
        if descriptor.executor == "device":
            return await self._call_device(context, request)
        assert handler is not None
        try:
            return await handler(context, request)
        except Exception:
            return _failed(request, "tool_execution_failed")

    async def _approval_gate(
        self, context: ToolContext, request: ToolCallRequest, descriptor: ToolDescriptor
    ) -> ToolOutcome | None:
        if request.approval_id is None:
            pending = await self._approvals.request(
                ApprovalRequest(
                    user_id=context.user_id,
                    run_id=context.run_id,
                    tool_call_id=request.call_id,
                    tool=request.tool,
                    risk=descriptor.risk,
                    summary=f"{descriptor.spec.description}: {request.tool}",
                    arguments=request.arguments,
                    plugin_id=descriptor.plugin_id,
                )
            )
            return ToolOutcome(
                call_id=request.call_id, status="approval_required", approval=pending
            )
        approval = await self._approvals.get(
            user_id=context.user_id, approval_id=request.approval_id
        )
        if approval is None or request.approval_id in self._consumed_approvals:
            return ToolOutcome(
                call_id=request.call_id, status="denied", error_code="approval_invalid"
            )
        if approval.status == "expired":
            return ToolOutcome(
                call_id=request.call_id, status="expired", error_code="approval_expired"
            )
        if (
            approval.status != "approved"
            or approval.tool != request.tool
            or approval.args_hash != canonical_args_hash(request.arguments)
        ):
            return ToolOutcome(
                call_id=request.call_id, status="denied", error_code="approval_invalid"
            )
        self._consumed_approvals.add(request.approval_id)
        return None

    async def _call_device(self, context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
        if self._devices is None or context.device_id is None:
            return _failed(request, "device_unavailable")
        result = await self._devices.invoke(
            user_id=context.user_id,
            call=DeviceToolCall(
                call_id=request.call_id,
                run_id=context.run_id,
                device_id=context.device_id,
                tool=request.tool,
                arguments=request.arguments,
                deadline_at=self._now() + self._device_timeout,
                approval_id=request.approval_id,
            ),
        )
        return ToolOutcome(
            call_id=request.call_id,
            status=result.status,
            content=result.content,
            structured_content=result.structured_content,
            error_code=result.error_code,
        )


def _visible(context: ToolContext, descriptor: ToolDescriptor) -> bool:
    if context.profile == "voice":
        return descriptor.voice
    if context.profile == "app":
        return descriptor.plugin_id is not None and (
            f"tools.use:{descriptor.plugin_id}" in context.scopes
        )
    return True


def _failed(request: ToolCallRequest, code: str) -> ToolOutcome:
    return ToolOutcome(call_id=request.call_id, status="failed", error_code=code)
