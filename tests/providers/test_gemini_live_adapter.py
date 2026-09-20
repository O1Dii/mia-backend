from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from app.modules.realtime.contracts import (
    INPUT_AUDIO_FORMAT,
    OUTPUT_AUDIO_FORMAT,
    ConfigurePayload,
)
from app.modules.realtime.provider import ProviderControl, ProviderOutput
from app.modules.realtime.sessions import RealtimeSession, RealtimeSessionStatus
from app.providers.ai.base import ToolDefinition
from app.providers.ai.gemini_live import (
    GeminiLiveAdapter,
    GeminiLiveAdapterError,
    GeminiLiveConfig,
    GeminiLiveSetup,
    GeminiLiveTransport,
    InMemoryGeminiLiveResumeStore,
)
from app.providers.ai.gemini_live.adapter import _MAX_DEFERRED_MESSAGES
from google.genai import types  # type: ignore[import-untyped]
from pydantic import SecretStr

SESSION_ID = UUID("10000000-0000-4000-8000-000000000016")
USER_ID = UUID("90000000-0000-4000-8000-000000000016")
CONVERSATION_ID = UUID("20000000-0000-4000-8000-000000000016")
TURN_ID = UUID("40000000-0000-4000-8000-000000000016")
STREAM_ID = UUID("30000000-0000-4000-8000-000000000016")


def realtime_session() -> RealtimeSession:
    return RealtimeSession(
        id=SESSION_ID,
        user_id=USER_ID,
        conversation_id=CONVERSATION_ID,
        locale="ru-BY",
        input_audio=INPUT_AUDIO_FORMAT,
        output_audio=OUTPUT_AUDIO_FORMAT,
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
        status=RealtimeSessionStatus.CONNECTED,
    )


def configure_payload() -> ConfigurePayload:
    return ConfigurePayload(
        protocol_version=1,
        conversation_id=CONVERSATION_ID,
        locale="ru-BY",
        input_audio=(INPUT_AUDIO_FORMAT,),
        output_audio=(OUTPUT_AUDIO_FORMAT,),
        capabilities=("barge_in", "input_transcript", "output_transcript"),
    )


def live_config(*, request_timeout_seconds: float = 1.0) -> GeminiLiveConfig:
    return GeminiLiveConfig(
        api_key=SecretStr("credential-used-only-by-fake"),
        model="configured-live-model",
        system_instruction="Respond helpfully.",
        request_timeout_seconds=request_timeout_seconds,
        idle_timeout_seconds=5.0,
        tools=(
            ToolDefinition(
                name="system.get_current_datetime",
                description="Return local time.",
                parameters={"type": "object"},
            ),
        ),
    )


class QueueTransport:
    _CLOSED = object()

    def __init__(self) -> None:
        self.sent_audio: list[bytes] = []
        self.activity_starts = 0
        self.activity_ends = 0
        self.closed = False
        self.tool_responses: list[tuple[str, str, dict[str, object]]] = []
        self.incoming: asyncio.Queue[types.LiveServerMessage | object] = asyncio.Queue()

    async def send_audio(self, chunk: bytes) -> None:
        self.sent_audio.append(chunk)

    async def start_activity(self) -> None:
        self.activity_starts += 1

    async def end_activity(self) -> None:
        self.activity_ends += 1

    async def send_tool_response(
        self, *, call_id: str, name: str, response: Mapping[str, object]
    ) -> None:
        self.tool_responses.append((call_id, name, dict(response)))

    async def push(self, message: types.LiveServerMessage) -> None:
        await self.incoming.put(message)

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        while True:
            message = await self.incoming.get()
            if message is self._CLOSED:
                return
            if isinstance(message, types.LiveServerMessage):
                yield message

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.incoming.put(self._CLOSED)


class RecordingFactory:
    def __init__(self) -> None:
        self.setups: list[GeminiLiveSetup] = []
        self.transports: list[QueueTransport] = []
        self.reconnected = asyncio.Event()

    async def connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport:
        self.setups.append(setup)
        transport = QueueTransport()
        self.transports.append(transport)
        if len(self.transports) > 1:
            self.reconnected.set()
        return transport


async def next_output(events: AsyncIterator[ProviderOutput]) -> ProviderOutput:
    return await asyncio.wait_for(anext(events), timeout=1.0)


async def test_adapter_configures_pcm_transcripts_tools_and_streams_audio() -> None:
    factory = RecordingFactory()
    adapter = GeminiLiveAdapter(
        realtime_session(),
        live_config(),
        transport_factory=factory,
    )
    await adapter.configure(configure_payload())

    setup = factory.setups[0]
    assert setup.model == "configured-live-model"
    assert setup.locale == "ru-BY"
    assert setup.system_instruction.endswith("User locale: ru-BY.")
    assert setup.input_transcription is True
    assert setup.output_transcription is True
    assert setup.resume_handle is None
    assert [tool.name for tool in setup.tools] == ["system.get_current_datetime"]

    chunk = bytes(range(256)) * 5
    await adapter.send_audio(chunk)
    await adapter.commit_audio(stream_id=STREAM_ID, turn_id=TURN_ID)
    assert factory.transports[0].sent_audio == [chunk]
    assert factory.transports[0].activity_starts == 1
    assert factory.transports[0].activity_ends == 1

    events = adapter.events()
    speech_started = await next_output(events)
    speech_stopped = await next_output(events)
    assert isinstance(speech_started, ProviderControl)
    assert speech_started.event_type == "input_audio.speech_started"
    assert isinstance(speech_stopped, ProviderControl)
    assert speech_stopped.payload["offset_ms"] == 40

    await factory.transports[0].push(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                model_turn=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            inline_data=types.Blob(
                                data=bytes(960), mime_type="audio/pcm;rate=24000"
                            )
                        )
                    ],
                ),
                generation_complete=True,
                turn_complete=True,
            ),
            usage_metadata=types.UsageMetadata(
                prompt_token_count=10,
                response_token_count=4,
            ),
        )
    )
    emitted: list[ProviderOutput] = []
    while True:
        output = await next_output(events)
        emitted.append(output)
        if isinstance(output, ProviderControl) and output.event_type == "usage.updated":
            break
    assert [item.event_type for item in emitted if isinstance(item, ProviderControl)] == [
        "response.started",
        "response.audio.started",
        "response.audio.completed",
        "turn.completed",
        "usage.updated",
    ]
    await adapter.close()


async def test_adapter_sends_only_explicit_tool_result_with_original_wire_id() -> None:
    factory = RecordingFactory()
    adapter = GeminiLiveAdapter(realtime_session(), live_config(), transport_factory=factory)
    await adapter.configure(configure_payload())
    await adapter.send_audio(bytes(1280))
    await adapter.commit_audio(stream_id=STREAM_ID, turn_id=TURN_ID)
    events = adapter.events()
    speech_start = await next_output(events)
    speech_stop = await next_output(events)
    assert isinstance(speech_start, ProviderControl)
    assert isinstance(speech_stop, ProviderControl)
    assert speech_start.event_type == "input_audio.speech_started"
    assert speech_stop.event_type == "input_audio.speech_stopped"
    await factory.transports[0].push(
        types.LiveServerMessage(
            tool_call=types.LiveServerToolCall(
                function_calls=[
                    types.FunctionCall(
                        id="opaque-wire-id",
                        name="system.get_current_datetime",
                        args={"timezone": "Europe/Minsk"},
                    )
                ]
            )
        )
    )
    response_start = await next_output(events)
    assert isinstance(response_start, ProviderControl)
    assert response_start.event_type == "response.started"
    requested = await next_output(events)
    assert isinstance(requested, ProviderControl)
    assert requested.event_type == "tool.call.started"
    call_id = UUID(str(requested.payload["call_id"]))
    assert factory.transports[0].tool_responses == []
    await adapter.send_tool_result(call_id, {"local_datetime": "2026-09-13T13:00:00+03:00"})
    assert factory.transports[0].tool_responses == [
        (
            "opaque-wire-id",
            "system.get_current_datetime",
            {"local_datetime": "2026-09-13T13:00:00+03:00"},
        )
    ]
    with pytest.raises(ValueError, match="no longer active"):
        await adapter.send_tool_result(call_id, {"local_datetime": "again"})
    await adapter.close()


async def test_goaway_reconnects_with_latest_ephemeral_handle() -> None:
    factory = RecordingFactory()
    resume_store = InMemoryGeminiLiveResumeStore()
    adapter = GeminiLiveAdapter(
        realtime_session(),
        live_config(),
        transport_factory=factory,
        resume_store=resume_store,
    )
    await adapter.configure(configure_payload())
    first = factory.transports[0]
    await first.push(
        types.LiveServerMessage(
            session_resumption_update=types.LiveServerSessionResumptionUpdate(
                resumable=True,
                new_handle="opaque-test-resumption-handle",
                last_consumed_client_message_index=2,
            )
        )
    )
    await first.push(types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="5s")))

    await asyncio.wait_for(factory.reconnected.wait(), timeout=1.0)
    assert first.closed is True
    assert factory.setups[1].resume_handle == "opaque-test-resumption-handle"
    assert await resume_store.load(SESSION_ID) == "opaque-test-resumption-handle"

    await adapter.close()
    assert factory.transports[1].closed is True
    assert await resume_store.load(SESSION_ID) is None


class FailingTransport(QueueTransport):
    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        if False:
            yield types.LiveServerMessage()
        raise RuntimeError("provider payload containing sensitive details")


class FailingFactory:
    async def connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport:
        return FailingTransport()


async def test_receive_failure_crosses_boundary_as_fixed_sanitized_error() -> None:
    adapter = GeminiLiveAdapter(
        realtime_session(),
        live_config(),
        transport_factory=FailingFactory(),
    )
    await adapter.configure(configure_payload())
    with pytest.raises(GeminiLiveAdapterError, match="^Gemini Live provider is unavailable\\.$"):
        await next_output(adapter.events())
    await adapter.close()


class HangingSendTransport(QueueTransport):
    async def send_audio(self, chunk: bytes) -> None:
        await asyncio.Event().wait()


class HangingSendFactory:
    def __init__(self) -> None:
        self.transport = HangingSendTransport()

    async def connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport:
        return self.transport


async def test_provider_write_is_bounded_by_request_timeout() -> None:
    adapter = GeminiLiveAdapter(
        realtime_session(),
        live_config(request_timeout_seconds=0.01),
        transport_factory=HangingSendFactory(),
    )
    await adapter.configure(configure_payload())
    with pytest.raises(GeminiLiveAdapterError, match="send timed out"):
        await adapter.send_audio(bytes(1280))
    await adapter.close()


class ConnectionFailureFactory:
    async def connect(self, setup: GeminiLiveSetup) -> GeminiLiveTransport:
        raise RuntimeError("private endpoint and credential must not escape")


async def test_connection_failure_is_sanitized() -> None:
    adapter = GeminiLiveAdapter(
        realtime_session(),
        live_config(),
        transport_factory=ConnectionFailureFactory(),
    )
    with pytest.raises(GeminiLiveAdapterError, match="^Gemini Live connection failed\\.$"):
        await adapter.configure(configure_payload())
    await adapter.close()


async def test_early_transcript_waits_for_commit_and_keeps_turn_identity() -> None:
    factory = RecordingFactory()
    adapter = GeminiLiveAdapter(realtime_session(), live_config(), transport_factory=factory)
    await adapter.configure(configure_payload())
    await adapter.send_audio(bytes(1280))
    await factory.transports[0].push(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                input_transcription=types.Transcription(text="Early transcript", finished=True),
            )
        )
    )
    # Usage is an observable barrier after the pre-commit transcription.
    await factory.transports[0].push(
        types.LiveServerMessage(
            usage_metadata=types.UsageMetadata(prompt_token_count=1),
        )
    )
    events = adapter.events()
    usage = await next_output(events)
    assert isinstance(usage, ProviderControl) and usage.event_type == "usage.updated"
    await adapter.commit_audio(stream_id=STREAM_ID, turn_id=TURN_ID)
    assert cast_control(await next_output(events)).event_type == "input_audio.speech_started"
    assert cast_control(await next_output(events)).event_type == "input_audio.speech_stopped"
    transcript = cast_control(await next_output(events))
    assert transcript.event_type == "transcript.input.final"
    assert transcript.turn_id == TURN_ID
    assert transcript.payload["text"] == "Early transcript"
    await adapter.close()


def cast_control(output: ProviderOutput) -> ProviderControl:
    assert isinstance(output, ProviderControl)
    return output


async def test_barge_in_transcript_before_second_commit_uses_second_turn() -> None:
    factory = RecordingFactory()
    adapter = GeminiLiveAdapter(realtime_session(), live_config(), transport_factory=factory)
    await adapter.configure(configure_payload())
    await adapter.send_audio(bytes(640))
    await adapter.commit_audio(stream_id=STREAM_ID, turn_id=TURN_ID)
    events = adapter.events()
    await next_output(events)
    await next_output(events)
    await adapter.send_audio(bytes(640))
    await factory.transports[0].push(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                input_transcription=types.Transcription(text="New speech", finished=True),
                interrupted=True,
                turn_complete=True,
            )
        )
    )
    assert cast_control(await next_output(events)).event_type == "response.started"
    assert cast_control(await next_output(events)).event_type == "response.interrupted"
    assert cast_control(await next_output(events)).event_type == "turn.completed"
    next_turn = UUID("40000000-0000-4000-8000-000000000017")
    await adapter.commit_audio(stream_id=STREAM_ID, turn_id=next_turn)
    await next_output(events)
    await next_output(events)
    transcript = cast_control(await next_output(events))
    assert transcript.event_type == "transcript.input.final"
    assert transcript.turn_id == next_turn
    await adapter.close()


async def test_receive_idle_timeout_and_full_queue_failure_are_bounded() -> None:
    factory = RecordingFactory()
    adapter = GeminiLiveAdapter(
        realtime_session(),
        replace(live_config(), idle_timeout_seconds=0.01),
        transport_factory=factory,
    )
    await adapter.configure(configure_payload())
    with pytest.raises(GeminiLiveAdapterError, match="provider is unavailable"):
        await next_output(adapter.events())
    await adapter.close()

    adapter = GeminiLiveAdapter(
        realtime_session(),
        live_config(request_timeout_seconds=0.01),
        transport_factory=RecordingFactory(),
        event_queue_size=1,
    )
    await adapter.configure(configure_payload())
    await adapter.send_audio(bytes(640))
    with pytest.raises(GeminiLiveAdapterError, match="consumer is too slow"):
        await adapter.commit_audio(stream_id=STREAM_ID, turn_id=TURN_ID)
    await adapter.close()
    with pytest.raises(StopAsyncIteration):
        await next_output(adapter.events())


@pytest.mark.parametrize("resumable,active_turn", [(False, False), (True, True)])
async def test_goaway_does_not_replay_unconfirmed_input(
    resumable: bool,
    active_turn: bool,
) -> None:
    factory = RecordingFactory()
    store = InMemoryGeminiLiveResumeStore()
    adapter = GeminiLiveAdapter(
        realtime_session(),
        live_config(),
        transport_factory=factory,
        resume_store=store,
    )
    await adapter.configure(configure_payload())
    if active_turn:
        await adapter.send_audio(bytes(640))
        await adapter.commit_audio(stream_id=STREAM_ID, turn_id=TURN_ID)
        events = adapter.events()
        await next_output(events)
        await next_output(events)
    await factory.transports[0].push(
        types.LiveServerMessage(
            session_resumption_update=types.LiveServerSessionResumptionUpdate(
                resumable=resumable,
                new_handle="fake-resumption-handle" if resumable else None,
            ),
        )
    )
    await factory.transports[0].push(
        types.LiveServerMessage(
            go_away=types.LiveServerGoAway(
                time_left="5s",
            )
        )
    )
    with pytest.raises(GeminiLiveAdapterError, match="provider is unavailable"):
        await next_output(adapter.events())
    assert len(factory.transports) == 1
    await adapter.close()
    assert await store.load(SESSION_ID) is None


def test_config_repr_redacts_api_key_and_rejects_unsafe_tool_names() -> None:
    config = live_config()
    assert "credential-used-only-by-fake" not in repr(config)
    with pytest.raises(ValueError, match="namespaced"):
        GeminiLiveConfig(
            api_key=SecretStr("fake"),
            model="model",
            system_instruction="instruction",
            tools=(
                ToolDefinition(
                    name="not_namespaced",
                    description="Invalid public realtime tool name.",
                    parameters={},
                ),
            ),
        )


async def test_provider_silence_does_not_end_a_session_the_client_is_still_feeding() -> None:
    """A user thinking in silence still streams microphone frames.

    W6 found that the idle timeout counted only inbound traffic, so a Live session
    ended as "provider unavailable" after 90 s of the assistant simply having
    nothing to say. The timeout now guards a socket quiet in both directions.
    """
    adapter = GeminiLiveAdapter(
        realtime_session(),
        replace(live_config(), idle_timeout_seconds=0.02),
        transport_factory=RecordingFactory(),
    )
    await adapter.configure(configure_payload())
    events = adapter.events()

    async def keep_streaming() -> None:
        for _ in range(40):
            await adapter.send_audio(bytes(640))
            await asyncio.sleep(0.01)

    streaming = asyncio.create_task(keep_streaming())
    try:
        # No event at all is the pass condition: a failure would surface here.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(next_output(events), timeout=0.2)
    finally:
        streaming.cancel()
        await asyncio.gather(streaming, return_exceptions=True)
        await adapter.close()


async def test_a_flood_of_pre_commit_turns_degrades_instead_of_ending_the_session() -> None:
    """Gemini's own VAD can open turns the client never commits.

    W6 watched a real device session die after ~30 s with "too many messages
    arrived before input commit": room noise triggered the provider's activity
    detection while the client's local VAD stayed silent, so no commit ever
    drained the buffer. Dropping the oldest deferred message is survivable;
    ending the voice session is not.
    """
    factory = RecordingFactory()
    adapter = GeminiLiveAdapter(realtime_session(), live_config(), transport_factory=factory)
    await adapter.configure(configure_payload())
    events = adapter.events()

    for _ in range(_MAX_DEFERRED_MESSAGES * 3):
        await factory.transports[0].push(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(text="noise"),
                ),
            )
        )
    await asyncio.sleep(0.05)
    assert adapter._dropped_deferred_messages > 0

    # The session is still alive: committing a turn still drains and produces events.
    collected: list[ProviderOutput] = []

    async def consume() -> None:
        async for event in events:
            collected.append(event)

    consumer = asyncio.create_task(consume())
    await adapter.send_audio(bytes(640))
    await adapter.commit_audio(stream_id=STREAM_ID, turn_id=TURN_ID)
    await asyncio.sleep(0.05)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await adapter.close()

    assert any(
        isinstance(event, ProviderControl) and event.event_type == "input_audio.speech_started"
        for event in collected
    )
