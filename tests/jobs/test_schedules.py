"""Schedules on Postgres: survive worker restarts, misfire policies, no double firing,
pause/resume, run_now, and M14 timer/alarm deadlines fired by the scheduler."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.modules.jobs.domain import JobError
from app.modules.jobs.models import JobRow
from app.modules.notifications.models import DeliveryRow, InboxItemRow
from app.modules.workspace.models import Installation, PluginState
from app.modules.workspace.service import ensure_personal_workspace
from sqlalchemy import select

from tests.fakes import assert_mvp_valid
from tests.jobs.conftest import JobsWorld, requires_postgres
from tests.runs.gateways import scripted

pytestmark = requires_postgres

HOURLY_FROM_MIDNIGHT = "DTSTART:{day}T000000Z\nRRULE:FREQ=HOURLY"


def stack_of(world: JobsWorld):  # type: ignore[no-untyped-def]
    return world.stack(world.run_service(scripted()))


def hourly(world: JobsWorld, **extra: Any) -> dict[str, Any]:
    day = world.clock.now.strftime("%Y%m%d")
    return {
        "rule": HOURLY_FROM_MIDNIGHT.format(day=day),
        "timezone": "UTC",
        "goal": "Проверь почту",
        "delivery": {"channels": ["push"]},
        **extra,
    }


async def _jobs(world: JobsWorld) -> list[JobRow]:
    async with world.factory() as session:
        return list(
            (await session.scalars(select(JobRow).order_by(JobRow.created_at, JobRow.goal))).all()
        )


def _at(world: JobsWorld, hour: int, minute: int = 0) -> datetime:
    return world.clock.now.replace(hour=hour, minute=minute, second=0, microsecond=0)


async def test_schedule_contract_and_validation(world: JobsWorld) -> None:
    world.clock.set(datetime(2026, 10, 9, 12, 0, 5, tzinfo=UTC))
    service = stack_of(world).service
    schedule, _ = await service.create_schedule(
        user_id=world.user,
        request={
            "rule": "RRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
            "timezone": "Europe/Moscow",
            "goal": "Утренняя сводка",
            "delivery": {"channels": ["push", "telegram"]},
            "misfire_policy": "limited_catch_up",
            "catch_up_limit": 2,
        },
    )
    wire = schedule.to_wire()
    assert_mvp_valid(wire, "job", "Schedule")
    assert wire["next_run_at"] == "2026-10-10T05:30:00Z"
    for bad, code in (
        ({"timezone": "Nowhere/City"}, "invalid_timezone"),
        ({"rule": "RRULE:FREQ=MINUTELY"}, "schedule_interval_too_short"),
        ({"rule": "RRULE:FREQ=DAILY;BYHOUR=25"}, "invalid_schedule_rule"),
    ):
        with pytest.raises(JobError) as raised:
            await service.create_schedule(user_id=world.user, request={**hourly(world), **bad})
        assert raised.value.code == code


async def test_schedule_survives_worker_restart(world: JobsWorld) -> None:
    """DoD: worker A sees nothing due and stops; worker B (fresh process objects) fires it."""
    world.clock.set(datetime(2026, 10, 9, 9, 30, tzinfo=UTC))
    stack_a = stack_of(world)
    await stack_a.service.create_schedule(user_id=world.user, request=hourly(world))
    worker_a = world.worker(stack_a, "a")
    assert await worker_a.tick_scheduler() == 0
    await stack_a.runs.aclose()
    del stack_a, worker_a

    world.clock.set(_at(world, 10, 0) + timedelta(seconds=20))
    stack_b = stack_of(world)
    worker_b = world.worker(stack_b, "b")
    assert await worker_b.tick_scheduler() == 1
    assert await worker_b.tick_scheduler() == 0
    [job] = await _jobs(world)
    assert (job.origin_type, job.kind, job.status) == ("schedule", "run", "queued")
    assert job.inputs == {"scheduled_for": "2026-10-09T10:00:00Z", "late": False}
    [schedule] = await stack_b.service.list_schedules(user_id=world.user)
    assert schedule.last_run_at == _at(world, 10)
    assert schedule.next_run_at == _at(world, 11)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("skip", []),
        ("run_once", ["2026-10-09T12:00:00Z"]),
        (
            "limited_catch_up",
            ["2026-10-09T10:00:00Z", "2026-10-09T11:00:00Z", "2026-10-09T12:00:00Z"],
        ),
    ],
)
async def test_missed_occurrences_follow_the_policy(
    world: JobsWorld, policy: str, expected: list[str]
) -> None:
    world.clock.set(datetime(2026, 10, 9, 6, 30, tzinfo=UTC))
    stack = stack_of(world)
    await stack.service.create_schedule(
        user_id=world.user, request=hourly(world, misfire_policy=policy, catch_up_limit=3)
    )
    # The worker was down from 06:30 to 12:10: occurrences 07..12 were missed.
    world.clock.set(datetime(2026, 10, 9, 12, 10, tzinfo=UTC))
    await world.worker(stack).tick_scheduler()
    jobs = await _jobs(world)
    assert sorted(job.inputs["scheduled_for"] for job in jobs if job.inputs) == expected
    assert all(job.inputs and job.inputs["late"] for job in jobs)
    [schedule] = await stack.service.list_schedules(user_id=world.user)
    assert schedule.next_run_at == datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


async def test_two_schedulers_never_fire_one_occurrence_twice(world: JobsWorld) -> None:
    world.clock.set(datetime(2026, 10, 9, 9, 30, tzinfo=UTC))
    stack = stack_of(world)
    for index in range(10):
        await stack.service.create_schedule(
            user_id=world.user if index % 2 else world.stranger,
            request=hourly(world, goal=f"g{index}"),
        )
    world.clock.set(datetime(2026, 10, 9, 10, 0, 30, tzinfo=UTC))
    workers = [world.worker(stack_of(world), f"w{i}") for i in range(4)]
    created = await asyncio.gather(*(worker.tick_scheduler() for worker in workers))
    assert sum(created) == 10
    jobs = await _jobs(world)
    assert len(jobs) == len({job.dedupe_key for job in jobs}) == 10


async def test_pause_resume_run_now_delete(world: JobsWorld) -> None:
    world.clock.set(datetime(2026, 10, 9, 9, 30, tzinfo=UTC))
    stack = stack_of(world)
    service = stack.service
    schedule, _ = await service.create_schedule(user_id=world.user, request=hourly(world))
    with pytest.raises(JobError):
        await service.update_schedule(
            user_id=world.stranger, schedule_id=schedule.id, changes={"status": "paused"}
        )
    paused = await service.update_schedule(
        user_id=world.user, schedule_id=schedule.id, changes={"status": "paused"}
    )
    assert paused.status == "paused"
    world.clock.set(datetime(2026, 10, 9, 15, 10, tzinfo=UTC))
    assert await world.worker(stack).tick_scheduler() == 0
    resumed = await service.update_schedule(
        user_id=world.user, schedule_id=schedule.id, changes={"status": "active"}
    )
    # Occurrences missed while paused are not replayed.
    assert resumed.next_run_at == datetime(2026, 10, 9, 16, 0, tzinfo=UTC)
    job = await service.run_schedule_now(user_id=world.user, schedule_id=schedule.id)
    assert job.origin_type == "schedule" and job.origin_id == str(schedule.id)
    rescheduled = await service.update_schedule(
        user_id=world.user,
        schedule_id=schedule.id,
        changes={"rule": "RRULE:FREQ=DAILY;BYHOUR=7;BYMINUTE=0", "timezone": "Europe/Moscow"},
    )
    assert rescheduled.next_run_at == datetime(2026, 10, 10, 4, 0, tzinfo=UTC)
    await service.delete_schedule(user_id=world.user, schedule_id=schedule.id)
    assert await service.list_schedules(user_id=world.user) == []
    with pytest.raises(JobError) as raised:
        await service.run_schedule_now(user_id=world.user, schedule_id=schedule.id)
    assert raised.value.code == "schedule_not_found"


async def test_reminder_schedule_creates_reminder_jobs(world: JobsWorld) -> None:
    world.clock.set(datetime(2026, 10, 9, 9, 30, tzinfo=UTC))
    stack = stack_of(world)
    await stack.service.create_schedule(
        user_id=world.user, request=hourly(world, kind="reminder", goal="Размяться")
    )
    world.clock.set(datetime(2026, 10, 9, 10, 0, 1, tzinfo=UTC))
    worker = world.worker(stack)
    await worker.tick_scheduler()
    await worker.tick_jobs()
    await worker.wait_idle()
    [job] = await _jobs(world)
    assert (job.kind, job.status) == ("reminder", "succeeded")


# -- M14 timers and alarms ---------------------------------------------------------------


async def _plugin_state(
    world: JobsWorld, plugin_id: str, items: list[dict[str, Any]], *, status: str = "active"
) -> uuid.UUID:
    async with world.factory() as session, session.begin():
        workspace_id = await ensure_personal_workspace(session, world.user)
        session.add(
            Installation(
                workspace_id=workspace_id,
                plugin_id=plugin_id,
                version="1.0.0",
                status=status,
                source="catalog",
            )
        )
        session.add(
            PluginState(workspace_id=workspace_id, plugin_id=plugin_id, data={"items": items})
        )
    return workspace_id


def _item(item_id: str, label: str, deadline: datetime, status: str = "running") -> dict[str, Any]:
    ms = int(deadline.timestamp() * 1000)
    return {
        "id": item_id,
        "label": label,
        "status": status,
        "deadline_ms": ms if status != "paused" else None,
        "remaining_ms": 0,
        "duration_ms": 300000,
        "created_ms": ms - 300000,
    }


async def test_timer_and_alarm_deadlines_fire_exactly_once(world: JobsWorld) -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    world.clock.set(now)
    await _plugin_state(
        world,
        "builtin.timer",
        [
            _item("t1", "Паста", now - timedelta(seconds=3)),
            _item("t2", "Чай", now + timedelta(minutes=5)),
            _item("t3", "Пауза", now, status="paused"),
        ],
    )
    await _plugin_state(
        world, "builtin.alarm", [_item("a1", "Подъём", now - timedelta(hours=1), "scheduled")]
    )
    stacks = [stack_of(world) for _ in range(3)]
    fired = await asyncio.gather(
        *(world.worker(s, f"w{i}").tick_deadlines() for i, s in enumerate(stacks))
    )
    assert sum(fired) == 2
    assert await world.worker(stacks[0]).tick_deadlines() == 0
    async with world.factory() as session:
        items = list(
            (await session.scalars(select(InboxItemRow).order_by(InboxItemRow.title))).all()
        )
        deliveries = list((await session.scalars(select(DeliveryRow))).all())
    assert [(i.source_type, i.source_id, i.title) for i in items] == [
        ("plugin", "builtin.alarm:a1", "Будильник: Подъём"),
        ("plugin", "builtin.timer:t1", "Таймер: Паста"),
    ]
    assert items[1].response["speech"] == "Таймер «Паста» сработал."
    assert "опоздало" in items[0].response["speech"]  # an hour late: the worker was down
    for item in items:
        assert_mvp_valid(
            {
                "id": str(item.id),
                "source": {"type": item.source_type, "id": item.source_id},
                "title": item.title,
                "response": item.response,
                "created_at": "2026-10-09T12:00:00Z",
                "read_at": None,
            },
            "job",
            "InboxItem",
        )
    assert {d.channel for d in deliveries} == {"push"} and len(deliveries) == 2

    # The second timer fires later; a resumed timer (new deadline) fires again.
    world.clock.advance(301)
    assert await world.worker(stacks[0]).tick_deadlines() == 1


async def test_inactive_installation_does_not_fire(world: JobsWorld) -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    world.clock.set(now)
    await _plugin_state(
        world, "builtin.timer", [_item("t1", "x", now - timedelta(seconds=1))], status="disabled"
    )
    assert await world.worker(stack_of(world)).tick_deadlines() == 0


async def test_scanner_pages_through_all_states_and_skips_stale_deadlines(
    world: JobsWorld,
) -> None:
    from app.modules.jobs.deadlines import DeadlineScanner

    from tests.jobs.conftest import create_user

    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    world.clock.set(now)
    for index in range(5):
        world.user = await create_user(world.factory, f"p{index}")
        await _plugin_state(
            world,
            "builtin.timer",
            [
                _item(f"old{index}", "давно", now - timedelta(hours=25)),
                _item(f"t{index}", "сейчас", now - timedelta(seconds=1)),
            ],
        )
    scanner = DeadlineScanner(world.factory, clock=world.clock, batch=2)
    fired = await scanner.run_once()
    assert sorted(item.item_id for item in fired) == [f"t{i}" for i in range(5)]
    assert await scanner.run_once() == []
