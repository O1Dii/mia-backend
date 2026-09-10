"""Provider-neutral runtime projection of the normalized AIEvent v1 contract."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

ToolRisk = Literal["read", "write", "sensitive"]
ProviderErrorCode = Literal[
    "provider_unavailable",
    "provider_timeout",
    "provider_rate_limited",
    "provider_invalid_response",
    "provider_session_expired",
    "provider_cancelled",
    "provider_internal_error",
]
FinishReason = Literal["stop", "max_output", "tool_result", "content_filter"]
InterruptionReason = Literal["barge_in", "user_request", "disconnect", "provider_cancelled"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AIEvent:
    """Base metadata shared by every normalized provider event."""

    event_id: UUID
    seq: int
    at: datetime
    turn_id: UUID | None = None
    response_id: UUID | None = None

    event_type: ClassVar[str] = ""

    def __post_init__(self) -> None:
        if self.seq < 1:
            raise ValueError("AIEvent seq must be positive")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("AIEvent timestamp must include a timezone")

    def _require_turn(self) -> None:
        if self.turn_id is None:
            raise ValueError(f"{self.event_type} requires turn_id")

    def _require_response(self) -> None:
        self._require_turn()
        if self.response_id is None:
            raise ValueError(f"{self.event_type} requires response_id")

    def payload(self) -> dict[str, object]:
        raise NotImplementedError

    def to_contract(self) -> dict[str, object]:
        """Serialize metadata without leaking provider SDK objects."""
        result: dict[str, object] = {
            "v": 1,
            "type": self.event_type,
            "event_id": str(self.event_id),
            "seq": self.seq,
            "at": self.at.isoformat().replace("+00:00", "Z"),
            "payload": self.payload(),
        }
        if self.turn_id is not None:
            result["turn_id"] = str(self.turn_id)
        if self.response_id is not None:
            result["response_id"] = str(self.response_id)
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class TextDelta(AIEvent):
    event_type: ClassVar[str] = "text.delta"
    text: str

    def __post_init__(self) -> None:
        super(TextDelta, self).__post_init__()
        self._require_response()
        if not self.text:
            raise ValueError("text delta cannot be empty")

    def payload(self) -> dict[str, object]:
        return {"text": self.text}


@dataclass(frozen=True, slots=True, kw_only=True)
class _TranscriptEvent(AIEvent):
    text: str
    language: str | None = None

    def _transcript_payload(self) -> dict[str, object]:
        result: dict[str, object] = {"text": self.text}
        if self.language is not None:
            result["language"] = self.language
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class InputTranscriptDelta(_TranscriptEvent):
    event_type: ClassVar[str] = "transcript.input.delta"

    def __post_init__(self) -> None:
        super(InputTranscriptDelta, self).__post_init__()
        self._require_turn()
        if not self.text:
            raise ValueError("transcript delta cannot be empty")

    def payload(self) -> dict[str, object]:
        return self._transcript_payload()


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputTranscriptDelta(_TranscriptEvent):
    event_type: ClassVar[str] = "transcript.output.delta"

    def __post_init__(self) -> None:
        super(OutputTranscriptDelta, self).__post_init__()
        self._require_response()
        if not self.text:
            raise ValueError("transcript delta cannot be empty")

    def payload(self) -> dict[str, object]:
        return self._transcript_payload()


@dataclass(frozen=True, slots=True, kw_only=True)
class _TranscriptFinal(_TranscriptEvent):
    confidence: float | None = None

    def _final_payload(self) -> dict[str, object]:
        result = self._transcript_payload()
        if self.confidence is not None:
            if not 0 <= self.confidence <= 1:
                raise ValueError("transcript confidence must be between zero and one")
            result["confidence"] = self.confidence
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class InputTranscriptFinal(_TranscriptFinal):
    event_type: ClassVar[str] = "transcript.input.final"

    def __post_init__(self) -> None:
        super(InputTranscriptFinal, self).__post_init__()
        self._require_turn()
        self._final_payload()

    def payload(self) -> dict[str, object]:
        return self._final_payload()


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputTranscriptFinal(_TranscriptFinal):
    event_type: ClassVar[str] = "transcript.output.final"

    def __post_init__(self) -> None:
        super(OutputTranscriptFinal, self).__post_init__()
        self._require_response()
        self._final_payload()

    def payload(self) -> dict[str, object]:
        return self._final_payload()


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioDelta(AIEvent):
    event_type: ClassVar[str] = "audio.delta"
    audio: bytes
    sample_rate_hz: Literal[16000, 24000]
    channels: Literal[1] = 1

    def __post_init__(self) -> None:
        super(AudioDelta, self).__post_init__()
        self._require_response()

    def payload(self) -> dict[str, object]:
        return {
            "encoding": "pcm_s16le",
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "audio_base64": base64.b64encode(self.audio).decode("ascii"),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallRequested(AIEvent):
    event_type: ClassVar[str] = "tool.call.requested"
    call_id: UUID
    name: str
    arguments: Mapping[str, object]
    risk: ToolRisk
    requires_confirmation: bool

    def __post_init__(self) -> None:
        super(ToolCallRequested, self).__post_init__()
        self._require_response()

    def payload(self) -> dict[str, object]:
        return {
            "call_id": str(self.call_id),
            "name": self.name,
            "arguments": dict(self.arguments),
            "risk": self.risk,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallCancelled(AIEvent):
    event_type: ClassVar[str] = "tool.call.cancelled"
    call_id: UUID
    reason: Literal["response_cancelled", "provider_cancelled", "policy_denied", "session_closed"]

    def __post_init__(self) -> None:
        super(ToolCallCancelled, self).__post_init__()
        self._require_response()

    def payload(self) -> dict[str, object]:
        return {"call_id": str(self.call_id), "reason": self.reason}


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponseStarted(AIEvent):
    event_type: ClassVar[str] = "response.started"
    modalities: tuple[Literal["text", "audio"], ...]

    def __post_init__(self) -> None:
        super(ResponseStarted, self).__post_init__()
        self._require_response()
        if not self.modalities or len(set(self.modalities)) != len(self.modalities):
            raise ValueError("response modalities must be non-empty and unique")

    def payload(self) -> dict[str, object]:
        return {"modalities": list(self.modalities)}


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponseCompleted(AIEvent):
    event_type: ClassVar[str] = "response.completed"
    finish_reason: FinishReason

    def __post_init__(self) -> None:
        super(ResponseCompleted, self).__post_init__()
        self._require_response()

    def payload(self) -> dict[str, object]:
        return {"finish_reason": self.finish_reason}


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponseInterrupted(AIEvent):
    event_type: ClassVar[str] = "response.interrupted"
    reason: InterruptionReason

    def __post_init__(self) -> None:
        super(ResponseInterrupted, self).__post_init__()
        self._require_response()

    def payload(self) -> dict[str, object]:
        return {"reason": self.reason}


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageReported(AIEvent):
    event_type: ClassVar[str] = "usage.reported"
    provider_alias: str
    model_alias: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    input_audio_ms: int = 0
    output_audio_ms: int = 0

    def __post_init__(self) -> None:
        super(UsageReported, self).__post_init__()
        counters = (
            self.input_tokens,
            self.output_tokens,
            self.input_audio_ms,
            self.output_audio_ms,
        )
        if min(counters) < 0:
            raise ValueError("usage counters cannot be negative")

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "provider_alias": self.provider_alias,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_audio_ms": self.input_audio_ms,
            "output_audio_ms": self.output_audio_ms,
        }
        if self.model_alias is not None:
            result["model_alias"] = self.model_alias
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderError(AIEvent):
    event_type: ClassVar[str] = "provider.error"
    code: ProviderErrorCode
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        super(ProviderError, self).__post_init__()
        if not self.message or len(self.message) > 500:
            raise ValueError("provider error message must contain at most 500 characters")

    def payload(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}
