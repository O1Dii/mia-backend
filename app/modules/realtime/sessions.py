"""Short-lived realtime session creation and ownership claims."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from app.modules.realtime.contracts import AudioFormat


class RealtimeSessionStatus(StrEnum):
    CREATED = "created"
    CONNECTED = "connected"
    CLOSED = "closed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: UUID


class RealtimeAuthenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None: ...


@dataclass(frozen=True, slots=True)
class RealtimeSession:
    id: UUID
    user_id: UUID
    conversation_id: UUID
    locale: str
    input_audio: AudioFormat
    output_audio: AudioFormat
    expires_at: datetime
    status: RealtimeSessionStatus = RealtimeSessionStatus.CREATED


class RealtimeSessionNotFoundError(LookupError):
    pass


class RealtimeSessionConflictError(ValueError):
    pass


class InMemoryRealtimeSessionStore:
    """Credential-free PoC store; production persistence remains a later concern."""

    def __init__(self, *, ttl: timedelta = timedelta(minutes=2)) -> None:
        self._ttl = ttl
        self._sessions: dict[UUID, RealtimeSession] = {}
        self._idempotency: dict[tuple[UUID, str], tuple[str, UUID]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _request_hash(
        conversation_id: UUID,
        locale: str,
        input_audio: AudioFormat,
        output_audio: AudioFormat,
    ) -> str:
        serialized = json.dumps(
            {
                "conversation_id": str(conversation_id),
                "input_audio": input_audio.model_dump(mode="json"),
                "locale": locale,
                "output_audio": output_audio.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(serialized).hexdigest()

    async def create(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        locale: str,
        input_audio: AudioFormat,
        output_audio: AudioFormat,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> tuple[RealtimeSession, bool]:
        current_time = now or datetime.now(UTC)
        request_hash = self._request_hash(conversation_id, locale, input_audio, output_audio)
        key = (user_id, idempotency_key)
        async with self._lock:
            previous = self._idempotency.get(key)
            if previous is not None:
                previous_hash, session_id = previous
                if previous_hash != request_hash:
                    raise RealtimeSessionConflictError(
                        "idempotency key was used for a different realtime request"
                    )
                session = self._sessions.get(session_id)
                if session is not None:
                    return session, True
            session = RealtimeSession(
                id=uuid4(),
                user_id=user_id,
                conversation_id=conversation_id,
                locale=locale,
                input_audio=input_audio,
                output_audio=output_audio,
                expires_at=current_time + self._ttl,
            )
            self._sessions[session.id] = session
            self._idempotency[key] = (request_hash, session.id)
            return session, False

    async def claim(
        self, session_id: UUID, user_id: UUID, *, now: datetime | None = None
    ) -> RealtimeSession:
        current_time = now or datetime.now(UTC)
        async with self._lock:
            session = self._sessions.get(session_id)
            if (
                session is None
                or session.user_id != user_id
                or session.expires_at <= current_time
                or session.status is not RealtimeSessionStatus.CREATED
            ):
                raise RealtimeSessionNotFoundError("realtime session was not found")
            connected = replace(session, status=RealtimeSessionStatus.CONNECTED)
            self._sessions[session_id] = connected
            return connected

    async def finish(self, session_id: UUID, *, interrupted: bool) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            status = (
                RealtimeSessionStatus.INTERRUPTED if interrupted else RealtimeSessionStatus.CLOSED
            )
            self._sessions[session_id] = replace(session, status=status)

    async def get(self, session_id: UUID) -> RealtimeSession | None:
        async with self._lock:
            return self._sessions.get(session_id)
