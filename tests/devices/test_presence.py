"""Presence rules, the in-memory tracker and the phone's status report (M84, no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.modules.devices.presence import (
    FLUSH_INTERVAL,
    ONLINE_WINDOW,
    RECENT_WINDOW,
    PresenceTracker,
    presence_of,
)
from app.modules.devices.report import DeviceStatusReport, clean_text
from app.modules.devices.service import clean_name
from pydantic import ValidationError

NOW = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.parametrize(
    ("kind", "ago", "push", "connected", "status", "via"),
    [
        ("phone", timedelta(seconds=30), "registered", False, "online", "contact"),
        ("phone", ONLINE_WINDOW, "missing", False, "online", "contact"),
        ("phone", timedelta(minutes=5), "registered", False, "recent", None),
        ("phone", timedelta(minutes=5), "missing", False, "no_push", None),
        ("phone", timedelta(minutes=5), "not_configured", False, "no_push", None),
        ("phone", RECENT_WINDOW + timedelta(seconds=1), "registered", False, "offline", None),
        ("phone", None, "registered", False, "offline", None),
        ("browser", timedelta(minutes=10), "not_applicable", False, "recent", None),
        ("browser", timedelta(days=3), "not_applicable", False, "offline", None),
        ("node", timedelta(days=3), "not_applicable", True, "online", "connection"),
        ("node", timedelta(minutes=3), "not_applicable", False, "recent", None),
    ],
)
def test_presence_rule(
    kind: str, ago: timedelta | None, push: str, connected: bool, status: str, via: str | None
) -> None:
    seen = NOW - ago if ago is not None else None
    found = presence_of(
        kind=kind,  # type: ignore[arg-type]
        last_seen_at=seen,
        connected=connected,
        push=push,  # type: ignore[arg-type]
        now=NOW,
    )
    assert (found.status, found.via, found.last_seen_at) == (status, via, seen)


def test_tracker_persists_once_a_minute_and_forgets_on_disconnect() -> None:
    clock = Clock()
    tracker = PresenceTracker(clock)
    assert tracker.touch("phone", "a") is True
    clock.now += timedelta(seconds=20)
    assert tracker.touch("phone", "a") is False
    assert tracker.last_contact("phone", "a") == clock.now
    clock.now += FLUSH_INTERVAL
    assert tracker.touch("phone", "a") is True
    assert tracker.last_contact("browser", "a") is None
    tracker.forget("phone", "a")
    assert tracker.last_contact("phone", "a") is None


def test_tracker_connections_for_personal_nodes() -> None:
    clock = Clock()
    tracker = PresenceTracker(clock)
    tracker.connected("node", "n1")
    tracker.connected("node", "n1")
    tracker.disconnected("node", "n1")
    assert tracker.is_connected("node", "n1")
    tracker.disconnected("node", "n1")
    tracker.disconnected("node", "n1")
    assert not tracker.is_connected("node", "n1")
    assert tracker.last_contact("node", "n1") == NOW


def test_report_keeps_states_and_drops_phone_numbers() -> None:
    report = DeviceStatusReport.model_validate(
        {
            "os": "Android 15\n",
            "model": "Phone of +375 29 123-45-67",
            "app_version": "0.9.0",
            "notifications": "granted",
            "capabilities": [
                {"capability": "contacts.read", "os_permission": "denied"},
                {"capability": "contacts.read", "os_permission": "granted"},
                {"capability": "alarm.clock", "os_permission": "not_required"},
            ],
        }
    )
    stored = report.stored()
    assert stored["os"] == "Android 15"
    assert "123" not in stored["model"] and "[номер скрыт]" in stored["model"]
    assert stored["capabilities"] == {"contacts.read": "granted", "alarm.clock": "not_required"}
    with pytest.raises(ValidationError):
        DeviceStatusReport.model_validate({"contacts": ["Яна +375291234567"]})
    with pytest.raises(ValidationError):
        DeviceStatusReport.model_validate(
            {"capabilities": [{"capability": "Contacts", "os_permission": "granted"}]}
        )


def test_names_are_one_line_and_bounded() -> None:
    assert clean_name("  Мой\tGalaxy \n") == "Мой Galaxy"
    assert clean_name("   ") is None
    assert clean_name(None) is None
    assert clean_text("x" * 100, 64) == "x" * 64
