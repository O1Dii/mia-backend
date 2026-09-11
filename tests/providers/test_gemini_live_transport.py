from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from app.providers.ai.base import ToolDefinition
from app.providers.ai.gemini_live import GeminiLiveSetup, GoogleGenAILiveTransportFactory
from app.providers.ai.gemini_live.transport import GoogleGenAILiveTransport
from google.genai import types  # type: ignore[import-untyped]
from pydantic import SecretStr


def setup() -> GeminiLiveSetup:
    return GeminiLiveSetup(
        model="configured-live-model",
        locale="ru-BY",
        system_instruction="Respond in Belarusian or Russian.",
        max_output_tokens=512,
        input_transcription=True,
        output_transcription=True,
        context_window_trigger_tokens=25_000,
        context_window_target_tokens=8_000,
        resume_handle="opaque-test-handle",
        tools=(
            ToolDefinition(
                name="system.get_current_datetime",
                description="Return local time.",
                parameters={"type": "object"},
            ),
        ),
    )


def test_sdk_setup_enables_audio_transcripts_tools_compression_and_resumption() -> None:
    sdk_config = GoogleGenAILiveTransportFactory._sdk_config(setup())
    assert sdk_config.response_modalities == ["AUDIO"]
    assert sdk_config.input_audio_transcription is not None
    assert sdk_config.output_audio_transcription is not None
    assert sdk_config.session_resumption is not None
    assert sdk_config.session_resumption.handle == "opaque-test-handle"
    assert sdk_config.session_resumption.transparent is True
    assert sdk_config.context_window_compression is not None
    assert sdk_config.context_window_compression.trigger_tokens == 25_000
    assert sdk_config.context_window_compression.sliding_window is not None
    assert sdk_config.context_window_compression.sliding_window.target_tokens == 8_000
    assert sdk_config.tools is not None
    declarations = sdk_config.tools[0].function_declarations
    assert declarations is not None
    assert declarations[0].name == "system.get_current_datetime"


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class LegacySDKSession:
    def __init__(self) -> None:
        self._ws = FakeWebSocket()
        self.media: list[types.Blob] = []
        self.receive_calls = 0

    async def send_realtime_input(self, *, media: object) -> None:
        assert isinstance(media, types.Blob)
        self.media.append(media)

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        self.receive_calls += 1
        yield types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


class FakeContext(AbstractAsyncContextManager[object]):
    def __init__(self) -> None:
        self.exited = False

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        self.exited = True


async def test_pinned_sdk_compatibility_sends_audio_end_and_receives_multiple_turns() -> None:
    context = FakeContext()
    session = LegacySDKSession()
    transport = GoogleGenAILiveTransport(
        context=cast(AbstractAsyncContextManager[Any], context),
        session=cast(Any, session),
    )
    await transport.send_audio(b"\x00\x01")
    await transport.end_audio_stream()
    assert session.media[0].data == b"\x00\x01"
    assert json.loads(session._ws.messages[0]) == {"realtime_input": {"audio_stream_end": True}}

    events = cast(AsyncGenerator[types.LiveServerMessage, None], transport.receive())
    assert (await anext(events)).server_content.turn_complete is True
    assert (await anext(events)).server_content.turn_complete is True
    assert session.receive_calls == 2
    await transport.close()
    await events.aclose()
    assert context.exited is True


def test_transport_factory_keeps_credential_redacted() -> None:
    factory = GoogleGenAILiveTransportFactory(SecretStr("provider-key"))
    assert "provider-key" not in repr(factory._api_key)
