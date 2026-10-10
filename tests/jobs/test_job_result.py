"""A background job has exactly one result (M74-R1, UC-MW5b-live).

Live symptom: the job's model called ``notify`` with the report (one inbox item and push),
the job stayed «в процессе», then its final reply «Отчёт готов, уведомление отправлено» became
a second result (card, inbox item, push). Now ``notify`` inside a job only drafts the result;
the job delivers once — one card, one inbox item, one push — at the moment it succeeds.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.core.ports.model_gateway import ModelEvent, ModelRequest
from app.modules.conversations.feed import FeedService
from app.modules.conversations.models import Entry, EntryRole
from app.modules.jobs.composition import JobsStack
from app.modules.jobs.messages import job_prompt, with_notes
from app.modules.jobs.models import JobRow
from app.modules.jobs.tools import JobTools, register_job_tools
from app.modules.notifications.inbox import InboxService
from app.modules.notifications.models import DeliveryRow, InboxItemRow
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
from sqlalchemy import func, select

from tests.fakes import FakeModelGateway, assert_mvp_valid, text_step, tool_step
from tests.jobs.conftest import JobsWorld, create_device, requires_postgres
from tests.runs.conftest import collect
from tests.runs.gateways import answer

pytestmark = requires_postgres

GOAL = "Сделай подробный отчёт по рынку электромобилей и пришли, когда будет готово"
REPORT = "## Рынок электромобилей\n\nПродажи выросли на 25%. Лидеры: BYD и Tesla."


class SnapshotGateway(FakeModelGateway):
    """Records what the user could see right before the job's final model call."""

    def __init__(self, world: JobsWorld) -> None:
        super().__init__([])
        self.world = world
        self.before_final: dict[str, Any] | None = None

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if len(self.requests) == 3:  # chat: 2 calls; job: notify, then the final answer
            async with self.world.factory() as session:
                self.before_final = {
                    "inbox": await session.scalar(select(func.count()).select_from(InboxItemRow)),
                    "status": await session.scalar(select(JobRow.status)),
                    "notes": await session.scalar(select(JobRow.notes)),
                }
        async for event in super().stream(request):
            yield event


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
    return runs, stack


async def _rows(world: JobsWorld, model: Any) -> list[Any]:
    async with world.factory() as session:
        return list((await session.scalars(select(model))).all())


async def test_job_that_calls_notify_delivers_one_result_when_it_succeeds(
    world: JobsWorld,
) -> None:
    gateway = SnapshotGateway(world)
    for step in (
        tool_step("c1", "background.start", {"goal": GOAL}),
        text_step(answer("Запустила, сообщу.")),
        tool_step("n1", "notify", {"text": REPORT, "title": "Отчёт готов"}),
        text_step(answer("Отчёт готов, уведомление отправлено.")),
    ):
        gateway.add_step(step)
    runs, stack = build(world, gateway)
    device = await create_device(world.factory, world.user)
    await stack.notifications.push_tokens.put(
        user_id=world.user, device_id=device, provider="fcm", token="token-1"
    )
    record, _ = await runs.start(NewRun(user_id=world.user, profile="chat", input_text=GOAL))
    await collect(runs, world.user, record.id)
    await runs.wait_finished(record.id)

    worker = world.worker(stack)
    assert await worker.tick_jobs() == 1
    await worker.wait_idle()

    # While the job ran, notify delivered nothing: no inbox item, the job still running.
    assert gateway.before_final == {
        "inbox": 0,
        "status": "running",
        "notes": [{"text": REPORT, "title": "Отчёт готов"}],
    }
    job_request = gateway.requests[2]
    assert "Не вызывай для этого notify" in job_request.messages[-1].content
    assert "Не вызывай notify для результата" in job_request.messages[0].content

    [row] = await _rows(world, JobRow)
    assert row.status == "succeeded" and row.notes is None
    job = await stack.service.get_job(user_id=world.user, job_id=row.id)
    assert_mvp_valid(job.to_wire(), "job")
    result = job.result
    assert result is not None and REPORT in result["display"]["markdown"]
    assert "уведомление отправлено" not in str(result)

    # Exactly one inbox item, created in the same moment the job succeeded.
    [item] = await _rows(world, InboxItemRow)
    assert (item.source_type, item.source_id) == ("job", str(row.id))
    assert item.created_at == row.finished_at and item.response == result
    [delivery] = await _rows(world, DeliveryRow)
    assert (delivery.channel, delivery.inbox_item_id) == ("push", item.id)
    assert await worker.tick_deliveries() == 1
    assert len(world.fcm.sent) == 1

    # Exactly one card in the chat the job came from, naming that inbox item.
    async with world.factory() as session:
        cards = [
            entry
            for entry in (
                await session.scalars(select(Entry).where(Entry.role == EntryRole.ASSISTANT))
            ).all()
            if "card" in entry.content
        ]
    [card] = cards
    assert card.content["card"]["inbox_item_id"] == str(item.id)
    assert REPORT in card.content["text"]

    # The chat history after a reload: the card carries the steps of the job's run.
    page = await FeedService(world.factory).history(user_id=world.user)
    card_item, started, _ = page.items
    assert card_item.card is not None and card_item.card["job_id"] == str(row.id)
    assert [(step["tool"], step["status"]) for step in card_item.steps] == [("notify", "succeeded")]
    assert [step["tool"] for step in started.steps] == ["background.start"]

    # The inbox gives the full report to expand (list and single item).
    inbox: InboxService = stack.notifications.inbox
    [listed], _ = await inbox.list(user_id=world.user)
    wire = listed.to_wire()
    assert_mvp_valid(wire, "job", "InboxItem")
    assert REPORT in wire["text"]
    one = await inbox.get(user_id=world.user, item_id=item.id)
    assert one is not None and one.to_wire() == wire
    assert await inbox.get(user_id=world.stranger, item_id=item.id) is None


async def test_notify_outside_a_job_still_notifies(world: JobsWorld) -> None:
    gateway = FakeModelGateway(
        [tool_step("n1", "notify", {"text": "Курс вырос"}), text_step(answer("Сообщила."))]
    )
    runs, _ = build(world, gateway)
    record, _ = await runs.start(NewRun(user_id=world.user, profile="chat", input_text="x"))
    # Not ``collect``: the core tool name ``notify`` is not namespaced (OI-M74-R1-002).
    await runs.wait_finished(record.id)
    [item] = await _rows(world, InboxItemRow)
    assert (item.source_type, item.title) == ("system", "Курс вырос")


def test_with_notes_folds_notify_texts_into_one_result() -> None:
    notes = [{"text": REPORT, "title": "Отчёт"}]
    ack: dict[str, Any] = {
        "speech": "Отчёт готов, уведомление отправлено.",
        "display": None,
        "followups": [],
    }
    folded = with_notes(ack, notes)
    assert folded["display"] == {"kind": "text", "markdown": f"**Отчёт**\n\n{REPORT}"}
    assert "уведомление" not in folded["speech"]
    assert_mvp_valid(folded, "assistant-response")

    full = {
        "speech": "Отчёт готов.",
        "display": {"kind": "text", "markdown": "Подробности: рынок растёт."},
        "followups": ["Ещё"],
    }
    merged = with_notes(full, [{"text": "Коротко: рост 25%."}])
    assert merged["display"]["markdown"] == "Коротко: рост 25%.\n\nПодробности: рынок растёт."
    assert merged["speech"] == "Отчёт готов." and merged["followups"] == ["Ещё"]

    containing = {**full, "display": {"kind": "text", "markdown": f"Итог.\n\n{REPORT}"}}
    assert with_notes(containing, [{"text": REPORT}]) == containing
    assert with_notes(full, None) == full and with_notes(full, [{"text": " "}]) == full
    link = {**full, "display": {"kind": "link", "url": "https://example.test", "title": "Отчёт"}}
    assert with_notes(link, notes) == link
    short = with_notes(ack, [{"text": "Курс вырос."}])
    assert short == {"speech": "Курс вырос.", "display": None, "followups": []}


def test_job_prompt_says_the_answer_is_delivered_once() -> None:
    prompt = job_prompt(GOAL, None)
    assert GOAL in prompt and "один раз" in prompt and "notify" in prompt


async def test_retry_drops_notes_of_the_failed_attempt(world: JobsWorld) -> None:
    runs = world.run_service(FakeModelGateway([]))
    stack = world.stack(runs)
    job, _ = await stack.service.create_job(
        user_id=world.user, request={"goal": "x"}, origin_type="user", origin_id=str(world.user)
    )
    [claimed] = await stack.store.claim(worker_id="w", limit=1, lease_s=30)
    run_id = uuid.uuid4()
    assert await stack.store.renew(claimed, worker_id="w", lease_s=30, run_id=run_id)
    assert await stack.store.add_note(job.id, run_id, {"text": "черновик"})
    assert not await stack.store.add_note(job.id, uuid.uuid4(), {"text": "чужой run"})
    assert await stack.store.notes(job.id) == [{"text": "черновик"}]
    assert await stack.store.retry(claimed, worker_id="w", delay_s=0, error_code="x")
    assert await stack.store.notes(job.id) == []
