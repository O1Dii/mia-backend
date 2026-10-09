"""Postgres queue: claim/lease, double claim impossible, lease loss, reclaim, cancel, retry."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from app.modules.jobs.domain import JobError, NewJob
from app.modules.jobs.store import SqlJobStore
from app.modules.tools.models import ToolExecution, ToolExecutionStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.fakes import assert_mvp_valid
from tests.jobs.conftest import TEST_DATABASE_URL, JobsWorld, requires_postgres

pytestmark = requires_postgres

BUDGET = {"max_steps": 10, "max_duration_seconds": 60}


def new_job(user: uuid.UUID, goal: str = "Собери сводку", **extra: Any) -> NewJob:
    return NewJob(
        user_id=user,
        goal=goal,
        origin_type="user",
        origin_id=str(user),
        budget=BUDGET,
        channels=("inbox", "push"),
        **extra,
    )


async def test_create_get_list_and_contract(world: JobsWorld) -> None:
    store = SqlJobStore(world.factory, clock=world.clock)
    job, replayed = await store.create_job(
        new_job(world.user), idempotency_key="k" * 16, request_hash="h"
    )
    again, replayed_again = await store.create_job(
        new_job(world.user), idempotency_key="k" * 16, request_hash="h"
    )
    assert not replayed and replayed_again and again.id == job.id
    assert_mvp_valid(job.to_wire(), "job")
    assert job.status == "queued" and job.attempt == 1
    assert await store.get_job(job.id, user_id=world.stranger) is None
    for index in range(3):
        world.clock.advance(1)
        await store.create_job(new_job(world.user, f"g{index}"))
    first, cursor = await store.list_jobs(user_id=world.user, limit=2)
    second, end = await store.list_jobs(user_id=world.user, limit=2, cursor=cursor)
    assert [j.goal for j in first + second] == ["g2", "g1", "g0", "Собери сводку"]
    assert end is None
    assert (await store.list_jobs(user_id=world.stranger))[0] == []


async def test_active_job_limit(world: JobsWorld) -> None:
    store = SqlJobStore(world.factory, clock=world.clock)
    for _ in range(2):
        await store.create_job(new_job(world.user), max_active=2)
    with pytest.raises(JobError) as raised:
        await store.create_job(new_job(world.user), max_active=2)
    assert raised.value.code == "job_limit_exceeded"
    await store.create_job(new_job(world.stranger), max_active=2)


async def test_concurrent_claims_never_take_one_job_twice(world: JobsWorld) -> None:
    """DoD: 8 workers with their own connection pools race for 60 jobs."""
    seed = SqlJobStore(world.factory, clock=world.clock)
    ids = {(await seed.create_job(new_job(world.user, f"j{i}")))[0].id for i in range(60)}
    engines = [create_async_engine(str(TEST_DATABASE_URL), pool_size=2) for _ in range(8)]
    stores = [
        SqlJobStore(async_sessionmaker(engine, expire_on_commit=False), clock=world.clock)
        for engine in engines
    ]
    claimed: dict[str, list[uuid.UUID]] = {}

    async def drain(index: int, store: SqlJobStore) -> None:
        name = f"w{index}"
        claimed[name] = []
        while True:
            batch = await store.claim(worker_id=name, limit=3, lease_s=30)
            if not batch:
                return
            claimed[name].extend(job.id for job in batch)
            await asyncio.sleep(0)

    try:
        await asyncio.gather(*(drain(i, store) for i, store in enumerate(stores)))
    finally:
        for engine in engines:
            await engine.dispose()
    every = [job_id for jobs in claimed.values() for job_id in jobs]
    assert len(every) == len(set(every)) == 60
    assert set(every) == ids
    assert sum(1 for jobs in claimed.values() if jobs) > 1
    async with world.factory() as session:
        from app.modules.jobs.models import JobAttemptRow, JobRow

        attempts = (await session.scalars(select(JobAttemptRow))).all()
        owners = {row.id: row.lease_owner for row in (await session.scalars(select(JobRow))).all()}
    assert len(attempts) == 60
    for name, jobs in claimed.items():
        assert all(owners[job_id] == name for job_id in jobs)


async def test_lost_lease_cannot_write_and_reclaim_requeues(world: JobsWorld) -> None:
    store = SqlJobStore(world.factory, clock=world.clock, max_attempts=2)
    job, _ = await store.create_job(new_job(world.user))
    [claimed] = await store.claim(worker_id="a", limit=1, lease_s=10)
    world.clock.advance(11)
    [reclaimed] = await store.reclaim_expired()
    assert reclaimed.requeued and reclaimed.job.status == "queued" and reclaimed.job.attempt == 2
    # The old worker comes back: every write is refused.
    assert await store.renew(claimed, worker_id="a", lease_s=10) is None
    assert await store.finish(claimed, worker_id="a", status="succeeded", result={}) is None
    [second] = await store.claim(worker_id="b", limit=1, lease_s=10)
    assert second.id == job.id and second.attempt == 2
    world.clock.advance(11)
    [final] = await store.reclaim_expired()
    assert not final.requeued and final.job.status == "failed"
    assert final.job.error and final.job.error["code"] == "job_interrupted"
    history = await store.attempts(job.id)
    assert [(a.attempt, a.worker_id, a.outcome) for a in history] == [
        (1, "a", "interrupted"),
        (2, "b", "interrupted"),
    ]


async def test_reclaim_never_reruns_a_job_whose_run_wrote(world: JobsWorld) -> None:
    store = SqlJobStore(world.factory, clock=world.clock, max_attempts=3)
    await store.create_job(new_job(world.user))
    [claimed] = await store.claim(worker_id="a", limit=1, lease_s=10)
    run_id = uuid.uuid4()
    await store.renew(claimed, worker_id="a", lease_s=10, run_id=run_id)
    async with world.factory() as session, session.begin():
        session.add(
            ToolExecution(
                user_id=world.user,
                call_id=uuid.uuid4(),
                name="team.linear.create_issue",
                arguments_hash="0" * 64,
                status=ToolExecutionStatus.UNCERTAIN,
                run_id=run_id,
                operation_id=uuid.uuid4(),
                risk="write_external",
            )
        )
    world.clock.advance(11)
    [reclaimed] = await store.reclaim_expired()
    assert not reclaimed.requeued and reclaimed.job.status == "failed"


async def test_cancel_queued_and_running(world: JobsWorld) -> None:
    store = SqlJobStore(world.factory, clock=world.clock)
    queued, _ = await store.create_job(new_job(world.user, "a"))
    world.clock.advance(1)
    running, _ = await store.create_job(new_job(world.user, "b"))
    assert await store.request_cancel(user_id=world.stranger, job_id=queued.id) is None
    cancelled = await store.request_cancel(user_id=world.user, job_id=queued.id)
    assert cancelled is not None and cancelled.status == "cancelled"
    [claimed] = await store.claim(worker_id="w", limit=5, lease_s=10)
    assert claimed.id == running.id  # the cancelled one is never claimed
    flagged = await store.request_cancel(user_id=world.user, job_id=running.id)
    assert flagged is not None and flagged.status == "running" and flagged.cancel_requested
    lease = await store.renew(claimed, worker_id="w", lease_s=10)
    assert lease is not None and lease.cancel_requested


async def test_retry_requeues_with_next_attempt(world: JobsWorld) -> None:
    store = SqlJobStore(world.factory, clock=world.clock)
    job, _ = await store.create_job(new_job(world.user))
    [claimed] = await store.claim(worker_id="w", limit=1, lease_s=10)
    assert await store.retry(claimed, worker_id="w", delay_s=30, error_code="provider_unavailable")
    assert await store.claim(worker_id="w", limit=1, lease_s=10) == []
    world.clock.advance(31)
    [again] = await store.claim(worker_id="w", limit=1, lease_s=10)
    assert again.attempt == 2
    history = await store.attempts(job.id)
    assert [(a.outcome, a.error_code) for a in history] == [
        ("retry", "provider_unavailable"),
        (None, None),
    ]
