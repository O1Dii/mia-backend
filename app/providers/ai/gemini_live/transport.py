"""Official Gemini Live wire transport with Google SDK message validation.

The shared google-genai 1.10 pin cannot configure input transcription for the
Developer API. This small transport uses the documented WebSocket API instead
of patching SDK internals. Google types never cross the realtime provider port.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.parse import urlencode

from google.genai import types  # type: ignore[import-untyped]
from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect

from app.providers.ai.base import ToolDefinition

_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
# A dedicated, unregistered logger cannot inherit application DEBUG settings.
# WebSocket request URLs and frames contain credentials/conversation content.
_WIRE_LOGGER = logging.Logger("mia.gemini_live.wire")
_WIRE_LOGGER.disabled = True
_WIRE_LOGGER.propagate = False


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
    resume_handle: str | None = field(repr=False)
    tools: tuple[ToolDefinition, ...]


class GeminiLiveTransport(Protocol):
    async def send_audio(self, chunk: bytes) -> None: ...

    async def end_audio_stream(self) -> None: ...

    async def send_tool_response(
        self, *, call_id: str, name: str, response: Mapping[str, object]
    ) -> None: ...

    def receive(self) -> AsyncIterator[types.LiveServerMessage]: ...

    async def close(self) -> None: ...


class GeminiLiveTransportFactory(Protocol):
    async def connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport: ...


class GeminiLiveTransportError(RuntimeError):
    """Fixed transport failure without raw frame, close reason, or endpoint."""


class GoogleGenAILiveTransport:
    def __init__(self, websocket: ClientConnection) -> None:
        self._websocket = websocket
        self._closed = False

    async def _send(self, message: Mapping[str, object]) -> None:
        try:
            await self._websocket.send(json.dumps(message))
        except Exception:
            raise GeminiLiveTransportError("Gemini Live send failed.") from None

    async def send_audio(self, chunk: bytes) -> None:
        await self._send(
            {
                "realtimeInput": {
                    "audio": {
                        "data": base64.b64encode(chunk).decode("ascii"),
                        "mimeType": "audio/pcm;rate=16000",
                    }
                }
            }
        )

    async def end_audio_stream(self) -> None:
        await self._send({"realtimeInput": {"audioStreamEnd": True}})

    async def send_tool_response(
        self, *, call_id: str, name: str, response: Mapping[str, object]
    ) -> None:
        await self._send(
            {
                "toolResponse": {
                    "functionResponses": [{"id": call_id, "name": name, "response": dict(response)}]
                }
            }
        )

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        while not self._closed:
            try:
                raw = await self._websocket.recv()
                message = json.loads(raw)
                if not isinstance(message, dict) or "error" in message:
                    raise ValueError("invalid provider envelope")
                parsed = types.LiveServerMessage.model_validate(message)
            except Exception:
                raise GeminiLiveTransportError("Gemini Live receive failed.") from None
            yield parsed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._websocket.close()
        except Exception:
            raise GeminiLiveTransportError("Gemini Live close failed.") from None


class GoogleGenAILiveTransportFactory:
    """Open an API-key authenticated Developer API connection and validate setup."""

    def __init__(self, api_key: SecretStr) -> None:
        self._api_key = api_key

    @staticmethod
    def _wire_setup(setup: GeminiLiveSetup) -> dict[str, object]:
        config: dict[str, object] = {
            "model": setup.model if setup.model.startswith("models/") else f"models/{setup.model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "maxOutputTokens": setup.max_output_tokens,
            },
            "systemInstruction": {"parts": [{"text": setup.system_instruction}]},
            "realtimeInputConfig": {
                "automaticActivityDetection": {"disabled": False},
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
            },
            "sessionResumption": {"handle": setup.resume_handle} if setup.resume_handle else {},
            "contextWindowCompression": {
                "triggerTokens": setup.context_window_trigger_tokens,
                "slidingWindow": {"targetTokens": setup.context_window_target_tokens},
            },
        }
        if setup.input_transcription:
            config["inputAudioTranscription"] = {}
        if setup.output_transcription:
            config["outputAudioTranscription"] = {}
        if setup.tools:
            config["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": dict(tool.parameters),
                        }
                        for tool in setup.tools
                    ]
                }
            ]
        return {"setup": config}

    async def connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport:
        websocket: ClientConnection | None = None
        try:
            websocket = await connect(
                f"{_ENDPOINT}?{urlencode({'key': self._api_key.get_secret_value()})}",
                logger=_WIRE_LOGGER,
                max_size=1024 * 1024,
                max_queue=16,
                open_timeout=30,
                close_timeout=3,
            )
            await websocket.send(json.dumps(self._wire_setup(setup)))
            response = json.loads(await websocket.recv())
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("setupComplete"), dict)
                or "error" in response
            ):
                raise ValueError("provider setup was not acknowledged")
        except BaseException as error:
            if websocket is not None:
                try:
                    await websocket.close()
                except Exception:
                    pass
            if isinstance(error, asyncio.CancelledError):
                raise
            if not isinstance(error, Exception):
                raise
            raise GeminiLiveTransportError("Gemini Live connection failed.") from None
        return GoogleGenAILiveTransport(websocket)


def normalized_arguments(arguments: object) -> Mapping[str, object]:
    if arguments is None:
        return {}
    if not isinstance(arguments, Mapping):
        raise ValueError("Gemini Live function arguments are not an object")
    return cast(Mapping[str, object], arguments)
