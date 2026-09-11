from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
        self.audio_stream_ends = 0
        self.closed = False
        self.incoming: asyncio.Queue[types.LiveServerMessage | object] = asyncio.Queue()

    async def send_audio(self, chunk: bytes) -> None:
        self.sent_audio.append(chunk)

    async def end_audio_stream(self) -> None:
        self.audio_stream_ends += 1

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
    assert factory.transports[0].audio_stream_ends == 1

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
    with pytest.raises(TimeoutError):
        await adapter.send_audio(bytes(1280))
    await adapter.close()


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
