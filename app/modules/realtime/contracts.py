"""Strict runtime models for existing realtime v1 client controls."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AudioFormat(StrictModel):
    encoding: Literal["pcm_s16le"]
    sample_rate_hz: Literal[16000, 24000]
    channels: Literal[1]


INPUT_AUDIO_FORMAT = AudioFormat(encoding="pcm_s16le", sample_rate_hz=16000, channels=1)
OUTPUT_AUDIO_FORMAT = AudioFormat(encoding="pcm_s16le", sample_rate_hz=24000, channels=1)


class ClientEventBase(StrictModel):
    v: Literal[1]
    type: str
    session_id: UUID
    turn_id: UUID | None = None
    response_id: UUID | None = None
    seq: int = Field(ge=1)
    at: datetime

    @field_validator("at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp requires a timezone")
        return value


class ConfigurePayload(StrictModel):
    protocol_version: Literal[1]
    conversation_id: UUID
    locale: str = Field(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    input_audio: tuple[AudioFormat, ...] = Field(min_length=1)
    output_audio: tuple[AudioFormat, ...] = Field(min_length=1)
    capabilities: tuple[Literal["barge_in", "input_transcript", "output_transcript"], ...] = ()

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("capabilities must be unique")
        return value


class SessionConfigure(ClientEventBase):
    type: Literal["session.configure"]
    payload: ConfigurePayload


class InputAudioStartPayload(StrictModel):
    stream_id: UUID
    format: AudioFormat


class InputAudioStart(ClientEventBase):
    type: Literal["input_audio.start"]
    payload: InputAudioStartPayload


class InputAudioCommitPayload(StrictModel):
    stream_id: UUID
    audio_bytes: int = Field(ge=0)
    duration_ms: int = Field(ge=0)


class InputAudioCommit(ClientEventBase):
    type: Literal["input_audio.commit"]
    payload: InputAudioCommitPayload


class ResponseCancelPayload(StrictModel):
    reason: Literal["barge_in", "user_request", "client_shutdown"]


class ResponseCancel(ClientEventBase):
    type: Literal["response.cancel"]
    response_id: UUID
    payload: ResponseCancelPayload


class SessionClosePayload(StrictModel):
    reason: Literal["completed", "user_request", "client_shutdown", "protocol_error"]


class SessionClose(ClientEventBase):
    type: Literal["session.close"]
    payload: SessionClosePayload


class PingPayload(StrictModel):
    nonce: str = Field(min_length=1, max_length=64)


class Ping(ClientEventBase):
    type: Literal["ping"]
    payload: PingPayload


ClientEvent = Annotated[
    SessionConfigure | InputAudioStart | InputAudioCommit | ResponseCancel | SessionClose | Ping,
    Field(discriminator="type"),
]
CLIENT_EVENT_ADAPTER: TypeAdapter[ClientEvent] = TypeAdapter(ClientEvent)


def parse_client_event(raw: str) -> ClientEvent:
    return CLIENT_EVENT_ADAPTER.validate_json(raw)
