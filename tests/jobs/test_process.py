"""The real worker process (``python -m app.worker``): SIGTERM, restart, a missed occurrence."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import timedelta

from app.modules.jobs.models import JobRow
from sqlalchemy import select

from tests.jobs.conftest import TEST_DATABASE_URL, JobsWorld, requires_postgres
from tests.runs.gateways import scripted

pytestmark = requires_postgres


async def _start_worker() -> asyncio.subprocess.Process:
    env = {
        **{
            key: value for key, value in os.environ.items() if not key.startswith(("JOBS_", "FCM_"))
        },
        "APP_ENV": "test",
        "AI_PROVIDER": "fake",
        "DATABASE_URL": str(TEST_DATABASE_URL),
        "JOBS_POLL_SECONDS": "0.1",
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.worker",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


async def _stop(process: asyncio.subprocess.Process) -> None:
    process.send_signal(signal.SIGTERM)
    _, stderr = await asyncio.wait_for(process.communicate(), 20)
    assert process.returncode == 0, stderr.decode(errors="replace")[-2000:]


async def _jobs(world: JobsWorld) -> list[JobRow]:
    async with world.factory() as session:
        return list((await session.scalars(select(JobRow))).all())


async def test_schedule_missed_while_worker_was_down_runs_after_restart(world: JobsWorld) -> None:
    first = await _start_worker()
    await asyncio.sleep(3.0)  # app start ≈ 1.5 s; SIGTERM before the handler kills it (-15)
    await _stop(first)

    # While no worker runs, occurrences of an hourly reminder pass: it was created 2.5 h ago,
    # so its latest occurrence is ~30 min late when the worker comes back.
    world.clock.advance(-9000)
    stack = world.stack(world.run_service(scripted()), push=False)
    schedule, _ = await stack.service.create_schedule(
        user_id=world.user,
        request={
            "rule": "RRULE:FREQ=HOURLY",
            "timezone": "UTC",
            "goal": "Размяться",
            "delivery": {"channels": ["inbox"]},
            "misfire_policy": "run_once",
            "kind": "reminder",
        },
    )
    assert schedule.next_run_at is not None
    world.clock.advance(9000)
    assert schedule.next_run_at < world.clock.now - timedelta(minutes=30)

    second = await _start_worker()
    try:
        for _ in range(150):
            jobs = await _jobs(world)
            if jobs and jobs[0].status == "succeeded":
                break
            await asyncio.sleep(0.1)
    finally:
        await _stop(second)
    [job] = await _jobs(world)
    assert (job.kind, job.status, job.origin_id) == ("reminder", "succeeded", str(schedule.id))
    assert job.inputs and job.inputs["late"] is True
    [after] = await stack.service.list_schedules(user_id=world.user)
    assert after.next_run_at is not None and after.next_run_at > world.clock.now
