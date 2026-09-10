"""Credential-free deterministic text provider for development and CI."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from app.providers.ai.base import (
    AIEvent,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    TextRequest,
    ToolCallRequested,
    UsageReported,
)
from app.providers.ai.base.events import FinishReason


@dataclass(frozen=True, slots=True)
class FakeToolCall:
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class FakeTextResponse:
    text_chunks: tuple[str, ...] = ("Готово.",)
    tool_call: FakeToolCall | None = None
    input_tokens: int = 8
    output_tokens: int = 3


class FakeAIProvider:
    """Emit a fixed script with stable sequence and event identifiers."""

    def __init__(
        self,
        response: FakeTextResponse | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._response = response or FakeTextResponse()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _event_id(self, response_id: UUID, seq: int) -> UUID:
        return uuid5(response_id, f"fake-text:{seq}")

    async def stream(self, request: TextRequest) -> AsyncIterator[AIEvent]:
        seq = 1
        yield ResponseStarted(
            event_id=self._event_id(request.response_id, seq),
            turn_id=request.turn_id,
            response_id=request.response_id,
            seq=seq,
            at=self._clock(),
            modalities=("text",),
        )
        for text in self._response.text_chunks:
            seq += 1
            yield TextDelta(
                event_id=self._event_id(request.response_id, seq),
                turn_id=request.turn_id,
                response_id=request.response_id,
                seq=seq,
                at=self._clock(),
                text=text,
            )

        finish_reason: FinishReason = "stop"
        if self._response.tool_call is not None:
            seq += 1
            scripted_call = self._response.tool_call
            definition = next(
                (tool for tool in request.tools if tool.name == scripted_call.name), None
            )
            if definition is None:
                raise ValueError("fake response requested an undeclared tool")
            yield ToolCallRequested(
                event_id=self._event_id(request.response_id, seq),
                turn_id=request.turn_id,
                response_id=request.response_id,
                seq=seq,
                at=self._clock(),
                call_id=uuid5(request.response_id, f"fake-tool:{scripted_call.name}"),
                name=scripted_call.name,
                arguments=scripted_call.arguments,
                risk=definition.risk,
                requires_confirmation=definition.requires_confirmation,
            )
            finish_reason = "tool_result"

        seq += 1
        yield ResponseCompleted(
            event_id=self._event_id(request.response_id, seq),
            turn_id=request.turn_id,
            response_id=request.response_id,
            seq=seq,
            at=self._clock(),
            finish_reason=finish_reason,
        )
        seq += 1
        yield UsageReported(
            event_id=self._event_id(request.response_id, seq),
            turn_id=request.turn_id,
            response_id=request.response_id,
            seq=seq,
            at=self._clock(),
            provider_alias="fake",
            model_alias="text-default",
            input_tokens=self._response.input_tokens,
            output_tokens=self._response.output_tokens,
        )
