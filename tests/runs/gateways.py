"""Test gateways beyond ``tests.fakes.FakeModelGateway``: blocking, echoing, info presets."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.core.ports.model_gateway import (
    ModelDone,
    ModelEvent,
    ModelInfo,
    ModelRequest,
    ModelUsage,
    TextDelta,
)
from app.core.ports.types import DEFAULT_MODEL

from tests.fakes import FakeModelGateway

STRUCTURED = ModelInfo(
    ref="fake/structured-1",
    provider="fake",
    supports_tools=True,
    supports_structured_output=True,
    verified=True,
)
PLAIN = ModelInfo(
    ref="fake/plain-1",
    provider="fake",
    supports_tools=True,
    supports_structured_output=False,
    verified=True,
)


def answer(speech: str, display: Any = None, followups: Sequence[str] = ()) -> str:
    return json.dumps(
        {"speech": speech, "display": display, "followups": list(followups)}, ensure_ascii=False
    )


def scripted(*steps: Sequence[ModelEvent], info: ModelInfo = STRUCTURED) -> FakeModelGateway:
    return FakeModelGateway(list(steps), info=info)


class BlockingGateway:
    """Streams one chunk, then blocks until ``release`` is set."""

    def __init__(self, final_text: str, *, info: ModelInfo = STRUCTURED) -> None:
        self.info = info
        self.final_text = final_text
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.requests: list[ModelRequest] = []

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        return self.info

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        try:
            self.entered.set()
            await self.release.wait()
            yield TextDelta(self.final_text)
            yield ModelDone("stop")
        finally:
            self.closed.set()


class DelayedGateway:
    """Answers after ``delay_s`` (for ``speech.ack``)."""

    def __init__(self, text: str, delay_s: float, *, info: ModelInfo = STRUCTURED) -> None:
        self.text = text
        self.delay_s = delay_s
        self.info = info

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        return self.info

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        await asyncio.sleep(self.delay_s)
        yield TextDelta(self.text)
        yield ModelUsage(input_tokens=10, output_tokens=5)
        yield ModelDone("stop")


class EchoGateway:
    """Plain-text model that answers ``Ответ <input>.`` in jittered chunks."""

    def __init__(self, *, chunk: int = 3, seed: int = 7) -> None:
        self.info = PLAIN
        self.chunk = chunk
        self.random = random.Random(seed)

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        return self.info

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        text = f"Ответ {request.messages[-1].content}."
        for start in range(0, len(text), self.chunk):
            await asyncio.sleep(self.random.random() / 200)
            yield TextDelta(text[start : start + self.chunk])
        yield ModelDone("stop")


class GatedGateway:
    """Plain-text chunks; each chunk waits for one ``step`` permit (deterministic streaming)."""

    def __init__(self, chunks: Sequence[str]) -> None:
        self.info = PLAIN
        self.chunks = list(chunks)
        self.permits = asyncio.Semaphore(0)

    async def describe(self, *, user_id: str, model: str = DEFAULT_MODEL) -> ModelInfo:
        return self.info

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        for chunk in self.chunks:
            await self.permits.acquire()
            yield TextDelta(chunk)
        yield ModelDone("stop")
