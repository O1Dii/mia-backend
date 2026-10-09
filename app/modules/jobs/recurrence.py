"""RRULE + IANA time zone recurrences and the missed-occurrence (misfire) policy.

Occurrences are computed in the schedule's wall-clock time (``08:30 Europe/Moscow``
stays 08:30 across DST changes) and stored in UTC. Pure functions; no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrule, rrulestr

MisfirePolicy = Literal["skip", "run_once", "limited_catch_up"]

RULE_PATTERN = re.compile(
    r"^(DTSTART[^\n]*\n)?RRULE:FREQ=(MINUTELY|HOURLY|DAILY|WEEKLY|MONTHLY|YEARLY)"
    r"(;[A-Z]+=[A-Za-z0-9,:+-]+)*$"
)
"""``job.schema.json#/$defs/Schedule/properties/rule``."""
TIMEZONE_PATTERN = re.compile(r"^(UTC|[A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2})$")
_DTSTART = re.compile(r"^DTSTART(?:;TZID=([A-Za-z0-9_/+-]{1,64}))?:(\d{8}T\d{6})(Z?)$")
MAX_DTSTART_AGE = timedelta(days=366)
INTERVAL_PROBE = 12
MAX_DUE_SCAN = 10_000


class RecurrenceError(ValueError):
    """``code`` is a public error code: invalid_schedule_rule, invalid_timezone,
    schedule_interval_too_short."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Recurrence:
    rule: str
    timezone: str
    dtstart: datetime
    """Aware, in the schedule's zone."""
    _rrule: rrule

    def after(self, moment: datetime) -> datetime | None:
        """First occurrence strictly after ``moment`` (UTC), or ``None`` when exhausted."""
        found = self._rrule.after(moment.astimezone(self.zone()), inc=False)
        return None if found is None else found.astimezone(UTC)

    def due(self, start: datetime, now: datetime) -> list[datetime]:
        """Occurrences ``start <= o <= now`` (UTC, ascending, capped)."""
        zone = self.zone()
        found: list[datetime] = []
        for occurrence in self._rrule.xafter(start.astimezone(zone), inc=True):
            if occurrence > now:
                break
            found.append(occurrence.astimezone(UTC))
            if len(found) >= MAX_DUE_SCAN:
                break
        return found

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_zone(timezone: str) -> ZoneInfo:
    if not TIMEZONE_PATTERN.match(timezone):
        raise RecurrenceError("invalid_timezone", "unknown time zone")
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise RecurrenceError("invalid_timezone", "unknown time zone") from None


def parse_recurrence(
    rule: str,
    timezone: str,
    *,
    now: datetime,
    dtstart: datetime | None = None,
    min_interval_s: int | None = None,
) -> Recurrence:
    """Validate ``rule`` in ``timezone``.

    ``dtstart`` (aware) re-anchors a stored schedule; otherwise the ``DTSTART`` line is used,
    else ``now`` rounded down to the minute. With ``min_interval_s`` the first occurrences
    must be at least that far apart and at least one must lie in the future.
    """
    zone = load_zone(timezone)
    if not RULE_PATTERN.match(rule):
        raise RecurrenceError("invalid_schedule_rule", "rule must be RFC 5545 RRULE")
    lines = rule.split("\n")
    rrule_line = lines[-1]
    anchor: datetime
    if dtstart is not None:
        anchor = dtstart.astimezone(zone)
    elif len(lines) == 2:
        anchor = _parse_dtstart(lines[0], zone)
        if anchor < now - MAX_DTSTART_AGE:
            raise RecurrenceError("invalid_schedule_rule", "DTSTART is too far in the past")
    else:
        anchor = now.astimezone(zone).replace(second=0, microsecond=0)
    try:
        parsed = rrulestr(rrule_line, dtstart=anchor)
    except (ValueError, TypeError, OverflowError):
        raise RecurrenceError("invalid_schedule_rule", "rule could not be parsed") from None
    if not isinstance(parsed, rrule):
        raise RecurrenceError("invalid_schedule_rule", "rule could not be parsed")
    recurrence = Recurrence(rule=rule, timezone=timezone, dtstart=anchor, _rrule=parsed)
    if min_interval_s is not None:
        _check_interval(recurrence, now, min_interval_s)
    return recurrence


def _parse_dtstart(line: str, zone: ZoneInfo) -> datetime:
    match = _DTSTART.match(line)
    if match is None:
        raise RecurrenceError("invalid_schedule_rule", "DTSTART is malformed")
    tzid, stamp, utc = match.groups()
    try:
        naive = datetime.strptime(stamp, "%Y%m%dT%H%M%S")
    except ValueError:
        raise RecurrenceError("invalid_schedule_rule", "DTSTART is malformed") from None
    if utc:
        return naive.replace(tzinfo=UTC).astimezone(zone)
    if tzid:
        return naive.replace(tzinfo=load_zone(tzid)).astimezone(zone)
    return naive.replace(tzinfo=zone)


def _check_interval(recurrence: Recurrence, now: datetime, min_interval_s: int) -> None:
    occurrences: list[datetime] = []
    cursor = now
    try:
        for _ in range(INTERVAL_PROBE):
            following = recurrence.after(cursor)
            if following is None:
                break
            occurrences.append(following)
            cursor = following
    except (ValueError, TypeError, OverflowError):
        raise RecurrenceError("invalid_schedule_rule", "rule could not be evaluated") from None
    if not occurrences:
        raise RecurrenceError("invalid_schedule_rule", "rule has no future occurrences")
    for earlier, later in zip(occurrences, occurrences[1:], strict=False):
        if (later - earlier).total_seconds() < min_interval_s:
            raise RecurrenceError("schedule_interval_too_short", "occurrences are too frequent")


@dataclass(frozen=True, slots=True)
class FirePlan:
    run: tuple[tuple[datetime, bool], ...]
    """``(occurrence, late)`` to enqueue, ascending."""
    skipped: int
    next_run_at: datetime | None


def plan_fire(
    recurrence: Recurrence,
    *,
    next_run_at: datetime,
    now: datetime,
    policy: MisfirePolicy,
    catch_up_limit: int,
    grace_s: float,
) -> FirePlan:
    """Decide which due occurrences run.

    An occurrence within ``grace_s`` of ``now`` is on time and always runs. Older ones were
    missed (worker down): ``skip`` drops them, ``run_once`` runs the latest of them once
    unless an on-time occurrence runs anyway, ``limited_catch_up`` runs up to
    ``catch_up_limit`` of the latest in order.
    """
    due = recurrence.due(next_run_at, now)
    on_time = [o for o in due if (now - o).total_seconds() <= grace_s]
    late = [o for o in due if (now - o).total_seconds() > grace_s]
    chosen_late: list[datetime]
    if policy == "skip":
        chosen_late = []
    elif policy == "run_once":
        chosen_late = late[-1:] if late and not on_time else []
    else:
        chosen_late = late[-catch_up_limit:] if late else []
    run = tuple([(o, True) for o in chosen_late] + [(o, False) for o in on_time])
    return FirePlan(
        run=run,
        skipped=len(late) - len(chosen_late),
        next_run_at=recurrence.after(now),
    )
