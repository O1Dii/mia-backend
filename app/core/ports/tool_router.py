"""ToolRouter port: one entry point for every tool call (Review §5.3, §5.4).

The router owns lookup, argument validation, grants/scopes, risk policy,
budgets and dispatch to builtin, remote MCP, declarative and device tools.
The run engine (M12) only sees this protocol; M13 implements it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from app.core.ports.approval_service import Approval
from app.core.ports.model_gateway import ToolSpec
from app.core.ports.types import JsonObject, Risk, RunProfile

ToolExecutor = Literal["server", "device"]
ToolCallStatus = Literal[
    "succeeded",
    "failed",
    "approval_required",
    "denied",
    "expired",
    "uncertain",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class ToolContext:
    user_id: str
    run_id: str
    profile: RunProfile
    workspace_id: str | None = None
    device_id: str | None = None
    """Device that started the run; target of device tools."""
    app_client_id: str | None = None
    """Set for profile ``app``: the OAuth client acting for the user."""
    scopes: frozenset[str] = field(default_factory=frozenset)
    """Granted OAuth scopes for profile ``app`` (e.g. ``tools.use:team.linear``)."""


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    spec: ToolSpec
    risk: Risk
    executor: ToolExecutor
    voice: bool
    plugin_id: str | None = None
    """None for core tools (``background.*``, ``memory.*``) and device tools."""


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    call_id: str
    tool: str
    arguments: JsonObject
    approval_id: str | None = None
    """Approved approval to execute against; required for ``write_external``."""


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    call_id: str
    status: ToolCallStatus
    content: str = ""
    """Compact summary for the model (<= ~1-2k tokens), Review §5.2(c)."""
    structured_content: JsonObject | None = None
    """Full data for cards/MCP Apps; never sent to the model."""
    ui_resource_uri: str | None = None
    approval: Approval | None = None
    """Pending approval when ``status == "approval_required"``."""
    error_code: str | None = None


@runtime_checkable
class ToolRouter(Protocol):
    async def list_tools(self, context: ToolContext) -> Sequence[ToolDescriptor]:
        """Tools visible in this run. Profile ``voice`` returns only tools marked
        ``voice``; profile ``app`` only tools covered by ``scopes``."""
        ...

    async def call(self, context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
        """Execute one call through policy.

        - unknown tool or invalid arguments: ``failed`` with an error code;
        - ``write_external`` without ``approval_id``: ``approval_required`` and a
          pending ``approval``; nothing is executed;
        - ``approval_id`` not approved, foreign, expired or with a different
          ``args_hash``: ``denied``/``expired``; nothing is executed;
        - external write that timed out: ``uncertain``, never retried.
        """
        ...
