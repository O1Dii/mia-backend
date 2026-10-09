"""Worker end to end on Postgres: job run → inbox → push (fake FCM), retries, cancel,
budget, worker restart in the middle of a job and of a schedule."""

from __future__ import annotations

import asyncio
import uuid

from app.core.ports.model_gateway import ModelGatewayError
from app.modules.jobs.models import JobAttemptRow
from app.modules.notifications.models import DeliveryRow, InboxItemRow
from app.modules.runs import RunService
from sqlalchemy import select

from tests.fakes import FakeModelGateway, assert_mvp_valid, text_step
from tests.jobs.conftest import JobsWorld, create_device, jobs_settings, requires_postgres
from tests.runs.gateways import BlockingGateway, answer, scripted

pytestmark = requires_postgres


async def _deliveries(world: JobsWorld) -> list[DeliveryRow]:
    async with world.factory() as session:
        return list((await session.scalars(select(DeliveryRow))).all())


async def _inbox(world: JobsWorld) -> list[InboxItemRow]:
    async with world.factory() as session:
        return list(
            (await session.scalars(select(InboxItemRow).order_by(InboxItemRow.created_at))).all()
        )


async def test_job_runs_and_result_reaches_inbox_and_push(world: JobsWorld) -> None:
    gateway = scripted(
        text_step(answer("Карта по PDF готова.", {"kind": "text", "markdown": "# Карта"}))
    )
    runs = world.run_service(gateway)
    stack = world.stack(runs)
    device = await create_device(world.factory, world.user)
    await stack.notifications.push_tokens.put(
        user_id=world.user, device_id=device, provider="fcm", token="test-token-1"
    )
    job, _ = await stack.service.create_job(
        user_id=world.user,
        request={"goal": "Сделай карту по PDF", "inputs": {"url": "https://example.test/a.pdf"}},
        origin_type="user",
        origin_id=str(world.user),
    )
    worker = world.worker(stack)
    assert await worker.tick_jobs() == 1
    await worker.wait_idle()

    done = await stack.service.get_job(user_id=world.user, job_id=job.id)
    assert done.status == "succeeded" and done.run_id is not None
    assert done.result == {
        "speech": "Карта по PDF готова.",
        "display": {"kind": "text", "markdown": "# Карта"},
        "followups": [],
    }
    assert_mvp_valid(done.to_wire(), "job")
    run = await runs.store.get(done.run_id)
    assert run is not None and run.profile == "job"
    prompt = gateway.requests[0].messages[-1].content
    assert "Сделай карту по PDF" in prompt and "example.test/a.pdf" in prompt

    [item] = await _inbox(world)
    assert (item.source_type, item.source_id, item.title) == (
        "job",
        str(job.id),
        "Сделай карту по PDF",
    )
    assert item.response == done.result
    assert await worker.tick_deliveries() == 1
    [delivery] = await _deliveries(world)
    assert (delivery.channel, delivery.status, delivery.attempts) == ("push", "sent", 1)
    [push] = world.fcm.sent
    assert push["token"] == "test-token-1"
    assert push["notification"] == {"title": "Сделай карту по PDF", "body": "Карта по PDF готова."}
    assert push["data"]["type"] == "inbox_item" and push["data"]["inbox_item_id"] == str(item.id)


async def test_retryable_failure_is_retried_then_succeeds(world: JobsWorld) -> None:
    gateway = FakeModelGateway(
        [
            ModelGatewayError("provider_unavailable", "down", retryable=True),
            text_step(answer("Готово.")),
        ]
    )
    stack = world.stack(world.run_service(gateway))
    job, _ = await stack.service.create_job(
        user_id=world.user, request={"goal": "g"}, origin_type="user", origin_id="u"
    )
    worker = world.worker(stack)
    await worker.tick_jobs()
    await worker.wait_idle()
    assert (await stack.service.get_job(user_id=world.user, job_id=job.id)).status == "queued"
    await worker.tick_jobs()
    await worker.wait_idle()
    done = await stack.service.get_job(user_id=world.user, job_id=job.id)
    assert (done.status, done.attempt) == ("succeeded", 2)
    async with world.factory() as session:
        outcomes = [
            (row.attempt, row.outcome, row.error_code)
            for row in (
                await session.scalars(
                    select(JobAttemptRow)
                    .where(JobAttemptRow.job_id == job.id)
                    .order_by(JobAttemptRow.attempt)
                )
            ).all()
        ]
    assert outcomes == [(1, "retry", "provider_unavailable"), (2, "succeeded", None)]


async def test_permanent_failure_notifies_failure(world: JobsWorld) -> None:
    gateway = FakeModelGateway([ModelGatewayError("context_too_long", "too long")])
    stack = world.stack(world.run_service(gateway), push=False)
    job, _ = await stack.service.create_job(
        user_id=world.user, request={"goal": "Большая задача"}, origin_type="user", origin_id="u"
    )
    worker = world.worker(stack)
    await worker.tick_jobs()
    await worker.wait_idle()
    failed = await stack.service.get_job(user_id=world.user, job_id=job.id)
    assert failed.status == "failed" and failed.error and failed.error["code"] == "context_too_long"
    [item] = await _inbox(world)
    assert item.title.startswith("Не выполнено")
    await worker.tick_deliveries()
    [delivery] = await _deliveries(world)
    assert (delivery.status, delivery.last_error) == ("skipped", "push_not_configured")


async def test_cancel_running_job_cancels_its_run(world: JobsWorld) -> None:
    gateway = BlockingGateway(answer("never"))
    runs = world.run_service(gateway)
    stack = world.stack(runs, settings=jobs_settings(jobs_lease_seconds=0.6))
    job, _ = await stack.service.create_job(
        user_id=world.user, request={"goal": "долго"}, origin_type="user", origin_id="u"
    )
    worker = world.worker(stack)
    await worker.tick_jobs()
    await asyncio.wait_for(gateway.entered.wait(), 5)
    await stack.service.cancel_job(user_id=world.user, job_id=job.id)
    await asyncio.wait_for(worker.wait_idle(), 5)
    cancelled = await stack.service.get_job(user_id=world.user, job_id=job.id)
    assert cancelled.status == "cancelled"
    assert cancelled.run_id is not None
    run = await runs.store.get(cancelled.run_id)
    assert run is not None and run.status == "cancelled"
    assert await _inbox(world) == []


async def test_time_budget_cancels_the_run(world: JobsWorld) -> None:
    gateway = BlockingGateway(answer("never"))
    stack = world.stack(world.run_service(gateway), settings=jobs_settings(jobs_lease_seconds=0.3))
    job, _ = await stack.service.create_job(
        user_id=world.user,
        request={"goal": "g", "budget": {"max_steps": 3, "max_duration_seconds": 1}},
        origin_type="user",
        origin_id="u",
    )
    worker = world.worker(stack)
    await worker.tick_jobs()
    await asyncio.wait_for(worker.wait_idle(), 5)
    failed = await stack.service.get_job(user_id=world.user, job_id=job.id)
    assert failed.status == "failed" and failed.error
    assert failed.error["code"] == "job_budget_exceeded"
    assert failed.budget == {"max_steps": 3, "max_duration_seconds": 1}


async def test_reminder_job_needs_no_model(world: JobsWorld) -> None:
    stack = world.stack(world.run_service(scripted()))
    job, _ = await stack.store.create_job(
        __import__("app.modules.jobs.domain", fromlist=["NewJob"]).NewJob(
            user_id=world.user,
            goal="Выпить таблетку",
            origin_type="user",
            origin_id="u",
            budget=stack.service.default_budget("reminder"),
            channels=("inbox", "push"),
            kind="reminder",
        )
    )
    worker = world.worker(stack)
    await worker.tick_jobs()
    await worker.wait_idle()
    done = await stack.service.get_job(user_id=world.user, job_id=job.id)
    assert done.status == "succeeded" and done.run_id is None
    assert_mvp_valid(done.to_wire(), "job")
    [item] = await _inbox(world)
    assert item.response["speech"] == "Выпить таблетку"


async def test_worker_restart_mid_job_hands_it_to_the_next_worker(world: JobsWorld) -> None:
    """Graceful stop of worker A in the middle of a run; worker B finishes the job."""
    blocking = BlockingGateway(answer("never"))
    runs_a = world.run_service(blocking)
    stack_a = world.stack(runs_a)
    job, _ = await stack_a.service.create_job(
        user_id=world.user, request={"goal": "g"}, origin_type="user", origin_id="u"
    )
    worker_a = world.worker(stack_a, "a")
    await worker_a.tick_jobs()
    await asyncio.wait_for(blocking.entered.wait(), 5)
    await worker_a.drain(0)
    await runs_a.aclose()

    runs_b: RunService = world.run_service(scripted(text_step(answer("Сделано вторым."))))
    stack_b = world.stack(runs_b)
    worker_b = world.worker(stack_b, "b")
    await worker_b.tick_scheduler()  # reclaims the expired lease
    assert await worker_b.tick_jobs() == 1
    await worker_b.wait_idle()
    done = await stack_b.service.get_job(user_id=world.user, job_id=job.id)
    assert (done.status, done.attempt) == ("succeeded", 2)
    history = await stack_b.store.attempts(job.id)
    assert [(a.worker_id, a.outcome) for a in history] == [("a", "interrupted"), ("b", "succeeded")]
    first_run = history[0].run_id
    assert first_run is not None
    old = await runs_b.store.get(first_run)
    assert old is not None and old.terminal and old.status != "completed"


async def test_run_loop_processes_everything(world: JobsWorld) -> None:
    """The real loops: a schedule fires, its job runs and the push goes out."""
    stack = world.stack(world.run_service(scripted(text_step(answer("Утренняя сводка.")))))
    device = await create_device(world.factory, world.user)
    await stack.notifications.push_tokens.put(
        user_id=world.user, device_id=device, provider="fcm", token="tok"
    )
    schedule, _ = await stack.service.create_schedule(
        user_id=world.user,
        request={
            "rule": "RRULE:FREQ=DAILY",
            "timezone": "Europe/Moscow",
            "goal": "Сводка",
            "delivery": {"channels": ["push"]},
        },
    )
    assert schedule.next_run_at is not None
    world.clock.set(schedule.next_run_at)
    worker = world.worker(stack)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    for _ in range(200):
        if world.fcm.sent:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert [push["notification"]["body"] for push in world.fcm.sent] == ["Утренняя сводка."]
    [item] = await _inbox(world)
    assert item.source_type == "job"
    assert uuid.UUID(item.source_id)
