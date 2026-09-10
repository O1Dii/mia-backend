"""Provider-neutral realtime port and output events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.realtime.contracts import ConfigurePayload
from app.modules.realtime.sessions import RealtimeSession


@dataclass(frozen=True, slots=True)
class ProviderControl:
    event_type: str
    payload: Mapping[str, object]
    turn_id: UUID | None = None
    response_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ProviderAudio:
    data: bytes
    response_id: UUID


ProviderOutput = ProviderControl | ProviderAudio


class RealtimeProviderSession(Protocol):
    async def configure(self, config: ConfigurePayload) -> None: ...

    async def send_audio(self, chunk: bytes) -> None: ...

    async def commit_audio(self, *, stream_id: UUID, turn_id: UUID) -> None: ...

    async def cancel_response(self, response_id: UUID, reason: str) -> None: ...

    def events(self) -> AsyncIterator[ProviderOutput]: ...

    async def close(self) -> None: ...


class RealtimeProviderFactory(Protocol):
    def __call__(self, session: RealtimeSession) -> RealtimeProviderSession: ...
