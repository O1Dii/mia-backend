from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from app.modules.realtime.provider import ProviderAudio, ProviderControl, ProviderOutput
from app.providers.ai.base import ToolDefinition
from app.providers.ai.gemini_live.mapping import (
    GeminiLiveEventMapper,
    InvalidGeminiLiveMessage,
)
from google.genai import types  # type: ignore[import-untyped]
from tests.realtime.test_contracts import server_event_validator

TURN_ID = UUID("40000000-0000-4000-8000-000000000016")
STREAM_ID = UUID("30000000-0000-4000-8000-000000000016")
SESSION_ID = UUID("10000000-0000-4000-8000-000000000016")
FIXTURE = Path(__file__).parent / "fixtures" / "gemini_live_messages.json"


def load_messages() -> list[types.LiveServerMessage]:
    raw = cast(dict[str, list[object]], json.loads(FIXTURE.read_text()))
    return [types.LiveServerMessage.model_validate(item) for item in raw["messages"]]


def mapper() -> GeminiLiveEventMapper:
    return GeminiLiveEventMapper(
        locale="ru-BY",
        model_alias="voice-default",
        tools=(
            ToolDefinition(
                name="system.get_current_datetime",
                description="Return local time.",
                parameters={"type": "object"},
            ),
        ),
    )


def validate_controls(outputs: list[ProviderOutput]) -> None:
    validator = server_event_validator()
    sequence = 0
    for output in outputs:
        if not isinstance(output, ProviderControl):
            continue
        sequence += 1
        event: dict[str, object] = {
            "v": 1,
            "type": output.event_type,
            "session_id": str(SESSION_ID),
            "seq": sequence,
            "at": "2026-09-11T12:00:00Z",
            "payload": dict(output.payload),
        }
        if output.turn_id is not None:
            event["turn_id"] = str(output.turn_id)
        if output.response_id is not None:
            event["response_id"] = str(output.response_id)
        validator.validate(event)


def test_fixture_maps_transcripts_audio_tools_usage_and_completion() -> None:
    live_mapper = mapper()
    outputs: list[ProviderOutput] = list(
        live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=1280)
    )
    for message in load_messages():
        outputs.extend(live_mapper.map_message(message))

    controls = [item for item in outputs if isinstance(item, ProviderControl)]
    assert [item.event_type for item in controls] == [
        "input_audio.speech_started",
        "input_audio.speech_stopped",
        "transcript.input.delta",
        "transcript.input.final",
        "response.started",
        "tool.call.started",
        "tool.call.completed",
        "response.audio.started",
        "transcript.output.delta",
        "transcript.output.final",
        "usage.updated",
        "response.audio.completed",
        "turn.completed",
    ]
    audio = [item for item in outputs if isinstance(item, ProviderAudio)]
    assert audio == [
        ProviderAudio(data=bytes(range(48)), response_id=live_mapper.response_id(TURN_ID))
    ]
    assert controls[3].payload["text"] == "Привет мир"
    assert controls[9].payload["text"] == "Здравствуйте!"
    assert controls[10].payload == {
        "provider_alias": "gemini",
        "model_alias": "voice-default",
        "input_tokens": 12,
        "output_tokens": 7,
        "input_audio_ms": 40,
        "output_audio_ms": 1,
    }
    assert controls[11].payload["duration_ms"] == 1
    validate_controls(outputs)


def test_provider_interruption_finishes_old_turn_before_pending_barge_in_turn() -> None:
    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)
    first_response = live_mapper.response_id(TURN_ID)
    started = live_mapper.map_message(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                model_turn=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            inline_data=types.Blob(data=bytes(48), mime_type="audio/pcm;rate=24000")
                        )
                    ],
                )
            )
        )
    )
    assert [type(item) for item in started] == [ProviderControl, ProviderControl, ProviderAudio]

    second_turn = UUID("40000000-0000-4000-8000-000000000017")
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=second_turn, input_audio_bytes=640)
    interrupted = live_mapper.map_message(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(interrupted=True, turn_complete=True)
        )
    )
    interruption = cast(ProviderControl, interrupted[0])
    completion = cast(ProviderControl, interrupted[1])
    assert interruption.event_type == "response.interrupted"
    assert interruption.response_id == first_response
    assert interruption.payload == {"reason": "barge_in", "delivered_audio_ms": 1}
    assert completion.event_type == "turn.completed"
    assert completion.payload == {"status": "interrupted"}


def test_local_cancel_is_idempotent_and_provider_payload_errors_are_sanitizable() -> None:
    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)
    response_id = live_mapper.response_id(TURN_ID)
    first = live_mapper.cancel_response(response_id, "user_request")
    assert len(first) == 1
    assert first[0].payload == {"reason": "user_request", "delivered_audio_ms": 0}
    assert live_mapper.cancel_response(response_id, "user_request") == ()

    # Calls arriving after local cancellation are stale and never authorized.
    assert (
        live_mapper.map_message(
            types.LiveServerMessage(
                tool_call=types.LiveServerToolCall(
                    function_calls=[
                        types.FunctionCall(id="stale", name="system.get_current_datetime", args={}),
                    ]
                ),
            )
        )
        == ()
    )

    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)

    invalid = types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[types.FunctionCall(id="bad", name="undeclared.tool", args={})]
        )
    )
    try:
        live_mapper.map_message(invalid)
    except InvalidGeminiLiveMessage as error:
        assert "undeclared.tool" not in str(error)
    else:
        raise AssertionError("undeclared provider function must fail mapping")


def test_repeated_transcript_fragments_are_preserved_and_thoughts_are_discarded() -> None:
    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)
    outputs: list[ProviderOutput] = []
    for fragment in ("ha", "ha"):
        outputs.extend(
            live_mapper.map_message(
                types.LiveServerMessage(
                    server_content=types.LiveServerContent(
                        output_transcription=types.Transcription(text=fragment),
                        model_turn=types.Content(
                            parts=[types.Part(text="private thought", thought=True)]
                        ),
                    ),
                )
            )
        )
    outputs.extend(
        live_mapper.map_message(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    generation_complete=True, turn_complete=True
                ),
            )
        )
    )
    finals = [
        event
        for event in outputs
        if isinstance(event, ProviderControl) and event.event_type == "transcript.output.final"
    ]
    assert finals[0].payload["text"] == "haha"
    assert "private thought" not in repr(outputs)
    validate_controls(outputs)


def test_multi_part_audio_and_transcript_are_mapped_and_cancel_suppresses_late_audio() -> None:
    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)
    message = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            output_transcription=types.Transcription(text="Hello"),
            model_turn=types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(data=bytes(48), mime_type="audio/pcm;rate=24000")
                    ),
                    types.Part(
                        inline_data=types.Blob(data=bytes(96), mime_type="audio/pcm;rate=24000")
                    ),
                ]
            ),
        )
    )
    events = list(live_mapper.map_message(message))
    assert len([event for event in events if isinstance(event, ProviderAudio)]) == 2
    assert any(
        isinstance(event, ProviderControl) and event.event_type == "transcript.output.delta"
        for event in events
    )
    live_mapper.cancel_response(live_mapper.response_id(TURN_ID), "barge_in")
    assert live_mapper.map_message(message) == ()
    complete = live_mapper.map_message(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(interrupted=True, turn_complete=True),
        )
    )
    assert len(complete) == 1
    assert isinstance(complete[0], ProviderControl)
    assert complete[0].payload == {"status": "interrupted"}
    validate_controls(events + list(complete))


@pytest.mark.parametrize(
    "mime,data",
    [
        ("audio/pcm;rate=16000", bytes(48)),
        ("audio/pcm;rate=24000", bytes(3)),
    ],
)
def test_invalid_output_pcm_is_rejected(mime: str, data: bytes) -> None:
    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)
    with pytest.raises(InvalidGeminiLiveMessage):
        live_mapper.map_message(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=data,
                                    mime_type=mime,
                                )
                            )
                        ]
                    ),
                )
            )
        )


def test_large_provider_audio_is_split_to_public_frame_limit_without_data_loss() -> None:
    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)
    pcm = bytes(range(256)) * 40
    outputs = live_mapper.map_message(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                model_turn=types.Content(
                    parts=[
                        types.Part(
                            inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000")
                        ),
                    ]
                )
            ),
        )
    )
    audio = [event.data for event in outputs if isinstance(event, ProviderAudio)]
    assert [len(frame) for frame in audio] == [4096, 4096, 2048]
    assert b"".join(audio) == pcm


def _input_transcription(text: str, *, finished: bool = False) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text=text, finished=finished),
        ),
    )


def test_input_transcription_after_a_final_fragment_starts_the_next_transcript() -> None:
    """Gemini transcribes continuously; the next utterance may arrive before our commit.

    W6 found that the mapper treated such a fragment as a protocol violation, which
    failed the whole realtime session as soon as a user spoke a second time.
    """
    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)

    first = live_mapper.map_message(_input_transcription("where am I", finished=True))
    finals = [
        event
        for event in first
        if isinstance(event, ProviderControl) and event.event_type == "transcript.input.final"
    ]
    assert [event.payload["text"] for event in finals] == ["where am I"]

    second = live_mapper.map_message(_input_transcription("and what time is it"))
    deltas = [
        event
        for event in second
        if isinstance(event, ProviderControl) and event.event_type == "transcript.input.delta"
    ]
    assert [event.payload["text"] for event in deltas] == ["and what time is it"]

    closing = live_mapper.map_message(_input_transcription("", finished=True))
    closing_finals = [
        event
        for event in closing
        if isinstance(event, ProviderControl) and event.event_type == "transcript.input.final"
    ]
    # The new transcript stands on its own; the finished one is not re-sent or appended to.
    assert [event.payload["text"] for event in closing_finals] == ["and what time is it"]
    validate_controls([*first, *second, *closing])


def test_output_transcription_after_a_final_fragment_is_still_rejected() -> None:
    """An output transcript belongs to one response, so appending to it stays invalid."""
    live_mapper = mapper()
    live_mapper.commit_turn(stream_id=STREAM_ID, turn_id=TURN_ID, input_audio_bytes=640)
    live_mapper.map_message(
        types.LiveServerMessage(
            server_content=types.LiveServerContent(
                output_transcription=types.Transcription(text="Minsk.", finished=True),
            ),
        )
    )
    with pytest.raises(InvalidGeminiLiveMessage):
        live_mapper.map_message(
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(text=" And more."),
                ),
            )
        )
