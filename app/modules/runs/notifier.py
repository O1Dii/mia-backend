"""In-process wake-ups for SSE readers; the store stays the source of truth.

Readers call ``watch`` *before* reading the store and then wait on the returned
event, so a notification between the read and the wait is never lost. Readers
also re-poll on a timeout, which covers events written by another process.
"""

from __future__ import annotations

import asyncio
import uuid
import weakref


class RunNotifier:
    def __init__(self) -> None:
        self._events: weakref.WeakValueDictionary[uuid.UUID, asyncio.Event] = (
            weakref.WeakValueDictionary()
        )

    def watch(self, run_id: uuid.UUID) -> asyncio.Event:
        event = self._events.get(run_id)
        if event is None:
            event = asyncio.Event()
            self._events[run_id] = event
        return event

    def notify(self, run_id: uuid.UUID) -> None:
        event = self._events.pop(run_id, None)
        if event is not None:
            event.set()
