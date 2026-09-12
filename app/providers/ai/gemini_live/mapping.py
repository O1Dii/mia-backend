"""Deterministic Gemini Live message to MIA realtime event mapping."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from uuid import UUID, uuid5

from google.genai import types  # type: ignore[import-untyped]

from app.modules.realtime.contracts import OUTPUT_AUDIO_FORMAT
from app.modules.realtime.provider import ProviderAudio, ProviderControl, ProviderOutput
from app.providers.ai.base import ToolDefinition
from app.providers.ai.gemini_live.transport import normalized_arguments


class InvalidGeminiLiveMessage(ValueError):
    """The provider emitted data that cannot satisfy the stable MIA contract."""


@dataclass(slots=True)
class _TurnState:
    stream_id: UUID
    turn_id: UUID
    response_id: UUID
    input_audio_bytes: int
    response_started: bool = False
    audio_started: bool = False
    audio_completed: bool = False
    output_audio_bytes: int = 0
    input_transcript: str = ""
    output_transcript: str = ""
    input_transcript_final: bool = False
    output_transcript_final: bool = False
    interrupted_reason: str | None = None
    interruption_emitted: bool = False

    @property
    def segment_id(self) -> UUID:
        return uuid5(self.response_id, "gemini-live-audio-segment")


@dataclass(frozen=True, slots=True)
class _ToolState:
    state: _TurnState
    call_id: UUID
    name: str


class GeminiLiveEventMapper:
    """Stateful mapper for one provider connection, independent of transport timing."""

    def __init__(
        self,
        *,
        locale: str,
        model_alias: str,
        tools: tuple[ToolDefinition, ...],
        output_frame_bytes: int = 4096,
    ) -> None:
        if output_frame_bytes < 2 or output_frame_bytes % 2:
            raise ValueError("output frame limit must align with PCM16 samples")
        self._output_frame_bytes = output_frame_bytes
        self._locale = locale
        self._model_alias = model_alias
        self._tools = {tool.name: tool for tool in tools}
        self._turns: deque[_TurnState] = deque()
        self._last_state: _TurnState | None = None
        self._tool_calls: dict[str, _ToolState] = {}
        self._fallback_call_sequence = 0

    @staticmethod
    def response_id(turn_id: UUID) -> UUID:
        return uuid5(turn_id, "gemini-live-response")

    @property
    def has_turn(self) -> bool:
        return bool(self._turns)

    @property
    def has_completed_turn(self) -> bool:
        return self._last_state is not None

    def commit_turn(
        self,
        *,
        stream_id: UUID,
        turn_id: UUID,
        input_audio_bytes: int,
    ) -> tuple[ProviderControl, ProviderControl]:
        if input_audio_bytes < 0:
            raise ValueError("input audio bytes cannot be negative")
        if any(state.turn_id == turn_id for state in self._turns):
            raise ValueError("turn is already active")
        if len(self._turns) >= 8:
            raise InvalidGeminiLiveMessage("too many pending Gemini Live turns")
        state = _TurnState(
            stream_id=stream_id,
            turn_id=turn_id,
            response_id=self.response_id(turn_id),
            input_audio_bytes=input_audio_bytes,
        )
        self._turns.append(state)
        duration_ms = input_audio_bytes // 32
        return (
            ProviderControl(
                "input_audio.speech_started",
                {"stream_id": str(stream_id), "offset_ms": 0},
                turn_id=turn_id,
            ),
            ProviderControl(
                "input_audio.speech_stopped",
                {"stream_id": str(stream_id), "offset_ms": duration_ms},
                turn_id=turn_id,
            ),
        )

    def cancel_response(self, response_id: UUID, reason: str) -> tuple[ProviderControl, ...]:
        state = next(
            (candidate for candidate in self._turns if candidate.response_id == response_id),
            None,
        )
        if state is None or state.interruption_emitted:
            return ()
        state.interrupted_reason = "barge_in" if reason == "barge_in" else "user_request"
        state.interruption_emitted = True
        return (self._interruption(state),)

    def map_message(self, message: types.LiveServerMessage) -> tuple[ProviderOutput, ...]:
        outputs: list[ProviderOutput] = []
        usage_state = self._turns[0] if self._turns else self._last_state

        if message.tool_call is not None:
            outputs.extend(self._map_tool_calls(message.tool_call))
        if message.tool_call_cancellation is not None:
            outputs.extend(self._map_tool_cancellations(message.tool_call_cancellation))
        if message.server_content is not None:
            outputs.extend(self._map_server_content(message.server_content))
        if message.usage_metadata is not None:
            outputs.append(self._map_usage(message.usage_metadata, usage_state))
        return tuple(outputs)

    def _current(self) -> _TurnState:
        if not self._turns:
            raise InvalidGeminiLiveMessage("Gemini emitted turn data before an input commit")
        return self._turns[0]

    def _input_state(self) -> _TurnState:
        if not self._turns:
            if self._last_state is not None:
                return self._last_state
            raise InvalidGeminiLiveMessage("Gemini emitted a transcript before an input commit")
        return self._turns[-1]

    @staticmethod
    def _append_transcript(previous: str, fragment: str) -> str:
        return previous + fragment

    def _map_transcription(
        self,
        transcription: types.Transcription,
        *,
        state: _TurnState,
        input_audio: bool,
    ) -> list[ProviderControl]:
        text = transcription.text or ""
        finished = transcription.finished is True
        current = state.input_transcript if input_audio else state.output_transcript
        combined = self._append_transcript(current, text)
        if len(combined) > 65_536:
            raise InvalidGeminiLiveMessage("Gemini Live transcript exceeded its limit")
        if input_audio:
            state.input_transcript = combined
            already_final = state.input_transcript_final
        else:
            state.output_transcript = combined
            already_final = state.output_transcript_final

        if already_final:
            if text:
                raise InvalidGeminiLiveMessage("Gemini appended text after a final transcript")
            return []
        response_id = None if input_audio else state.response_id
        if finished:
            if input_audio:
                state.input_transcript_final = True
            else:
                state.output_transcript_final = True
            return [
                ProviderControl(
                    "transcript.input.final" if input_audio else "transcript.output.final",
                    {"text": combined, "language": self._locale},
                    turn_id=state.turn_id,
                    response_id=response_id,
                )
            ]
        if not text:
            return []
        return [
            ProviderControl(
                "transcript.input.delta" if input_audio else "transcript.output.delta",
                {"text": text, "language": self._locale},
                turn_id=state.turn_id,
                response_id=response_id,
            )
        ]

    @staticmethod
    def _is_pcm_24khz(mime_type: str | None) -> bool:
        if mime_type is None:
            return False
        pieces = [piece.strip().lower() for piece in mime_type.split(";")]
        return pieces[0] == "audio/pcm" and "rate=24000" in pieces[1:]

    def _response_started(self, state: _TurnState) -> list[ProviderControl]:
        if state.response_started:
            return []
        state.response_started = True
        return [
            ProviderControl(
                "response.started",
                {"modalities": ["audio"]},
                turn_id=state.turn_id,
                response_id=state.response_id,
            )
        ]

    def _map_model_turn(
        self,
        model_turn: types.Content,
        state: _TurnState,
    ) -> list[ProviderOutput]:
        outputs: list[ProviderOutput] = []
        for part in model_turn.parts or ():
            if part.thought is True:
                continue
            if part.text:
                raise InvalidGeminiLiveMessage("Gemini returned text for an audio-only session")
            if part.function_call is not None:
                raise InvalidGeminiLiveMessage("Gemini nested a function call in server content")
            blob = part.inline_data
            if blob is None:
                continue
            if not self._is_pcm_24khz(blob.mime_type) or blob.data is None:
                raise InvalidGeminiLiveMessage("Gemini returned an unsupported audio format")
            data = bytes(blob.data)
            if len(data) % 2 or state.audio_completed:
                raise InvalidGeminiLiveMessage("Gemini returned invalid PCM segment data")
            outputs.extend(self._response_started(state))
            if not state.audio_started:
                state.audio_started = True
                outputs.append(
                    ProviderControl(
                        "response.audio.started",
                        {
                            "segment_id": str(state.segment_id),
                            "format": OUTPUT_AUDIO_FORMAT.model_dump(mode="json"),
                        },
                        turn_id=state.turn_id,
                        response_id=state.response_id,
                    )
                )
            if data:
                state.output_audio_bytes += len(data)
                for offset in range(0, len(data), self._output_frame_bytes):
                    outputs.append(
                        ProviderAudio(
                            data=data[offset : offset + self._output_frame_bytes],
                            response_id=state.response_id,
                        )
                    )
        return outputs

    def _finalize_transcript(
        self, state: _TurnState, *, input_audio: bool
    ) -> list[ProviderControl]:
        text = state.input_transcript if input_audio else state.output_transcript
        final = state.input_transcript_final if input_audio else state.output_transcript_final
        if final or not text:
            return []
        if input_audio:
            state.input_transcript_final = True
        else:
            state.output_transcript_final = True
        return [
            ProviderControl(
                "transcript.input.final" if input_audio else "transcript.output.final",
                {"text": text, "language": self._locale},
                turn_id=state.turn_id,
                response_id=None if input_audio else state.response_id,
            )
        ]

    @staticmethod
    def _interruption(state: _TurnState) -> ProviderControl:
        return ProviderControl(
            "response.interrupted",
            {
                "reason": state.interrupted_reason or "provider_cancelled",
                "delivered_audio_ms": state.output_audio_bytes // 48,
            },
            turn_id=state.turn_id,
            response_id=state.response_id,
        )

    def _complete_audio(self, state: _TurnState) -> list[ProviderControl]:
        if not state.audio_started or state.audio_completed or state.interrupted_reason is not None:
            return []
        state.audio_completed = True
        return [
            ProviderControl(
                "response.audio.completed",
                {
                    "segment_id": str(state.segment_id),
                    "audio_bytes": state.output_audio_bytes,
                    "duration_ms": state.output_audio_bytes // 48,
                },
                turn_id=state.turn_id,
                response_id=state.response_id,
            )
        ]

    def _map_server_content(self, content: types.LiveServerContent) -> list[ProviderOutput]:
        outputs: list[ProviderOutput] = []
        if content.input_transcription is not None:
            outputs.extend(
                self._map_transcription(
                    content.input_transcription,
                    state=self._input_state(),
                    input_audio=True,
                )
            )

        if not any(
            (
                content.model_turn,
                content.output_transcription,
                content.interrupted,
                content.generation_complete,
                content.turn_complete,
            )
        ):
            return outputs

        state = self._current()
        if content.interrupted is True:
            state.interrupted_reason = state.interrupted_reason or (
                "barge_in" if len(self._turns) > 1 else "provider_cancelled"
            )
            if not state.interruption_emitted:
                outputs.extend(self._response_started(state))
                state.interruption_emitted = True
                outputs.append(self._interruption(state))

        if content.output_transcription is not None and state.interrupted_reason is None:
            outputs.extend(self._response_started(state))
            outputs.extend(
                self._map_transcription(
                    content.output_transcription,
                    state=state,
                    input_audio=False,
                )
            )
        if content.model_turn is not None and state.interrupted_reason is None:
            outputs.extend(self._map_model_turn(content.model_turn, state))
        if content.generation_complete is True:
            outputs.extend(self._complete_audio(state))
            if state.interrupted_reason is None:
                outputs.extend(self._finalize_transcript(state, input_audio=False))
        if content.turn_complete is True:
            outputs.extend(self._finalize_transcript(state, input_audio=True))
            if state.interrupted_reason is None:
                outputs.extend(self._complete_audio(state))
                outputs.extend(self._finalize_transcript(state, input_audio=False))
            outputs.append(
                ProviderControl(
                    "turn.completed",
                    {
                        "status": (
                            "interrupted" if state.interrupted_reason is not None else "completed"
                        )
                    },
                    turn_id=state.turn_id,
                )
            )
            self._last_state = self._turns.popleft()
            self._tool_calls = {
                key: call for key, call in self._tool_calls.items() if call.state is not state
            }
        return outputs

    def _map_tool_calls(self, tool_call: types.LiveServerToolCall) -> list[ProviderControl]:
        state = self._current()
        if state.interrupted_reason is not None:
            return []
        outputs = self._response_started(state)
        for call in tool_call.function_calls or ():
            name = call.name or ""
            definition = self._tools.get(name)
            if definition is None:
                raise InvalidGeminiLiveMessage("Gemini requested an undeclared function")
            self._fallback_call_sequence += 1
            raw_id = call.id or f"{name}:{self._fallback_call_sequence}"
            if raw_id in self._tool_calls:
                raise InvalidGeminiLiveMessage("Gemini repeated a function call id")
            if len(self._tool_calls) >= 32:
                raise InvalidGeminiLiveMessage("too many pending Gemini Live tool calls")
            mapped_id = uuid5(state.response_id, f"gemini-tool:{raw_id}")
            self._tool_calls[raw_id] = _ToolState(state, mapped_id, name)
            outputs.append(
                ProviderControl(
                    "tool.call.started",
                    {
                        "call_id": str(mapped_id),
                        "name": name,
                        "arguments": dict(normalized_arguments(call.args)),
                        "requires_confirmation": definition.requires_confirmation,
                    },
                    turn_id=state.turn_id,
                    response_id=state.response_id,
                )
            )
        return outputs

    def _map_tool_cancellations(
        self, cancellation: types.LiveServerToolCallCancellation
    ) -> list[ProviderControl]:
        outputs: list[ProviderControl] = []
        for raw_id in cancellation.ids or ():
            call = self._tool_calls.pop(raw_id, None)
            if call is None:
                raise InvalidGeminiLiveMessage("Gemini cancelled an unknown function call")
            outputs.append(
                ProviderControl(
                    "tool.call.completed",
                    {
                        "call_id": str(call.call_id),
                        "name": call.name,
                        "status": "cancelled",
                    },
                    turn_id=call.state.turn_id,
                    response_id=call.state.response_id,
                )
            )
        return outputs

    def _map_usage(
        self,
        usage: types.UsageMetadata,
        state: _TurnState | None,
    ) -> ProviderControl:
        return ProviderControl(
            "usage.updated",
            {
                "provider_alias": "gemini",
                "model_alias": self._model_alias,
                "input_tokens": max(usage.prompt_token_count or 0, 0),
                "output_tokens": max(usage.response_token_count or 0, 0),
                "input_audio_ms": state.input_audio_bytes // 32 if state is not None else 0,
                "output_audio_ms": state.output_audio_bytes // 48 if state is not None else 0,
            },
            turn_id=state.turn_id if state is not None else None,
            response_id=state.response_id if state is not None else None,
        )
