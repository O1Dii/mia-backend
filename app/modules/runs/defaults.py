"""Placeholders used until the real implementations are wired (M11 gateway, M13 router)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from app.core.ports.model_gateway import ModelEvent, ModelGatewayError, ModelInfo, ModelRequest
from app.core.ports.tool_router import ToolCallRequest, ToolContext, ToolDescriptor, ToolOutcome
from app.core.ports.types import DEFAULT_MODEL


class UnconfiguredModelGateway:
    """Every run fails cleanly with ``model_not_configured`` until M11 is wired."""

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        raise ModelGatewayError("model_not_configured", "No model gateway is configured")

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise ModelGatewayError("model_not_configured", "No model gateway is configured")
        yield  # pragma: no cover - makes this an async generator


class NoToolRouter:
    """A router without tools: runs answer from the model alone until M13 is wired."""

    async def list_tools(self, context: ToolContext) -> Sequence[ToolDescriptor]:
        return ()

    async def call(self, context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
        return ToolOutcome(call_id=request.call_id, status="failed", error_code="tool_not_found")
