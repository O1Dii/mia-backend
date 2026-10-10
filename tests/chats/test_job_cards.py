"""A task from chat A answers in chat A and in the inbox (ADR-0017 §4, M74 DoD)."""

from __future__ import annotations

import uuid
from typing import Any, cast

from app.core.config import Settings
from app.core.ports.model_gateway import ModelGatewayError
from app.modules.conversations.models import Conversation
from app.modules.jobs.composition import JobsStack, build_jobs
from app.modules.notifications.models import DeliveryRow, InboxItemRow
from app.worker.main import Worker
from sqlalchemy import select

from tests.chats.conftest import ChatWorld, _Runtime, check, create_chat
from tests.jobs.conftest import jobs_settings
from tests.runs.conftest import payload_of
from tests.workspace.harness import requires_postgres

pytestmark = requires_postgres


def jobs_stack(world: ChatWorld) -> JobsStack:
    return build_jobs(
        Settings(app_env="test"),
        cast(Any, _Runtime(world.factory)),
        runs=world.runs,
        jobs_settings=jobs_settings(),
    )


async def run_jobs(stack: JobsStack) -> None:
    worker = Worker(stack, worker_id="m74")
    assert await worker.tick_jobs() >= 1
    await worker.wait_idle()


async def inbox(world: ChatWorld, user: uuid.UUID) -> list[InboxItemRow]:
    async with world.factory() as session:
        return list(
            (
                await session.scalars(
                    select(InboxItemRow)
                    .where(InboxItemRow.user_id == user)
                    .order_by(InboxItemRow.created_at)
                )
            ).all()
        )


async def test_job_from_chat_a_answers_in_a_and_in_the_inbox(world: ChatWorld) -> None:
    stack = jobs_stack(world)
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        a = uuid.UUID((await create_chat(client, headers, "A"))["id"])
        b = uuid.UUID((await create_chat(client, headers, "B"))["id"])
        events = await world.ask(user, "Собери сводку новостей в фоне", conversation_id=a)
        origin = payload_of(events, "run.started")
        assert origin["conversation_id"] == str(a)
        run_id = events[0]["run_id"]
        await world.ask(user, "Другая тема", conversation_id=b)

        job, _ = await stack.service.create_job(
            user_id=user,
            request={"goal": "Сводка новостей"},
            origin_type="run",
            origin_id=run_id,
        )
        await run_jobs(stack)
        done = await stack.service.get_job(user_id=user, job_id=job.id)
        assert done.status == "succeeded" and done.run_id is not None
        assert done.result is not None
        speech = done.result["speech"]

        # The job ran in the hidden background context, not in chat A or B.
        job_run = await world.runs.store.get(done.run_id)
        assert job_run is not None and job_run.conversation_id is not None
        assert job_run.conversation_id not in (a, b)
        hidden = await world.conversation(job_run.conversation_id)
        assert hidden is not None and hidden.kind == "background"

        feed_a = (
            await client.get("/v1/feed", params={"conversation_id": str(a)}, headers=headers)
        ).json()
        check("FeedPage", feed_a)
        card_item = feed_a["items"][0]
        assert card_item["card"] == {
            "type": "job_result",
            "job_id": str(job.id),
            "status": "succeeded",
            "title": "Сводка новостей",
        }
        assert card_item["source"] == "run_job" and card_item["role"] == "assistant"
        assert card_item["run_id"] is None and card_item["text"] == speech
        feed_b = (
            await client.get("/v1/feed", params={"conversation_id": str(b)}, headers=headers)
        ).json()
        assert all("card" not in item for item in feed_b["items"])

        [item] = await inbox(world, user)
        assert (item.source_type, item.source_id) == ("job", str(job.id))

        # The card is part of chat A's history for the model.
        await world.ask(user, "Что там в сводке?", conversation_id=a)
        seen = [m.content for m in world.gateway.requests[-1].messages[1:-1]]
        assert speech in seen
        listing = (await client.get("/v1/chats", headers=headers)).json()
        assert [chat["id"] for chat in listing["items"]] == [str(a), str(b)]


async def test_jobs_without_a_chat_reach_only_the_inbox(world: ChatWorld) -> None:
    stack = jobs_stack(world)
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        a = uuid.UUID((await create_chat(client, headers, "A"))["id"])
        await world.ask(user, "Привет", conversation_id=a)
        before = len(await world.entries(a))
        job, _ = await stack.service.create_job(
            user_id=user, request={"goal": "Из API"}, origin_type="user", origin_id=str(user)
        )
        await run_jobs(stack)
        assert (await stack.service.get_job(user_id=user, job_id=job.id)).status == "succeeded"
        assert len(await world.entries(a)) == before
        assert [item.source_id for item in await inbox(world, user)] == [str(job.id)]


async def test_failed_job_card_and_deleted_source_chat(world: ChatWorld) -> None:
    stack = jobs_stack(world)
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        a = uuid.UUID((await create_chat(client, headers, "A"))["id"])
        events = await world.ask(user, "Сделай в фоне", conversation_id=a)
        failing, _ = await stack.service.create_job(
            user_id=user,
            request={"goal": "Сломается"},
            origin_type="run",
            origin_id=events[0]["run_id"],
        )
        original = world.gateway.stream

        async def broken(request: Any) -> Any:
            raise ModelGatewayError("model_not_configured", "no model")
            yield  # pragma: no cover

        world.gateway.stream = broken  # type: ignore[method-assign]
        await run_jobs(stack)
        world.gateway.stream = original  # type: ignore[method-assign]
        done = await stack.service.get_job(user_id=user, job_id=failing.id)
        assert done.status == "failed"
        card = (await world.entries(a))[-1].content
        assert card["card"]["status"] == "failed" and card["card"]["job_id"] == str(failing.id)

        # A job whose source chat was deleted still reaches the inbox.
        orphan, _ = await stack.service.create_job(
            user_id=user,
            request={"goal": "Сирота"},
            origin_type="run",
            origin_id=events[0]["run_id"],
        )
        await client.delete(f"/v1/chats/{a}", headers={**headers, **{"Idempotency-Key": "x" * 20}})
        await run_jobs(stack)
        assert (await stack.service.get_job(user_id=user, job_id=orphan.id)).status == "succeeded"
        assert str(orphan.id) in {item.source_id for item in await inbox(world, user)}
        async with world.factory() as session:
            kinds = set(
                (
                    await session.scalars(
                        select(Conversation.kind).where(Conversation.user_id == user)
                    )
                ).all()
            )
        assert kinds == {"background"}


async def test_job_from_the_telegram_chat_is_also_sent_to_telegram(world: ChatWorld) -> None:
    stack = jobs_stack(world)
    async with world.harness.client() as client:
        _, user = await world.login(client, "alice")
        telegram = await world.chats.telegram_chat_id(user_id=user)
        events = await world.ask(user, "Напомни в фоне", conversation_id=telegram)
        job, _ = await stack.service.create_job(
            user_id=user,
            request={"goal": "Из Telegram", "delivery": {"channels": ["inbox", "push"]}},
            origin_type="run",
            origin_id=events[0]["run_id"],
        )
        await run_jobs(stack)
        async with world.factory() as session:
            channels = set((await session.scalars(select(DeliveryRow.channel))).all())
        assert channels == {"push", "telegram"}
        assert (await world.entries(telegram))[-1].content["card"]["job_id"] == str(job.id)
