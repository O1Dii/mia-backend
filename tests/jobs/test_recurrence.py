"""RRULE + IANA TZ recurrences and the misfire policy (pure, no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.modules.jobs.recurrence import (
    FirePlan,
    MisfirePolicy,
    Recurrence,
    RecurrenceError,
    parse_recurrence,
    plan_fire,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def daily(
    hour: int = 8, minute: int = 30, tz: str = "Europe/Moscow", now: datetime = NOW
) -> Recurrence:
    return parse_recurrence(
        f"RRULE:FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute};BYSECOND=0",
        tz,
        now=now,
        min_interval_s=300,
    )


def test_daily_rule_in_local_time() -> None:
    recurrence = daily()
    # 08:30 Moscow (UTC+3) = 05:30 UTC; 12:00 UTC is past it, so tomorrow.
    assert recurrence.after(NOW) == datetime(2026, 10, 10, 5, 30, tzinfo=UTC)


def test_wall_clock_is_kept_across_dst() -> None:
    start = datetime(2026, 3, 27, 12, 0, tzinfo=UTC)
    recurrence = daily(9, 0, "Europe/Berlin", now=start)
    before = recurrence.after(start)
    after = recurrence.after(datetime(2026, 3, 29, 12, 0, tzinfo=UTC))
    assert before == datetime(2026, 3, 28, 8, 0, tzinfo=UTC)  # CET, UTC+1
    assert after == datetime(2026, 3, 30, 7, 0, tzinfo=UTC)  # CEST, UTC+2


def test_dtstart_with_tzid_and_utc() -> None:
    recurrence = parse_recurrence(
        "DTSTART;TZID=America/New_York:20261001T070000\nRRULE:FREQ=WEEKLY;BYDAY=MO",
        "Europe/Moscow",
        now=NOW,
    )
    # Monday 2026-10-12 07:00 New York (EDT, UTC-4) = 11:00 UTC.
    assert recurrence.after(NOW) == datetime(2026, 10, 12, 11, 0, tzinfo=UTC)
    utc = parse_recurrence("DTSTART:20261009T130000Z\nRRULE:FREQ=HOURLY", "UTC", now=NOW)
    assert utc.after(NOW) == datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("rule", "timezone", "code"),
    [
        ("RRULE:FREQ=DAILY", "Mars/Olympus", "invalid_timezone"),
        ("RRULE:FREQ=DAILY", "../etc", "invalid_timezone"),
        ("FREQ=DAILY", "UTC", "invalid_schedule_rule"),
        ("RRULE:FREQ=DAILY;BYHOUR=99", "UTC", "invalid_schedule_rule"),
        ("RRULE:FREQ=DAILY;COUNT=1\nRRULE:FREQ=DAILY", "UTC", "invalid_schedule_rule"),
        ("DTSTART:20200101T000000Z\nRRULE:FREQ=DAILY", "UTC", "invalid_schedule_rule"),
        ("DTSTART:20261001T000000Z\nRRULE:FREQ=DAILY;COUNT=2", "UTC", "invalid_schedule_rule"),
        ("RRULE:FREQ=MINUTELY", "UTC", "schedule_interval_too_short"),
        ("RRULE:FREQ=HOURLY;BYMINUTE=0,1", "UTC", "schedule_interval_too_short"),
    ],
)
def test_invalid_rules(rule: str, timezone: str, code: str) -> None:
    with pytest.raises(RecurrenceError) as raised:
        parse_recurrence(rule, timezone, now=NOW, min_interval_s=300)
    assert raised.value.code == code


def test_minutely_with_interval_is_allowed() -> None:
    recurrence = parse_recurrence(
        "RRULE:FREQ=MINUTELY;INTERVAL=15", "UTC", now=NOW, min_interval_s=300
    )
    assert recurrence.after(NOW) == NOW + timedelta(minutes=15)


def _hourly() -> Recurrence:
    return parse_recurrence(
        "DTSTART:20261009T000000Z\nRRULE:FREQ=HOURLY", "UTC", now=NOW, min_interval_s=300
    )


def test_on_time_occurrence_runs_with_every_policy() -> None:
    recurrence = _hourly()
    now = datetime(2026, 10, 9, 12, 0, 30, tzinfo=UTC)
    policies: tuple[MisfirePolicy, ...] = ("skip", "run_once", "limited_catch_up")
    for policy in policies:
        plan = plan_fire(
            recurrence,
            next_run_at=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
            now=now,
            policy=policy,
            catch_up_limit=3,
            grace_s=120,
        )
        assert plan.run == ((datetime(2026, 10, 9, 12, 0, tzinfo=UTC), False),)
        assert plan.skipped == 0
        assert plan.next_run_at == datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


def _missed(policy: MisfirePolicy, *, now_minute: int = 10) -> FirePlan:
    """Worker was down from 07:00 to 12:xx: occurrences 07..12 are due."""
    return plan_fire(
        _hourly(),
        next_run_at=datetime(2026, 10, 9, 7, 0, tzinfo=UTC),
        now=datetime(2026, 10, 9, 12, now_minute, tzinfo=UTC),
        policy=policy,
        catch_up_limit=3,
        grace_s=120,
    )


def test_skip_drops_missed_occurrences() -> None:
    plan = _missed("skip")
    assert plan.run == ()
    assert plan.skipped == 6
    assert plan.next_run_at == datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


def test_run_once_runs_the_latest_missed_once() -> None:
    plan = _missed("run_once")
    assert plan.run == ((datetime(2026, 10, 9, 12, 0, tzinfo=UTC), True),)
    assert plan.skipped == 5


def test_run_once_does_not_double_an_on_time_run() -> None:
    plan = _missed("run_once", now_minute=1)
    assert plan.run == ((datetime(2026, 10, 9, 12, 0, tzinfo=UTC), False),)
    assert plan.skipped == 5


def test_limited_catch_up_runs_the_latest_n_in_order() -> None:
    plan = _missed("limited_catch_up")
    assert plan.run == (
        (datetime(2026, 10, 9, 10, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 9, 11, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 9, 12, 0, tzinfo=UTC), True),
    )
    assert plan.skipped == 3
    on_time = _missed("limited_catch_up", now_minute=1)
    assert [late for _, late in on_time.run] == [True, True, True, False]


def test_exhausted_rule_has_no_next_run() -> None:
    recurrence = parse_recurrence(
        "DTSTART:20261009T110000Z\nRRULE:FREQ=HOURLY;COUNT=2", "UTC", now=NOW
    )
    plan = plan_fire(
        recurrence,
        next_run_at=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
        now=NOW,
        policy="skip",
        catch_up_limit=3,
        grace_s=120,
    )
    assert plan.run == ((NOW, False),)
    assert plan.next_run_at is None
