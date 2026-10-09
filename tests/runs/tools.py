"""Tool descriptors and handlers for run tests (on top of ``tests.fakes.FakeToolRouter``)."""

from __future__ import annotations

from app.core.ports.model_gateway import ToolSpec
from app.core.ports.tool_router import ToolCallRequest, ToolContext, ToolDescriptor, ToolOutcome
from app.core.ports.types import Risk

from tests.fakes import FakeApprovalService, FakeToolRouter


def descriptor(
    name: str, *, risk: Risk = "read", voice: bool = True, plugin_id: str | None = None
) -> ToolDescriptor:
    return ToolDescriptor(
        spec=ToolSpec(
            name=name,
            description=f"Test tool {name}",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        ),
        risk=risk,
        executor="server",
        voice=voice,
        plugin_id=plugin_id,
    )


async def echo_handler(context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
    return ToolOutcome(
        call_id=request.call_id,
        status="succeeded",
        content=f"result for {request.arguments.get('q', '')}",
        structured_content={"items": [1, 2, 3], "secret_for_ui_only": True},
        ui_resource_uri="ui://test.tools/list",
    )


async def failing_handler(context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
    raise RuntimeError("tool internals must not leak")


def router_with(
    *descriptors: ToolDescriptor, approvals: FakeApprovalService | None = None
) -> tuple[FakeToolRouter, FakeApprovalService]:
    service = approvals or FakeApprovalService()
    router = FakeToolRouter(service)
    for item in descriptors:
        router.register(item, echo_handler)
    return router, service
