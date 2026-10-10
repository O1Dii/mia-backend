"""Scenario 4: «Напоминай о просроченных задачах по будням в 9» — restart, delivery.

Voice from the phone → the model creates a weekday 09:00 schedule (``schedule.create``) →
the worker process is restarted (a second application on the same database) → at the
occurrence the new worker turns it into a job, runs it with the user's Linear (recorded
tool set) and delivers one result: the inbox, an FCM push to the phone and the linked
Telegram bot. A worker that dies holding a job hands it to the next one; occurrences missed
while no worker ran follow the ``skip`` policy. The live model's choice of the rule, a real
FCM project and a real bot are user checks (UC-M24-1, UC-M41-2).

"Now" on the worker side is moved with :func:`time_travel` instead of waiting for 09:00.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.worker.main import Worker

from tests.e2e_mvp.harness import (
    FakeBotApi,
    Mvp,
    Person,
    call,
    final,
    payloads,
    requires_stack,
    restarted,
    say,
)
from tests.e2e_mvp.s04_support import (
    TELEGRAM_CHAT,
    Moment,
    linear_connected,
    link_telegram,
    parse,
    push_token,
    time_travel,
)

pytestmark = requires_stack

ZONE = "Europe/Minsk"
RULE = "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"
GOAL = "Напоминай о просроченных задачах в Linear"
ASK = "Напоминай о просроченных задачах по будням в 9"
OVERDUE = "Просрочено: ENG-101 «Fix login redirect» (срок вчера)."


def use_linear(request: Any) -> Any:
    [name] = [t.name for t in request.tools if t.name.endswith(".list_issues")]
    return call(name, {"assignee": "me"})


async def schedule_by_voice(mvp: Mvp, phone: Person) -> dict[str, Any]:
    mvp.model.then(
        call("schedule.create", {"rule": RULE, "timezone": ZONE, "goal": GOAL}),
        say("Хорошо, буду напоминать по будням в девять утра."),
    )
    events = await phone.run(ASK, profile="voice")
    [done] = payloads(events, "tool.call.completed")
    assert done["status"] == "succeeded", done
    assert "по будням" in final(events)["speech"]
    assert "schedule.create" in mvp.model.offered(-2)
    [schedule] = (await phone.get("/v1/schedules")).json()["items"]
    return dict(schedule)


def bot_messages() -> list[dict[str, Any]]:
    """What every process's bot sent to the user's chat (a restart builds a new client)."""
    return [m for bot in FakeBotApi.instances for m in bot.sent if m["chat_id"] == TELEGRAM_CHAT]


def weekday_nine(stamp: str) -> None:
    local = parse(stamp).astimezone(ZoneInfo(ZONE))
    assert local.weekday() < 5 and (local.hour, local.minute) == (9, 0), local


async def prepared(mvp: Mvp, tmp_path: Path) -> tuple[Person, dict[str, Any]]:
    phone = await mvp.phone("ann")
    await phone.register_device_tools()
    await push_token(phone, "fcm-token-ann")
    await linear_connected(mvp, phone, tmp_path)
    await link_telegram(mvp, phone)
    for bot in FakeBotApi.instances:
        bot.sent.clear()
    schedule = await schedule_by_voice(mvp, phone)
    return phone, schedule


async def test_weekday_schedule_survives_a_worker_restart_and_is_delivered_once(
    mvp: Mvp, tmp_path: Path
) -> None:
    phone, schedule = await prepared(mvp, tmp_path)
    assert (schedule["rule"], schedule["timezone"], schedule["status"]) == (RULE, ZONE, "active")
    assert schedule["misfire_policy"] == "skip" and schedule["kind"] == "run"
    weekday_nine(schedule["next_run_at"])
    occurrence = parse(schedule["next_run_at"])

    # Worker A runs its loops for a moment, then the process stops (deploy / crash).
    worker_a = mvp.worker("worker-a")
    stop = asyncio.Event()
    loop = asyncio.create_task(worker_a.run(stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(loop, 5)
    assert (await phone.get("/v1/jobs")).json()["items"] == []  # nothing was due yet

    # A new process on the same database; it is now 09:00:30 on the occurrence day.
    async with restarted(mvp) as app_b:
        moment = Moment(occurrence + timedelta(seconds=30))
        time_travel(app_b, moment)
        worker_b = Worker(app_b.state.jobs, worker_id="worker-b")
        assert await worker_b.tick_scheduler() == 1
        assert await worker_b.tick_scheduler() == 0  # the same occurrence never fires twice

        mvp.model.then(
            use_linear, say("Есть просроченная задача.", {"kind": "text", "markdown": OVERDUE})
        )
        assert await worker_b.tick_jobs() == 1
        await worker_b.wait_idle()
        assert mvp.model.pending == 0
        job_request = mvp.model.requests[-2]
        assert GOAL in (job_request.messages[-1].content or "")
        assert "team.linear.list_issues" in {t.name for t in job_request.tools}

        [job] = (await phone.get("/v1/jobs")).json()["items"]
        assert job["status"] == "succeeded", job
        assert job["origin"] == {"type": "schedule", "id": schedule["id"]}
        assert (
            parse(job["inputs"]["scheduled_for"]) == occurrence and job["inputs"]["late"] is False
        )
        assert mvp.remote.calls and mvp.remote.calls[-1][0] == "list_issues"

        delivered = 0
        for _ in range(5):
            delivered += await worker_b.tick_deliveries()
        assert delivered >= 2  # push + telegram

    # Exactly one result: one inbox item, one push to the phone, one Telegram message.
    [item] = (await phone.get("/v1/inbox")).json()["items"]
    assert item["source"] == {"type": "job", "id": job["id"]}
    assert "ENG-101" in item["text"]
    [push] = mvp.fcm.sent
    assert push["token"] == "fcm-token-ann"
    assert push["data"]["type"] == "inbox_item" and push["data"]["inbox_item_id"] == item["id"]
    to_bot = bot_messages()
    assert len(to_bot) == 1 and "ENG-101" in to_bot[0]["text"]

    # The schedule moved to the next weekday 09:00.
    [after] = (await phone.get("/v1/schedules")).json()["items"]
    weekday_nine(after["next_run_at"])
    assert parse(after["next_run_at"]) > occurrence
    assert parse(after["last_run_at"]) == occurrence


async def test_a_job_held_by_a_dead_worker_is_finished_once_by_the_next(
    mvp: Mvp, tmp_path: Path
) -> None:
    phone, schedule = await prepared(mvp, tmp_path)
    occurrence = parse(schedule["next_run_at"])

    async with restarted(mvp) as app_a:
        moment = Moment(occurrence + timedelta(seconds=10))
        time_travel(app_a, moment)
        worker_a = Worker(app_a.state.jobs, worker_id="worker-a")
        assert await worker_a.tick_scheduler() == 1
        # Worker A claims the job and dies before running it (no lease renewal, no run).
        [claimed] = await app_a.state.jobs.store.claim(worker_id="worker-a", limit=1, lease_s=60)
        assert claimed.status == "running"

    async with restarted(mvp) as app_b:
        moment = Moment(occurrence + timedelta(seconds=10))
        time_travel(app_b, moment)
        worker_b = Worker(app_b.state.jobs, worker_id="worker-b")
        assert await worker_b.tick_jobs() == 0  # still leased by A
        moment.advance(seconds=61)  # A's lease ran out
        await worker_b.tick_scheduler()  # reclaims expired leases
        mvp.model.then(
            use_linear, say("Есть просроченная задача.", {"kind": "text", "markdown": OVERDUE})
        )
        assert await worker_b.tick_jobs() == 1
        await worker_b.wait_idle()
        for _ in range(5):
            await worker_b.tick_deliveries()

    [job] = (await phone.get("/v1/jobs")).json()["items"]
    assert job["status"] == "succeeded" and job["attempt"] == 2, job
    assert len((await phone.get("/v1/inbox")).json()["items"]) == 1
    assert len(mvp.fcm.sent) == 1
    assert len(bot_messages()) == 1
    attempts = await mvp.sql(
        "SELECT worker_id, outcome FROM job_attempts WHERE job_id = :job ORDER BY attempt",
        job=job["id"],
    )
    assert [a[0] for a in attempts] == ["worker-a", "worker-b"]


async def test_occurrences_missed_while_no_worker_ran_are_skipped(mvp: Mvp, tmp_path: Path) -> None:
    phone, schedule = await prepared(mvp, tmp_path)
    occurrence = parse(schedule["next_run_at"])

    # The worker was down for a week: five weekday occurrences passed, all beyond the grace.
    async with restarted(mvp) as app_b:
        moment = Moment(occurrence + timedelta(days=7, hours=2))
        time_travel(app_b, moment)
        worker_b = Worker(app_b.state.jobs, worker_id="worker-b")
        assert await worker_b.tick_scheduler() == 0
        assert await worker_b.tick_jobs() == 0
        assert mvp.model.pending == 0

    assert (await phone.get("/v1/jobs")).json()["items"] == []
    assert (await phone.get("/v1/inbox")).json()["items"] == []
    assert mvp.fcm.sent == []
    [after] = (await phone.get("/v1/schedules")).json()["items"]
    weekday_nine(after["next_run_at"])
    assert parse(after["next_run_at"]) > moment.now
    assert after["last_run_at"] is None
    assert moment.now.tzinfo is UTC or moment.now.utcoffset() == timedelta(0)
