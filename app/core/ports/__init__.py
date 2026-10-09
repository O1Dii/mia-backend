"""Core ports of the MVP (owner: M00, contracts/mvp/v1).

Protocols here are the only coupling between the run engine and the modules
that implement models, tools, memory, device tools and approvals. Changes are
additive and recorded in the handoff of the session that makes them.
"""

from app.core.ports.approval_service import (
    Approval,
    ApprovalDecision,
    ApprovalError,
    ApprovalRequest,
    ApprovalService,
    canonical_args_hash,
)
from app.core.ports.device_tool_channel import (
    DeviceToolCall,
    DeviceToolChannel,
    DeviceToolDeclaration,
    DeviceToolResult,
)
from app.core.ports.memory_provider import (
    FactRecord,
    MemoryProvider,
    MemorySource,
    PersonRef,
)
from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelGateway,
    ModelGatewayError,
    ModelInfo,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    ModelUsage,
    TextDelta,
    ToolCallRequested,
    ToolSpec,
)
from app.core.ports.tool_router import (
    ToolCallRequest,
    ToolContext,
    ToolDescriptor,
    ToolOutcome,
    ToolRouter,
)
from app.core.ports.types import DEFAULT_MODEL, JsonObject, PortError, Risk, RunProfile

__all__ = [
    "DEFAULT_MODEL",
    "Approval",
    "ApprovalDecision",
    "ApprovalError",
    "ApprovalRequest",
    "ApprovalService",
    "DeviceToolCall",
    "DeviceToolChannel",
    "DeviceToolDeclaration",
    "DeviceToolResult",
    "FactRecord",
    "JsonObject",
    "MemoryProvider",
    "MemorySource",
    "ModelDone",
    "ModelEvent",
    "ModelGateway",
    "ModelGatewayError",
    "ModelInfo",
    "ModelMessage",
    "ModelRequest",
    "ModelToolCall",
    "ModelUsage",
    "PersonRef",
    "PortError",
    "Risk",
    "RunProfile",
    "TextDelta",
    "ToolCallRequest",
    "ToolCallRequested",
    "ToolContext",
    "ToolDescriptor",
    "ToolOutcome",
    "ToolRouter",
    "ToolSpec",
    "canonical_args_hash",
]
