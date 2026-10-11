"""Presence of the account's devices (M84): «онлайн», «был(а) N мин назад», «офлайн», «нет push».

Two inputs:

- **contact** — the device talked to the API: any authenticated request (phone bearer, the
  app's WebView, a browser tab) and the clients' heartbeat while the app/tab is open. The
  tracker keeps the exact time in memory and persists it at most once per
  :data:`FLUSH_INTERVAL` (``devices.last_seen_at`` / ``web_sessions.last_seen_at``), so a
  restart only loses up to a minute;
- **connection** — an open long-lived connection (Personal Node WSS of M86): online while it
  is open, whatever the last contact.

The API process is a single instance (the device tool channel is in memory too), so
in-process state is the truth for «онлайн»; the database answers after a restart.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

DeviceKind = Literal["phone", "browser", "node"]
PresenceStatus = Literal["online", "recent", "offline", "no_push"]
PresenceVia = Literal["connection", "contact"]
PushState = Literal["registered", "missing", "not_configured", "not_applicable"]

ONLINE_WINDOW = timedelta(seconds=120)
"""Contact within this window = «онлайн» (the clients send a heartbeat every 60 s)."""
RECENT_WINDOW = timedelta(minutes=60)
"""Contact within this window = «был(а) N мин назад»; older = «офлайн»."""
FLUSH_INTERVAL = timedelta(seconds=60)
"""How often one device's contact time is written to the database."""
HEARTBEAT_SECONDS = 60
MAX_TRACKED = 10_000


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Presence:
    status: PresenceStatus
    via: PresenceVia | None
    last_seen_at: datetime | None


def latest(*values: datetime | None) -> datetime | None:
    found = [value for value in values if value is not None]
    return max(found) if found else None


def presence_of(
    *,
    kind: DeviceKind,
    last_seen_at: datetime | None,
    connected: bool,
    push: PushState,
    now: datetime,
) -> Presence:
    """The single presence rule of the screen.

    A phone that cannot be woken by push (no token, or push not configured on the server) and
    is not in contact right now is «нет push»: requests of other clients would fail with
    ``device_offline`` until the app is opened (ADR-0016 §8).
    """
    if connected:
        return Presence("online", "connection", last_seen_at)
    if last_seen_at is not None and now - last_seen_at <= ONLINE_WINDOW:
        return Presence("online", "contact", last_seen_at)
    if kind == "phone" and push != "registered":
        return Presence("no_push", None, last_seen_at)
    if last_seen_at is not None and now - last_seen_at <= RECENT_WINDOW:
        return Presence("recent", None, last_seen_at)
    return Presence("offline", None, last_seen_at)


@dataclass(slots=True)
class _Entry:
    seen_at: datetime | None = None
    flushed_at: datetime | None = None
    connections: int = 0


class PresenceTracker:
    """In-memory contact times and open connections, keyed by ``(kind, device id)``."""

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def now(self) -> datetime:
        return self._clock()

    def _entry(self, kind: DeviceKind, device_id: str) -> _Entry:
        key = (kind, device_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _Entry()
            while len(self._entries) > MAX_TRACKED:
                oldest, value = next(iter(self._entries.items()))
                if value.connections:
                    self._entries.move_to_end(oldest)
                    break
                self._entries.popitem(last=False)
        else:
            self._entries.move_to_end(key)
        return entry

    def touch(self, kind: DeviceKind, device_id: str) -> bool:
        """Record a contact now; ``True`` when it is time to persist it."""
        now = self._clock()
        entry = self._entry(kind, device_id)
        entry.seen_at = now
        if entry.flushed_at is None or now - entry.flushed_at >= FLUSH_INTERVAL:
            entry.flushed_at = now
            return True
        return False

    def last_contact(self, kind: DeviceKind, device_id: str) -> datetime | None:
        entry = self._entries.get((kind, device_id))
        return entry.seen_at if entry is not None else None

    def connected(self, kind: DeviceKind, device_id: str) -> None:
        """A long-lived connection opened (Personal Node WSS, M86)."""
        entry = self._entry(kind, device_id)
        entry.connections += 1
        entry.seen_at = self._clock()

    def disconnected(self, kind: DeviceKind, device_id: str) -> None:
        entry = self._entries.get((kind, device_id))
        if entry is None:
            return
        entry.connections = max(0, entry.connections - 1)
        entry.seen_at = self._clock()

    def is_connected(self, kind: DeviceKind, device_id: str) -> bool:
        entry = self._entries.get((kind, device_id))
        return entry is not None and entry.connections > 0

    def forget(self, kind: DeviceKind, device_id: str) -> None:
        """The device was disconnected by the user: it is not «онлайн» any more."""
        self._entries.pop((kind, device_id), None)
