"""Gemini Live implementation of the provider-neutral realtime session port."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from google.genai import types  # type: ignore[import-untyped]

from app.modules.realtime.contracts import INPUT_AUDIO_FORMAT, OUTPUT_AUDIO_FORMAT, ConfigurePayload
from app.modules.realtime.provider import ProviderAudio, ProviderControl, ProviderOutput
from app.modules.realtime.sessions import RealtimeSession
from app.providers.ai.gemini_live.config import GeminiLiveConfig
from app.providers.ai.gemini_live.mapping import GeminiLiveEventMapper, InvalidGeminiLiveMessage
from app.providers.ai.gemini_live.resumption import (
    GeminiLiveResumeStore,
    InMemoryGeminiLiveResumeStore,
)
from app.providers.ai.gemini_live.transport import (
    GeminiLiveSetup,
    GeminiLiveTransport,
    GeminiLiveTransportFactory,
    GoogleGenAILiveTransportFactory,
)


class GeminiLiveAdapterError(RuntimeError):
    """Sanitized provider failure safe to cross the adapter boundary."""


@dataclass(frozen=True, slots=True)
class _Failure:
    error: GeminiLiveAdapterError


class GeminiLiveAdapter:
    """Map one MIA realtime session to resumable Gemini Live connections."""

    _CLOSED = object()

    def __init__(
        self,
        session: RealtimeSession,
        config: GeminiLiveConfig,
        *,
        transport_factory: GeminiLiveTransportFactory | None = None,
        resume_store: GeminiLiveResumeStore | None = None,
        event_queue_size: int = 32,
    ) -> None:
        if event_queue_size < 1:
            raise ValueError("Gemini Live event queue size must be positive")
        self._session = session
        self._config = config
        self._transport_factory = transport_factory or GoogleGenAILiveTransportFactory(
            config.api_key
        )
        self._resume_store = resume_store or InMemoryGeminiLiveResumeStore()
        self._events: asyncio.Queue[ProviderOutput | _Failure | object] = asyncio.Queue(
            maxsize=event_queue_size
        )
        self._mapper = GeminiLiveEventMapper(
            locale=session.locale,
            model_alias=config.model_alias,
            tools=config.tools,
        )
        self._mapping_lock = asyncio.Lock()
        self._transport_lock = asyncio.Lock()
        self._transport: GeminiLiveTransport | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._setup: GeminiLiveSetup | None = None
        self._resume_handle: str | None = None
        self._input_audio_bytes = 0
        self._deferred_messages: list[types.LiveServerMessage] = []
        self._configured = False
        self._closed = False

    def _build_setup(self, config: ConfigurePayload, resume_handle: str | None) -> GeminiLiveSetup:
        return GeminiLiveSetup(
            model=self._config.model,
            locale=config.locale,
            system_instruction=(
                f"{self._config.system_instruction.rstrip()}\nUser locale: {config.locale}."
            ),
            max_output_tokens=self._config.max_output_tokens,
            input_transcription="input_transcript" in config.capabilities,
            output_transcription="output_transcript" in config.capabilities,
            context_window_trigger_tokens=self._config.context_window_trigger_tokens,
            context_window_target_tokens=self._config.context_window_target_tokens,
            resume_handle=resume_handle,
            tools=self._config.tools,
        )

    async def _connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport:
        async with asyncio.timeout(self._config.request_timeout_seconds):
            return await self._transport_factory.connect(setup)

    async def configure(self, config: ConfigurePayload) -> None:
        if self._configured or self._closed:
            raise RuntimeError("Gemini Live adapter cannot be configured now")
        if config.conversation_id != self._session.conversation_id:
            raise ValueError("conversation does not match the realtime session")
        if (
            INPUT_AUDIO_FORMAT not in config.input_audio
            or OUTPUT_AUDIO_FORMAT not in config.output_audio
        ):
            raise ValueError("Gemini Live requires the negotiated PCM formats")
        self._resume_handle = await self._resume_store.load(self._session.id)
        self._setup = self._build_setup(config, self._resume_handle)
        self._transport = await self._connect(self._setup)
        self._configured = True
        self._receive_task = asyncio.create_task(
            self._receive_loop(), name=f"gemini-live-receive-{self._session.id}"
        )

    def _require_transport(self) -> GeminiLiveTransport:
        if not self._configured or self._closed or self._transport is None:
            raise RuntimeError("Gemini Live adapter is not ready")
        return self._transport

    async def send_audio(self, chunk: bytes) -> None:
        if not chunk:
            raise ValueError("Gemini Live audio chunk cannot be empty")
        async with self._transport_lock:
            transport = self._require_transport()
            async with asyncio.timeout(self._config.request_timeout_seconds):
                await transport.send_audio(bytes(chunk))
        self._input_audio_bytes += len(chunk)

    async def commit_audio(self, *, stream_id: UUID, turn_id: UUID) -> None:
        async with self._mapping_lock:
            speech_events = self._mapper.commit_turn(
                stream_id=stream_id,
                turn_id=turn_id,
                input_audio_bytes=self._input_audio_bytes,
            )
            self._input_audio_bytes = 0
            for speech_event in speech_events:
                await self._events.put(speech_event)
            deferred = tuple(self._deferred_messages)
            self._deferred_messages.clear()
            for message in deferred:
                for mapped_event in self._mapper.map_message(message):
                    await self._events.put(mapped_event)

        async with self._transport_lock:
            transport = self._require_transport()
            async with asyncio.timeout(self._config.request_timeout_seconds):
                await transport.end_audio_stream()

    async def cancel_response(self, response_id: UUID, reason: str) -> None:
        async with self._mapping_lock:
            for event in self._mapper.cancel_response(response_id, reason):
                await self._events.put(event)

    @staticmethod
    def _requires_turn(message: types.LiveServerMessage) -> bool:
        return any(
            value is not None
            for value in (
                message.server_content,
                message.tool_call,
                message.tool_call_cancellation,
            )
        )

    async def _map_and_queue(self, message: types.LiveServerMessage) -> None:
        async with self._mapping_lock:
            if self._requires_turn(message) and not self._mapper.has_turn:
                if len(self._deferred_messages) >= 32:
                    raise InvalidGeminiLiveMessage("too many messages arrived before input commit")
                self._deferred_messages.append(message)
                return
            for event in self._mapper.map_message(message):
                await self._events.put(event)

    async def _record_resumption(self, message: types.LiveServerMessage) -> None:
        update = message.session_resumption_update
        if update is None:
            return
        if update.resumable is True and update.new_handle:
            self._resume_handle = update.new_handle
            await self._resume_store.save(self._session.id, update.new_handle)
            return
        self._resume_handle = None
        await self._resume_store.delete(self._session.id)

    async def _reconnect(self) -> None:
        if self._setup is None or not self._resume_handle:
            raise GeminiLiveAdapterError("Gemini Live session cannot be resumed.")
        async with self._transport_lock:
            previous = self._require_transport()
            async with asyncio.timeout(self._config.request_timeout_seconds):
                await previous.close()
            last_error: Exception | None = None
            for _ in range(self._config.reconnect_attempts + 1):
                setup = self._build_setup_from_previous(self._resume_handle)
                try:
                    self._transport = await self._connect(setup)
                    self._setup = setup
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    last_error = error
            raise GeminiLiveAdapterError("Gemini Live session could not reconnect.") from last_error

    def _build_setup_from_previous(self, resume_handle: str) -> GeminiLiveSetup:
        if self._setup is None:
            raise RuntimeError("Gemini Live setup is unavailable")
        return GeminiLiveSetup(
            model=self._setup.model,
            locale=self._setup.locale,
            system_instruction=self._setup.system_instruction,
            max_output_tokens=self._setup.max_output_tokens,
            input_transcription=self._setup.input_transcription,
            output_transcription=self._setup.output_transcription,
            context_window_trigger_tokens=self._setup.context_window_trigger_tokens,
            context_window_target_tokens=self._setup.context_window_target_tokens,
            resume_handle=resume_handle,
            tools=self._setup.tools,
        )

    async def _receive_connection(self, transport: GeminiLiveTransport) -> bool:
        iterator = transport.receive()
        while not self._closed:
            try:
                message = await asyncio.wait_for(
                    anext(iterator), timeout=self._config.idle_timeout_seconds
                )
            except StopAsyncIteration:
                return True
            await self._record_resumption(message)
            await self._map_and_queue(message)
            if message.go_away is not None:
                return True
        return False

    async def _receive_loop(self) -> None:
        try:
            while not self._closed:
                transport = self._require_transport()
                should_reconnect = await self._receive_connection(transport)
                if not should_reconnect or self._closed:
                    return
                await self._reconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._events.put(
                _Failure(GeminiLiveAdapterError("Gemini Live provider is unavailable."))
            )

    async def events(self) -> AsyncIterator[ProviderOutput]:
        while True:
            event = await self._events.get()
            if event is self._CLOSED:
                return
            if isinstance(event, _Failure):
                raise event.error from None
            if isinstance(event, ProviderControl | ProviderAudio):
                yield event

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._receive_task is not None:
            self._receive_task.cancel()
            await asyncio.gather(self._receive_task, return_exceptions=True)
        async with self._transport_lock:
            if self._transport is not None:
                try:
                    async with asyncio.timeout(self._config.request_timeout_seconds):
                        await self._transport.close()
                except TimeoutError:
                    pass
                self._transport = None
        await self._resume_store.delete(self._session.id)
        try:
            self._events.put_nowait(self._CLOSED)
        except asyncio.QueueFull:
            _ = self._events.get_nowait()
            self._events.put_nowait(self._CLOSED)


class GeminiLiveAdapterFactory:
    """Callable composition object accepted by ``RealtimeGateway``."""

    def __init__(
        self,
        config: GeminiLiveConfig,
        *,
        transport_factory: GeminiLiveTransportFactory | None = None,
        resume_store: GeminiLiveResumeStore | None = None,
    ) -> None:
        self._config = config
        self._transport_factory = transport_factory
        self._resume_store = resume_store or InMemoryGeminiLiveResumeStore()

    def __call__(self, session: RealtimeSession) -> GeminiLiveAdapter:
        return GeminiLiveAdapter(
            session,
            self._config,
            transport_factory=self._transport_factory,
            resume_store=self._resume_store,
        )
