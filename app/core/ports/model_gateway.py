"""ModelGateway port: provider-neutral model access (Review §5.5, ADR-0014).

The run engine (M12) depends on this protocol; the gateway module (M11) owns
adapters, connections and the resolution of ``mia:default`` to the user's
primary model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from app.core.ports.types import DEFAULT_MODEL, JsonObject, PortError, RunProfile

ModelGatewayErrorCode = Literal[
    "model_not_configured",
    "invalid_request",
    "provider_unavailable",
    "provider_timeout",
    "rate_limited",
    "context_too_long",
]


class ModelGatewayError(PortError):
    """Raised by ``stream``/``describe``; ``code`` is a ``ModelGatewayErrorCode``."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool definition as presented to the model."""

    name: str
    """Qualified name, e.g. ``team.linear.list_issues`` or ``background.start``."""
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    """A function call proposed by the model."""

    call_id: str
    name: str
    arguments: JsonObject
    provider_state: JsonObject | None = None
    """Opaque provider parts (e.g. thought signatures) that must be replayed
    verbatim on the next request. Never shown to clients (closes OI-W6-013)."""


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str | None = None
    """Set on ``role="tool"`` messages: which call this result answers."""
    provider_state: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class ModelRequest:
    user_id: str
    profile: RunProfile
    messages: Sequence[ModelMessage]
    tools: Sequence[ToolSpec] = ()
    model: str = DEFAULT_MODEL
    structured_response: bool = True
    """Ask for the speech/display JSON (assistant-response.schema.json) when the
    model supports structured output; otherwise the run engine splits text."""
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str
    kind: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True, slots=True)
class ToolCallRequested:
    call: ModelToolCall
    kind: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None
    kind: Literal["usage"] = "usage"


@dataclass(frozen=True, slots=True)
class ModelDone:
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"]
    kind: Literal["done"] = "done"


ModelEvent = TextDelta | ToolCallRequested | ModelUsage | ModelDone
"""Stream items. Every successful stream ends with exactly one ``ModelDone``."""


@dataclass(frozen=True, slots=True)
class ModelInfo:
    ref: str
    """Resolved ``<provider>/<model>`` reference."""
    provider: str
    supports_tools: bool
    supports_structured_output: bool
    verified: bool
    """True when the model passed the contract test suite (ADR-0014)."""


@runtime_checkable
class ModelGateway(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one model step. Raises ``ModelGatewayError`` before or during
        iteration; a stream that raised must not be resumed."""
        ...

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        """Resolve a model reference for a user without calling the provider."""
        ...
