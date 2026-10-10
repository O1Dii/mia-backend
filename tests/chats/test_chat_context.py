"""Each chat has its own model context and feed; voice into any chat; default chat; titles."""

from __future__ import annotations

import uuid
from typing import Any

from app.modules.conversations.feed import FeedService
from app.modules.conversations.models import Conversation
from app.modules.conversations.titling import ModelChatTitler, TitlingFeed, clean_title
from app.modules.runs import (
    EngineConfig,
    NewRun,
    RunEngine,
    RunNotifier,
    RunService,
    SqlRunStore,
)
from sqlalchemy import select

from tests.chats.conftest import ChatWorld, check, create_chat
from tests.fakes import text_step
from tests.runs.conftest import TEST_LIMITS, collect, payload_of
from tests.runs.gateways import answer, scripted
from tests.workspace.harness import key, requires_postgres

pytestmark = requires_postgres


def history_of(world: ChatWorld, index: int) -> list[tuple[str, str]]:
    """The dialog the model saw in request ``index`` (without system and the new input)."""
    messages = world.gateway.requests[index].messages
    return [(message.role, message.content) for message in messages[1:-1]]


async def test_two_chats_have_separate_context_and_feed(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        a = uuid.UUID((await create_chat(client, headers, "A"))["id"])
        b = uuid.UUID((await create_chat(client, headers, "B"))["id"])

        await world.ask(user, "Меня интересует Рим", conversation_id=a)
        await world.ask(user, "Что посмотреть в Токио?", conversation_id=b)
        await world.ask(user, "А музеи?", conversation_id=a)

        assert history_of(world, 0) == []
        assert history_of(world, 1) == []  # chat B never sees chat A
        assert history_of(world, 2) == [
            ("user", "Меня интересует Рим"),
            ("assistant", "Ответ 1."),
        ]

        feed_a = await client.get("/v1/feed", params={"conversation_id": str(a)}, headers=headers)
        feed_b = await client.get("/v1/feed", params={"conversation_id": str(b)}, headers=headers)
        check("FeedPage", feed_a.json())
        assert [item["text"] for item in feed_a.json()["items"]] == [
            "Ответ 3.",
            "А музеи?",
            "Ответ 1.",
            "Меня интересует Рим",
        ]
        assert feed_a.json()["conversation_id"] == str(a)
        assert [item["text"] for item in feed_b.json()["items"]] == [
            "Ответ 2.",
            "Что посмотреть в Токио?",
        ]

        # Without conversation_id: the most recently active chat (A got the last message).
        latest = (await client.get("/v1/feed", headers=headers)).json()
        assert latest["conversation_id"] == str(a)

        # A cursor is bound to its chat.
        page = await client.get(
            "/v1/feed", params={"conversation_id": str(a), "limit": 1}, headers=headers
        )
        cursor = page.json()["next_cursor"]
        crossed = await client.get(
            "/v1/feed", params={"conversation_id": str(b), "before": cursor}, headers=headers
        )
        assert crossed.status_code == 400

        listing = (await client.get("/v1/chats", headers=headers)).json()
        check("ChatPage", listing)
        assert [item["id"] for item in listing["items"]] == [str(a), str(b)]
        assert listing["items"][0]["preview"] == "Ответ 3."
        assert listing["items"][0]["preview_role"] == "assistant"


async def test_summary_is_per_chat(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        a = uuid.UUID((await create_chat(client, headers))["id"])
        b = uuid.UUID((await create_chat(client, headers))["id"])
        for index in range(3):
            await world.ask(user, f"Вопрос {index}", conversation_id=a)
        await world.ask(user, "Другое", conversation_id=b)

        feed = FeedService(world.factory, recent_entries=2, summary_batch=2)

        class Summarizer:
            async def summarize(self, *, previous: str | None, messages: Any) -> str:
                return "сводка A: " + "; ".join(m.text for m in messages)

        assert await feed.summarize_if_needed(
            user_id=user, conversation_id=a, summarizer=Summarizer()
        )
        assert not await feed.summarize_if_needed(
            user_id=user, conversation_id=b, summarizer=Summarizer()
        )
        context_a = await feed.context(user_id=user, conversation_id=a)
        context_b = await feed.context(user_id=user, conversation_id=b)
        assert context_a.summary is not None and context_a.summary.startswith("сводка A")
        assert context_b.summary is None
        assert [m.text for m in context_b.messages] == ["Другое", "Ответ 4."]


async def test_voice_runs_target_any_chat(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        target = (await create_chat(client, headers, "Голос"))["id"]
        await create_chat(client, headers, "Новее")
        started = await client.post(
            "/v1/runs",
            json={
                "profile": "voice",
                "input": {"text": "Сколько времени?"},
                "conversation_id": target,
            },
            headers={**headers, **key()},
        )
        assert started.status_code == 202, started.text
        events = await collect(world.runs, user, uuid.UUID(started.json()["id"]))
        assert payload_of(events, "run.started")["conversation_id"] == target
        rows = await world.entries(uuid.UUID(target))
        assert [(row.role.value, row.modality.value) for row in rows] == [
            ("user", "audio"),
            ("assistant", "audio"),
        ]
        foreign = await client.post(
            "/v1/runs",
            json={
                "profile": "voice",
                "input": {"text": "x"},
                "conversation_id": str(uuid.uuid4()),
            },
            headers={**headers, **key()},
        )
        assert foreign.status_code == 404


async def test_runs_without_a_chat_use_the_latest_chat(world: ChatWorld) -> None:
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        events = await world.ask(user, "Первый вопрос\nподробности")
        first = uuid.UUID(payload_of(events, "run.started")["conversation_id"])
        chat = await world.conversation(first)
        assert chat is not None and chat.kind == "chat" and chat.is_primary_feed
        assert (chat.title, chat.title_source) == ("Первый вопрос", "auto")

        newer = uuid.UUID((await create_chat(client, headers))["id"])
        events = await world.ask(user, "Куда это попадёт?")
        assert payload_of(events, "run.started")["conversation_id"] == str(newer)
        assert history_of(world, 1) == []
        latest = (await client.get("/v1/feed", headers=headers)).json()
        assert latest["conversation_id"] == str(newer)

        # Deleting every chat: the next run without a chat opens a new default chat.
        for chat_id in (first, newer):
            await client.delete(f"/v1/chats/{chat_id}", headers={**headers, **key()})
        events = await world.ask(user, "Снова")
        again = uuid.UUID(payload_of(events, "run.started")["conversation_id"])
        assert again not in (first, newer)
        async with world.factory() as session:
            chats = (
                await session.scalars(select(Conversation).where(Conversation.user_id == user))
            ).all()
        assert [row.id for row in chats] == [again]


async def test_model_titles_young_chats_and_respects_user_titles(world: ChatWorld) -> None:
    titles = scripted(text_step("«Поездка в Рим»."), text_step("Не должно быть"))
    titling = TitlingFeed(
        FeedService(world.factory), session_factory=world.factory, titler=ModelChatTitler(titles)
    )
    engine = RunEngine(
        store=SqlRunStore(world.factory),
        notifier=RunNotifier(),
        gateway=scripted(text_step(answer("Да.")), text_step(answer("Ок."))),
        config=EngineConfig(limits=TEST_LIMITS),
        feed=titling,
    )
    runs = RunService(engine, keepalive_s=0.2)
    async with world.harness.client() as client:
        headers, user = await world.login(client, "alice")
        auto = uuid.UUID((await create_chat(client, headers))["id"])
        named = uuid.UUID((await create_chat(client, headers, "Моё название"))["id"])
        for chat_id, text_value in ((auto, "Хочу в Рим летом"), (named, "Привет")):
            record, _ = await runs.start(
                NewRun(user_id=user, profile="chat", input_text=text_value, conversation_id=chat_id)
            )
            await collect(runs, user, record.id)
        await titling.wait_idle()
        chat = await world.conversation(auto)
        assert chat is not None and (chat.title, chat.title_source) == ("Поездка в Рим", "model")
        kept = await world.conversation(named)
        assert kept is not None and (kept.title, kept.title_source) == ("Моё название", "user")
        assert len(titles.requests) == 1
        assert "Хочу в Рим летом" in titles.requests[0].messages[1].content


def test_clean_title() -> None:
    assert clean_title("Название: «План отпуска».\nлишнее") == "План отпуска"
    assert clean_title("   \n\n") is None
    long = clean_title("слово " * 40)
    assert long is not None and len(long) <= 60 and long.endswith("…")
