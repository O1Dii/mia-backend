"""Scripted ModelGateway: each ``stream`` call replays the next scripted step."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence

from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelGatewayError,
    ModelInfo,
    ModelRequest,
    ModelToolCall,
    TextDelta,
    ToolCallRequested,
)
from app.core.ports.types import DEFAULT_MODEL, JsonObject

FAKE_MODEL_REF = "fake/scripted-1"


def text_step(*chunks: str) -> list[ModelEvent]:
    """A final answer streamed in ``chunks``."""
    return [*(TextDelta(chunk) for chunk in chunks), ModelDone("stop")]


def tool_step(call_id: str, name: str, arguments: JsonObject) -> list[ModelEvent]:
    """A step in which the model asks for one tool call."""
    call = ModelToolCall(call_id=call_id, name=name, arguments=arguments)
    return [ToolCallRequested(call), ModelDone("tool_calls")]


class FakeModelGateway:
    def __init__(
        self,
        steps: Iterable[Sequence[ModelEvent] | ModelGatewayError] = (),
        *,
        info: ModelInfo | None = None,
    ) -> None:
        self._steps: deque[Sequence[ModelEvent] | ModelGatewayError] = deque(steps)
        self.requests: list[ModelRequest] = []
        self.info = info or ModelInfo(
            ref=FAKE_MODEL_REF,
            provider="fake",
            supports_tools=True,
            supports_structured_output=True,
            verified=True,
        )

    def add_step(self, step: Sequence[ModelEvent] | ModelGatewayError) -> None:
        self._steps.append(step)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self._steps:
            raise ModelGatewayError("invalid_request", "Fake gateway script is exhausted")
        step = self._steps.popleft()
        if isinstance(step, ModelGatewayError):
            raise step
        for event in step:
            yield event

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        if model not in (DEFAULT_MODEL, self.info.ref):
            raise ModelGatewayError("model_not_configured", "Unknown model reference")
        return self.info
