"""Deterministic credential-free realtime provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid5

from app.modules.realtime.contracts import OUTPUT_AUDIO_FORMAT, ConfigurePayload
from app.modules.realtime.provider import ProviderAudio, ProviderControl, ProviderOutput
from app.modules.realtime.sessions import RealtimeSession


class FakeRealtimeProvider:
    """Generate transcript, tool, audio, completion, and usage events after commit."""

    _CLOSED = object()

    def __init__(self, session: RealtimeSession, *, event_queue_size: int = 32) -> None:
        self._session = session
        self._events: asyncio.Queue[ProviderOutput | object] = asyncio.Queue(
            maxsize=event_queue_size
        )
        self._audio = bytearray()
        self._configured = False
        self._closed = False
        self._cancelled: set[UUID] = set()
        self._response_turns: dict[UUID, UUID] = {}

    @staticmethod
    def response_id(turn_id: UUID) -> UUID:
        return uuid5(turn_id, "fake-response")

    async def configure(self, config: ConfigurePayload) -> None:
        if config.conversation_id != self._session.conversation_id:
            raise ValueError("conversation does not match the realtime session")
        self._configured = True

    async def send_audio(self, chunk: bytes) -> None:
        if not self._configured or self._closed:
            raise RuntimeError("fake provider is not ready")
        self._audio.extend(chunk)

    async def commit_audio(self, *, stream_id: UUID, turn_id: UUID) -> None:
        if not self._configured or self._closed:
            raise RuntimeError("fake provider is not ready")
        response_id = self.response_id(turn_id)
        self._response_turns[response_id] = turn_id
        segment_id = uuid5(response_id, "fake-audio-segment")
        call_id = uuid5(response_id, "fake-tool-call")
        input_duration = len(self._audio) // 32
        generated_audio = bytes(index % 127 for index in range(1920))
        script: tuple[ProviderOutput, ...] = (
            ProviderControl(
                "input_audio.speech_started",
                {"stream_id": str(stream_id), "offset_ms": 0},
                turn_id=turn_id,
            ),
            ProviderControl(
                "input_audio.speech_stopped",
                {"stream_id": str(stream_id), "offset_ms": input_duration},
                turn_id=turn_id,
            ),
            ProviderControl(
                "transcript.input.delta",
                {"text": "Тестовый", "language": self._session.locale},
                turn_id=turn_id,
            ),
            ProviderControl(
                "transcript.input.final",
                {"text": "Тестовый запрос", "language": self._session.locale},
                turn_id=turn_id,
            ),
            ProviderControl(
                "response.started",
                {"modalities": ["text", "audio"]},
                turn_id=turn_id,
                response_id=response_id,
            ),
            ProviderControl(
                "tool.call.started",
                {
                    "call_id": str(call_id),
                    "name": "system.get_current_datetime",
                    "arguments": {"timezone": "Europe/Minsk"},
                    "requires_confirmation": False,
                },
                turn_id=turn_id,
                response_id=response_id,
            ),
            ProviderControl(
                "tool.call.completed",
                {
                    "call_id": str(call_id),
                    "name": "system.get_current_datetime",
                    "status": "succeeded",
                    "result": {"local_datetime": "2026-09-10T13:00:03+03:00"},
                },
                turn_id=turn_id,
                response_id=response_id,
            ),
            ProviderControl(
                "response.audio.started",
                {
                    "segment_id": str(segment_id),
                    "format": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
                },
                turn_id=turn_id,
                response_id=response_id,
            ),
            ProviderAudio(generated_audio, response_id=response_id),
            ProviderControl(
                "response.audio.completed",
                {
                    "segment_id": str(segment_id),
                    "audio_bytes": len(generated_audio),
                    "duration_ms": 40,
                },
                turn_id=turn_id,
                response_id=response_id,
            ),
            ProviderControl(
                "transcript.output.delta",
                {"text": "Тестовый", "language": self._session.locale},
                turn_id=turn_id,
                response_id=response_id,
            ),
            ProviderControl(
                "transcript.output.final",
                {"text": "Тестовый ответ", "language": self._session.locale},
                turn_id=turn_id,
                response_id=response_id,
            ),
            ProviderControl("turn.completed", {"status": "completed"}, turn_id=turn_id),
            ProviderControl(
                "usage.updated",
                {
                    "provider_alias": "fake",
                    "model_alias": "realtime-default",
                    "input_tokens": 2,
                    "output_tokens": 2,
                    "input_audio_ms": input_duration,
                    "output_audio_ms": 40,
                },
                turn_id=turn_id,
            ),
        )
        self._audio.clear()
        for event in script:
            if response_id in self._cancelled and not (
                isinstance(event, ProviderControl) and event.event_type == "response.interrupted"
            ):
                continue
            await self._events.put(event)

    async def cancel_response(self, response_id: UUID, reason: str) -> None:
        self._cancelled.add(response_id)
        await self._events.put(
            ProviderControl(
                "response.interrupted",
                {
                    "reason": "barge_in" if reason == "barge_in" else "user_request",
                    "delivered_audio_ms": 0,
                },
                turn_id=self._response_turns.get(response_id),
                response_id=response_id,
            )
        )

    async def events(self) -> AsyncIterator[ProviderOutput]:
        while True:
            event = await self._events.get()
            if event is self._CLOSED:
                return
            if isinstance(event, ProviderControl | ProviderAudio):
                yield event

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._events.put_nowait(self._CLOSED)
        except asyncio.QueueFull:
            _ = self._events.get_nowait()
            self._events.put_nowait(self._CLOSED)
