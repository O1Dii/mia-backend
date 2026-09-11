"""Google Gen AI SDK transport kept behind a narrow testable boundary."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol, cast

from google import genai  # type: ignore[import-untyped]
from google.genai import types  # type: ignore[import-untyped]
from pydantic import SecretStr

from app.providers.ai.base import ToolDefinition


@dataclass(frozen=True, slots=True)
class GeminiLiveSetup:
    model: str
    locale: str
    system_instruction: str
    max_output_tokens: int
    input_transcription: bool
    output_transcription: bool
    context_window_trigger_tokens: int
    context_window_target_tokens: int
    resume_handle: str | None
    tools: tuple[ToolDefinition, ...]


class GeminiLiveTransport(Protocol):
    async def send_audio(self, chunk: bytes) -> None: ...

    async def end_audio_stream(self) -> None: ...

    def receive(self) -> AsyncIterator[types.LiveServerMessage]: ...

    async def close(self) -> None: ...


class GeminiLiveTransportFactory(Protocol):
    async def connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport: ...


class _SDKLiveSession(Protocol):
    async def send_realtime_input(self, **kwargs: object) -> None: ...

    def receive(self) -> AsyncIterator[types.LiveServerMessage]: ...


class GoogleGenAILiveTransport:
    """Compatibility wrapper for the pinned SDK and its newer Live signature."""

    def __init__(
        self,
        *,
        context: AbstractAsyncContextManager[Any],
        session: _SDKLiveSession,
    ) -> None:
        self._context = context
        self._session = session
        self._closed = False

    async def send_audio(self, chunk: bytes) -> None:
        blob = types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
        parameters = inspect.signature(self._session.send_realtime_input).parameters
        if "audio" in parameters:
            await self._session.send_realtime_input(audio=blob)
        else:
            await self._session.send_realtime_input(media=blob)

    async def end_audio_stream(self) -> None:
        parameters = inspect.signature(self._session.send_realtime_input).parameters
        if "audio_stream_end" in parameters:
            await self._session.send_realtime_input(audio_stream_end=True)
            return

        # google-genai 1.10 predates the public audio_stream_end keyword but its
        # session already uses the same Live WebSocket wire format. Keep this
        # isolated compatibility path until the shared dependency owner upgrades it.
        websocket = getattr(self._session, "_ws", None)
        send = getattr(websocket, "send", None)
        if send is None:
            raise RuntimeError("Gemini Live SDK cannot signal the end of audio")
        result = send(json.dumps({"realtime_input": {"audio_stream_end": True}}))
        if not isinstance(result, Awaitable):
            raise RuntimeError("Gemini Live SDK returned an invalid WebSocket sender")
        await result

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        # The SDK iterator ends at each ``turn_complete`` even though the Live
        # connection remains open. Re-enter it so the adapter sees a continuous
        # provider stream across turns.
        while not self._closed:
            async for message in self._session.receive():
                yield message

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._context.__aexit__(None, None, None)


class GoogleGenAILiveTransportFactory:
    """Open server-authenticated Gemini Live connections without leaking SDK types."""

    def __init__(self, api_key: SecretStr) -> None:
        self._api_key = api_key

    @staticmethod
    def _sdk_config(setup: GeminiLiveSetup) -> types.LiveConnectConfig:
        declarations = [
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=dict(tool.parameters),
            )
            for tool in setup.tools
        ]
        tools = [types.Tool(function_declarations=declarations)] if declarations else None
        resume = types.SessionResumptionConfig(handle=setup.resume_handle, transparent=True)
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            max_output_tokens=setup.max_output_tokens,
            system_instruction=types.Content(parts=[types.Part(text=setup.system_instruction)]),
            tools=tools,
            input_audio_transcription=(
                types.AudioTranscriptionConfig() if setup.input_transcription else None
            ),
            output_audio_transcription=(
                types.AudioTranscriptionConfig() if setup.output_transcription else None
            ),
            session_resumption=resume,
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=setup.context_window_trigger_tokens,
                sliding_window=types.SlidingWindow(
                    target_tokens=setup.context_window_target_tokens
                ),
            ),
        )

    async def connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport:
        client = genai.Client(api_key=self._api_key.get_secret_value())
        context = cast(
            AbstractAsyncContextManager[Any],
            client.aio.live.connect(model=setup.model, config=self._sdk_config(setup)),
        )
        session = cast(_SDKLiveSession, await context.__aenter__())
        return GoogleGenAILiveTransport(context=context, session=session)


def normalized_arguments(arguments: object) -> Mapping[str, object]:
    if arguments is None:
        return {}
    if not isinstance(arguments, Mapping):
        raise ValueError("Gemini Live function arguments are not an object")
    return cast(Mapping[str, object], arguments)
