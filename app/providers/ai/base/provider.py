"""Provider-neutral text request and streaming port."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.providers.ai.base.events import AIEvent, ToolRisk


@dataclass(frozen=True, slots=True)
class TextMessage:
    role: Literal["user", "assistant"]
    text: str

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("text message cannot be empty")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, object]
    risk: ToolRisk = "read"
    requires_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class ToolFeedback:
    """A policy-approved result, never a system instruction."""

    name: str
    arguments: Mapping[str, object]
    response: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TextRequest:
    turn_id: UUID
    response_id: UUID
    messages: Sequence[TextMessage]
    system_instruction: str
    tools: Sequence[ToolDefinition] = ()
    tool_feedback: Sequence[ToolFeedback] = ()
    max_output_tokens: int = 512

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("text request requires at least one message")
        if not self.system_instruction:
            raise ValueError("system instruction cannot be empty")
        if self.max_output_tokens < 1:
            raise ValueError("max output tokens must be positive")


class TextAIProvider(Protocol):
    """Provider port consumed by orchestration; SDK types cannot cross it."""

    def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]: ...
