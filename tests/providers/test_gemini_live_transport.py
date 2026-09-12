from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from app.providers.ai.base import ToolDefinition
from app.providers.ai.gemini_live import GeminiLiveSetup, GoogleGenAILiveTransportFactory
from app.providers.ai.gemini_live import transport as wire
from app.providers.ai.gemini_live.transport import (
    GeminiLiveTransportError,
    GoogleGenAILiveTransport,
)
from google.genai import types  # type: ignore[import-untyped]
from pydantic import SecretStr
from websockets.asyncio.server import ServerConnection, serve


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


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        for message in messages:
            self.incoming.put_nowait(message)
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


def install_socket(monkeypatch: pytest.MonkeyPatch, socket: FakeWebSocket) -> None:
    async def connect(uri: str, **kwargs: object) -> Any:
        assert uri.endswith("?key=fake-test-key")
        assert kwargs["max_size"] == 1024 * 1024
        logger = cast(logging.Logger, kwargs["logger"])
        assert logger.disabled and not logger.propagate
        return socket

    monkeypatch.setattr(wire, "connect", connect)


async def test_setup_handshake_pcm_and_multiple_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = FakeWebSocket(
        [
            '{"setupComplete": {}}',
            '{"serverContent": {"turnComplete": true}}',
            '{"serverContent": {"turnComplete": true}}',
        ]
    )
    install_socket(monkeypatch, socket)
    transport = await GoogleGenAILiveTransportFactory(SecretStr("fake-test-key")).connect(setup())
    config = socket.sent[0]["setup"]
    assert config["model"] == "models/configured-live-model"
    assert config["generationConfig"] == {"responseModalities": ["AUDIO"], "maxOutputTokens": 512}
    assert config["inputAudioTranscription"] == config["outputAudioTranscription"] == {}
    assert config["sessionResumption"] == {"handle": "opaque-test-handle"}
    assert config["realtimeInputConfig"]["automaticActivityDetection"] == {"disabled": False}
    assert config["tools"][0]["functionDeclarations"][0]["name"] == "system.get_current_datetime"
    assert config["contextWindowCompression"]["slidingWindow"] == {"targetTokens": 8_000}
    assert "opaque-test-handle" not in repr(setup())

    await transport.send_audio(b"\x00\x01")
    await transport.end_audio_stream()
    assert socket.sent[1] == {
        "realtimeInput": {
            "audio": {
                "mimeType": "audio/pcm;rate=16000",
                "data": "AAE=",
            }
        }
    }
    assert socket.sent[2] == {"realtimeInput": {"audioStreamEnd": True}}
    events = transport.receive()
    for _ in range(2):
        assert (await anext(events)).server_content.turn_complete is True
    await transport.close()
    assert socket.closed


@pytest.mark.parametrize("response", ['{"error":{"message":"private-detail"}}', "[]", "{}"])
async def test_invalid_setup_is_sanitized_and_socket_closed(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    socket = FakeWebSocket([response])
    install_socket(monkeypatch, socket)
    with pytest.raises(GeminiLiveTransportError, match="^Gemini Live connection failed\\.$"):
        await GoogleGenAILiveTransportFactory(SecretStr("fake-test-key")).connect(setup())
    assert socket.closed


async def test_cancelled_handshake_releases_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = FakeWebSocket([])
    install_socket(monkeypatch, socket)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await GoogleGenAILiveTransportFactory(SecretStr("fake-test-key")).connect(setup())
    assert socket.closed


@pytest.mark.parametrize(
    "message",
    [
        "raw-private-detail",
        '{"error":{"message":"private-detail"}}',
        '{"serverContent":{"modelTurn":{"parts":[{"inlineData":{"data":"!"}}]}}}',
    ],
)
async def test_raw_provider_errors_do_not_escape(message: str) -> None:
    socket = FakeWebSocket([message])
    transport = GoogleGenAILiveTransport(cast(Any, socket))
    with pytest.raises(GeminiLiveTransportError, match="^Gemini Live receive failed\\.$"):
        await anext(transport.receive())
    await transport.close()


@asynccontextmanager
async def fixture_server() -> AsyncIterator[str]:
    fixture = Path(__file__).parent / "fixtures" / "gemini_live_messages.json"
    messages = json.loads(fixture.read_text())["messages"]

    async def handle(socket: ServerConnection) -> None:
        config = json.loads(await socket.recv())["setup"]
        assert "inputAudioTranscription" in config
        await socket.send('{"setupComplete": {}}')
        audio = json.loads(await socket.recv())["realtimeInput"]["audio"]
        assert base64.b64decode(audio["data"]) == bytes(1280)
        assert json.loads(await socket.recv()) == {"realtimeInput": {"audioStreamEnd": True}}
        for message in messages:
            await socket.send(json.dumps(message))
        await socket.wait_closed()

    async with serve(handle, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def test_real_websocket_codec_with_deterministic_provider_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with fixture_server() as endpoint:
        monkeypatch.setattr(wire, "_ENDPOINT", endpoint)
        transport = await GoogleGenAILiveTransportFactory(SecretStr("fake-test-key")).connect(
            setup()
        )
        try:
            await transport.send_audio(bytes(1280))
            await transport.end_audio_stream()
            messages: list[types.LiveServerMessage] = []
            async with asyncio.timeout(2):
                async for message in transport.receive():
                    messages.append(message)
                    if message.server_content and message.server_content.turn_complete:
                        break
            audio = messages[4].server_content.model_turn.parts[0].inline_data.data
            assert audio == bytes(range(48))
            assert messages[7].usage_metadata.response_token_count == 7
        finally:
            await transport.close()
