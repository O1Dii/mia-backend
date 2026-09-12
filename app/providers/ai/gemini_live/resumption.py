"""Ephemeral session-resumption handle storage."""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID


class GeminiLiveResumeStore(Protocol):
    """Port for an active-session-only resumption handle store."""

    async def load(self, session_id: UUID) -> str | None: ...

    async def save(self, session_id: UUID, handle: str) -> None: ...

    async def delete(self, session_id: UUID) -> None: ...


class InMemoryGeminiLiveResumeStore:
    """Credential-free development store; S18 may bind this port to Redis."""

    def __init__(self) -> None:
        self._handles: dict[UUID, str] = {}
        self._lock = asyncio.Lock()

    async def load(self, session_id: UUID) -> str | None:
        async with self._lock:
            return self._handles.get(session_id)

    async def save(self, session_id: UUID, handle: str) -> None:
        if not handle:
            raise ValueError("resumption handle cannot be empty")
        async with self._lock:
            self._handles[session_id] = handle

    async def delete(self, session_id: UUID) -> None:
        async with self._lock:
            self._handles.pop(session_id, None)
