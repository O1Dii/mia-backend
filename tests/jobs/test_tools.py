"""Core tools through M13's router inside real runs, and the «while you were away» note."""

from __future__ import annotations

import uuid

from app.core.ports.tool_router import ToolContext
from app.modules.conversations.feed import FeedService
from app.modules.jobs.composition import JobsStack
from app.modules.jobs.tools import JobTools, register_job_tools
from app.modules.notifications.away import AwaySummaryFeed, suppress_away_summary
from app.modules.notifications.models import InboxItemRow
from app.modules.plugins.registry import (
    PluginRegistry,
    StaticInstallationReader,
    StaticManifestSource,
)
from app.modules.runs import NewRun, RunService
from app.modules.tools.approvals import InMemoryApprovalService
from app.modules.tools.audit import InMemoryToolAudit
from app.modules.tools.core_tools import CoreToolRegistry
from app.modules.tools.mcp_client import McpConnector
from app.modules.tools.router import McpToolRouter
from sqlalchemy import select

from tests.fakes import FakeModelGateway, text_step, tool_step
from tests.jobs.conftest import JobsWorld, requires_postgres
from tests.runs.conftest import collect, payload_of
from tests.runs.gateways import answer

pytestmark = requires_postgres


def build(world: JobsWorld, gateway: FakeModelGateway) -> tuple[RunService, JobsStack]:
    core = CoreToolRegistry()
    router = McpToolRouter(
        registry=PluginRegistry(StaticInstallationReader({}), StaticManifestSource([])),
        connector=McpConnector(),
        audit=InMemoryToolAudit(),
        approvals=InMemoryApprovalService(),
        core_tools=core,
    )
    runs = world.run_service(gateway, router=router, with_feed=True)
    stack = world.stack(runs)
    register_job_tools(core, JobTools(stack.service, runs))
    feed = runs.engine.feed
    assert isinstance(feed, FeedService)
    runs.engine.feed = AwaySummaryFeed(feed, stack.notifications.inbox)
    return runs, stack


async def test_voice_starts_a_job_and_hears_about_it_next_time(world: JobsWorld) -> None:
    gateway = FakeModelGateway(
        [
            tool_step("c1", "background.start", {"goal": "Сделай карту по PDF"}),
            text_step(answer("Запустила, сообщу.")),
        ]
    )
    runs, stack = build(world, gateway)
    record, _ = await runs.start(NewRun(user_id=world.user, profile="voice", input_text="карту"))
    events = await collect(runs, world.user, record.id)
    await runs.wait_finished(record.id)
    offered = {spec.name for spec in gateway.requests[0].tools}
    assert {"background.start", "background.status", "schedule.create"} <= offered
    assert "notify" not in offered  # not a voice tool
    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "succeeded"
    job_id = completed["structured_content"]["job"]["id"]
    assert payload_of(events, "job.created") == {"job_id": job_id, "goal": "Сделай карту по PDF"}
    job = await stack.service.get_job(user_id=world.user, job_id=uuid.UUID(job_id))
    assert (job.origin_type, job.origin_id) == ("run", str(record.id))

    # The worker runs the job: a job run sees neither background.start nor the away note.
    gateway.add_step(text_step(answer("Карта по PDF готова.")))
    worker = world.worker(stack)
    await worker.tick_jobs()
    await worker.wait_idle()
    job_request = gateway.requests[2]
    job_tools = {spec.name for spec in job_request.tools}
    assert "background.start" not in job_tools and "notify" in job_tools
    assert "Пока пользователя не было" not in job_request.messages[0].content

    # The next voice run hears «while you were away» once; the one after does not.
    gateway.add_step(text_step(answer("Пока вас не было: карта готова. Слушаю.")))
    gateway.add_step(text_step(answer("Слушаю.")))
    for _ in range(2):
        again, _ = await runs.start(NewRun(user_id=world.user, profile="voice", input_text="Мия"))
        await collect(runs, world.user, again.id)
        await runs.wait_finished(again.id)
    first, second = gateway.requests[3].messages[0].content, gateway.requests[4].messages[0].content
    assert "Пока пользователя не было, во входящих появилось: Сделай карту по PDF" in first
    assert "Пока пользователя не было" not in second


async def test_schedule_and_notify_tools(world: JobsWorld) -> None:
    gateway = FakeModelGateway(
        [
            tool_step(
                "c1",
                "schedule.create",
                {
                    "rule": "RRULE:FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
                    "timezone": "Europe/Moscow",
                    "goal": "Утренняя сводка",
                    "misfire_policy": "limited_catch_up",
                },
            ),
            tool_step("c2", "schedule.list", {}),
            text_step(answer("Готово.")),
        ]
    )
    runs, stack = build(world, gateway)
    record, _ = await runs.start(NewRun(user_id=world.user, profile="chat", input_text="x"))
    events = await collect(runs, world.user, record.id)
    await runs.wait_finished(record.id)
    calls = [e["payload"] for e in events if e["type"] == "tool.call.completed"]
    assert [c["status"] for c in calls] == ["succeeded", "succeeded"]
    [schedule] = await stack.service.list_schedules(user_id=world.user)
    assert schedule.misfire_policy == "limited_catch_up"
    assert calls[1]["structured_content"]["schedules"][0]["id"] == str(schedule.id)

    tools = JobTools(stack.service, runs)
    context = ToolContext(user_id=str(world.user), run_id=str(uuid.uuid4()), profile="job")
    sent = await tools.notify(context, {"text": "Курс доллара вырос", "channels": ["push"]})
    assert sent.status == "succeeded"
    paused = await tools.schedule_pause(context, {"schedule_id": str(schedule.id)})
    assert paused.structured_content is not None
    assert paused.structured_content["schedule"] == {
        **schedule.to_wire(),
        "status": "paused",
        "next_run_at": schedule.to_wire()["next_run_at"],
    }
    foreign = ToolContext(user_id=str(world.stranger), run_id=str(uuid.uuid4()), profile="chat")
    denied = await tools.schedule_run_now(foreign, {"schedule_id": str(schedule.id)})
    assert (denied.status, denied.error_code) == ("failed", "schedule_not_found")
    async with world.factory() as session:
        [item] = (await session.scalars(select(InboxItemRow))).all()
    assert (item.source_type, item.title) == ("system", "Курс доллара вырос")


async def test_suppressed_context_skips_the_note(world: JobsWorld) -> None:
    _, stack = build(world, FakeModelGateway([]))
    await stack.service.notify(user_id=world.user, text="Новое")
    away = AwaySummaryFeed(FeedService(world.factory), stack.notifications.inbox)
    with suppress_away_summary():
        context = await away.context(user_id=world.user)
    assert context.summary is None
    noted = await away.context(user_id=world.user)
    assert noted.summary and "Новое" in noted.summary
    assert (await away.context(user_id=world.user)).summary is None
