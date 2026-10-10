"""System instruction of a run (M73): user's date, search always on, short chat answers."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.ports.types import RunProfile
from app.modules.runs.prompts import system_instruction, user_time_context

NOW = datetime(2026, 10, 10, 22, 30, tzinfo=UTC)


def build(profile: RunProfile = "chat", *, timezone: str = "Europe/Minsk") -> str:
    return system_instruction(
        profile=profile,
        structured=True,
        speech_limit=250,
        now=NOW,
        locale="ru-BY",
        timezone=timezone,
        summary=None,
    )


def test_date_is_in_the_users_time_zone() -> None:
    # 22:30 UTC is already Sunday 11 October in Minsk (UTC+3).
    text = build()
    assert "Сегодня 2026-10-11, воскресенье, сейчас 01:30" in text
    assert "Europe/Minsk, UTC+03:00" in text
    assert "ru-BY" in text


def test_unknown_time_zone_falls_back_to_utc() -> None:
    text = user_time_context(now=NOW, locale="en", timezone="Mars/Olympus")
    assert "Сегодня 2026-10-10, суббота, сейчас 22:30" in text
    assert "UTC, UTC+00:00" in text


def test_search_is_always_on_and_background_is_for_long_work() -> None:
    profiles: tuple[RunProfile, ...] = ("chat", "voice")
    for profile in profiles:
        text = build(profile)
        assert "builtin.internet.news" in text
        assert "background.start" in text and "словами пользователя" in text
    job = build("job")
    assert "builtin.internet.search" in job and "background.start" not in job
    app = build("app")
    assert "builtin.internet" not in app and "background.start" not in app
