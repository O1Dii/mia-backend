"""The user's continuous feed and its episodic summary (Review §5.2(f)); PostgreSQL only."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import pytest
from alembic import command
from alembic.config import Config
from app.core.ports.model_gateway import ModelGatewayError
from app.modules.conversations import (
    ConversationService,
    ExtractiveFeedSummarizer,
    FeedMessage,
    FeedService,
)
from app.modules.conversations.models import Conversation, Entry, Turn, TurnSource, TurnStatus
from app.modules.conversations.repositories import ConversationNotFoundError
from app.modules.runs import NewRun, VoiceFeedOnlyError
from app.modules.runs.models import RunRow
from app.modules.runs.summarizer import ModelFeedSummarizer
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes import text_step
from tests.runs.conftest import (
    TEST_DATABASE_URL,
    TEST_LIMITS,
    RunWorld,
    collect,
    create_users,
    payload_of,
    requires_postgres,
)
from tests.runs.gateways import answer, scripted

pytestmark = requires_postgres


@pytest.fixture
async def pg_world(pg_factory: async_sessionmaker[AsyncSession]) -> RunWorld:
    from app.modules.runs import SqlRunStore

    users = await create_users(pg_factory, 2)
    return RunWorld(store=SqlRunStore(pg_factory), users=users, session_factory=pg_factory)


async def entries(
    factory: async_sessionmaker[AsyncSession], conversation_id: uuid.UUID
) -> list[Entry]:
    async with factory() as session:
        rows = await session.scalars(
            select(Entry).where(Entry.conversation_id == conversation_id).order_by(Entry.sequence)
        )
        return list(rows)


async def test_runs_write_to_one_primary_feed_and_read_history(pg_world: RunWorld) -> None:
    factory = pg_world.session_factory
    assert factory is not None
    gateway = scripted(
        text_step(answer("Записала.", {"kind": "text", "markdown": "Список: молоко"})),
        text_step(answer("Молоко.")),
    )
    service = pg_world.service(gateway, with_feed=True)

    first, _ = await service.start(
        NewRun(user_id=pg_world.user, profile="voice", input_text="Запомни молоко")
    )
    first_events = await collect(service, pg_world.user, first.id)
    second, _ = await service.start(
        NewRun(user_id=pg_world.user, profile="chat", input_text="Что купить?")
    )
    await collect(service, pg_world.user, second.id)

    feed_id = uuid.UUID(payload_of(first_events, "run.started")["conversation_id"])
    stored = await service.get(user_id=pg_world.user, run_id=second.id)
    assert stored.conversation_id == feed_id and stored.turn_id is not None
    rows = await entries(factory, feed_id)
    assert [(row.role.value, row.content["text"]) for row in rows] == [
        ("user", "Запомни молоко"),
        ("assistant", "Список: молоко"),
        ("user", "Что купить?"),
        ("assistant", "Молоко."),
    ]
    assert rows[0].kind.value == "transcript" and rows[0].modality.value == "audio"
    assert rows[1].content["response"]["speech"] == "Записала."
    history = [(m.role, m.content) for m in gateway.requests[1].messages[1:-1]]
    assert history == [("user", "Запомни молоко"), ("assistant", "Список: молоко")]
    assert "Europe/Minsk" in gateway.requests[1].messages[0].content
    async with factory() as session:
        feeds = (
            await session.scalars(select(Conversation).where(Conversation.user_id == pg_world.user))
        ).all()
        turns = (await session.scalars(select(Turn).where(Turn.conversation_id == feed_id))).all()
    assert len(feeds) == 1 and feeds[0].is_primary_feed
    assert {turn.source for turn in turns} == {TurnSource.RUN_VOICE, TurnSource.RUN_CHAT}
    assert {turn.status for turn in turns} == {TurnStatus.COMPLETED}


async def test_incognito_and_failed_runs(pg_world: RunWorld) -> None:
    factory = pg_world.session_factory
    assert factory is not None
    gateway = scripted(
        text_step(answer("Тихо.")),
        ModelGatewayError("provider_unavailable", "down"),  # type: ignore[arg-type]
    )
    service = pg_world.service(gateway, with_feed=True)

    hidden, _ = await service.start(
        NewRun(user_id=pg_world.user, profile="chat", input_text="секрет", incognito=True)
    )
    hidden_events = await collect(service, pg_world.user, hidden.id)
    failed, _ = await service.start(NewRun(user_id=pg_world.user, profile="chat", input_text="ой"))
    await collect(service, pg_world.user, failed.id)

    assert "conversation_id" not in payload_of(hidden_events, "run.started")
    record = await service.get(user_id=pg_world.user, run_id=hidden.id)
    assert record.input_text is None and record.conversation_id is None
    feed_id = await FeedService(factory).primary_feed_id(user_id=pg_world.user)
    rows = await entries(factory, feed_id)
    assert [row.content["text"] for row in rows] == ["ой"]
    async with factory() as session:
        turn = await session.scalar(select(Turn).where(Turn.conversation_id == feed_id))
    assert turn is not None and turn.status == TurnStatus.FAILED


async def test_conversation_rules(pg_world: RunWorld) -> None:
    factory = pg_world.session_factory
    assert factory is not None
    service = pg_world.service(scripted(text_step(answer("Ок."))), with_feed=True)
    topic = await ConversationService(factory).create_conversation(
        user_id=pg_world.user, idempotency_key="topic-conversation-01", title="Тема"
    )

    with pytest.raises(VoiceFeedOnlyError):
        await service.start(
            NewRun(
                user_id=pg_world.user,
                profile="voice",
                input_text="x",
                conversation_id=topic.value.id,
            )
        )
    with pytest.raises(ConversationNotFoundError):
        await service.start(
            NewRun(
                user_id=pg_world.users[1],
                profile="chat",
                input_text="x",
                conversation_id=topic.value.id,
            )
        )
    record, _ = await service.start(
        NewRun(
            user_id=pg_world.user,
            profile="chat",
            input_text="в тему",
            conversation_id=topic.value.id,
        )
    )
    events = await collect(service, pg_world.user, record.id)
    assert payload_of(events, "run.started")["conversation_id"] == str(topic.value.id)


class RecordingSummarizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, list[str]]] = []

    async def summarize(self, *, previous: str | None, messages: Sequence[FeedMessage]) -> str:
        self.calls.append((previous, [m.text for m in messages]))
        return f"сводка {len(self.calls)}"


async def test_episodic_summary_folds_the_older_part(pg_world: RunWorld) -> None:
    factory = pg_world.session_factory
    assert factory is not None
    replies = [text_step(answer(f"Ответ {index}.")) for index in range(5)]
    gateway = scripted(*replies)
    service = pg_world.service(gateway, with_feed=True)
    for index in range(4):
        record, _ = await service.start(
            NewRun(user_id=pg_world.user, profile="chat", input_text=f"Вопрос {index}")
        )
        await collect(service, pg_world.user, record.id)
    feed = FeedService(factory, recent_entries=2, summary_batch=4)
    feed_id = await feed.primary_feed_id(user_id=pg_world.user)
    summarizer = RecordingSummarizer()

    assert await feed.summarize_if_needed(
        user_id=pg_world.user, conversation_id=feed_id, summarizer=summarizer
    )
    assert summarizer.calls == [
        (None, ["Вопрос 0", "Ответ 0.", "Вопрос 1", "Ответ 1.", "Вопрос 2", "Ответ 2."])
    ]
    assert not await feed.summarize_if_needed(
        user_id=pg_world.user, conversation_id=feed_id, summarizer=summarizer
    )
    context = await feed.context(user_id=pg_world.user)
    assert context.summary == "сводка 1"
    assert [m.text for m in context.messages] == ["Вопрос 3", "Ответ 3."]

    summarized = pg_world.service(gateway, feed=feed)
    record, _ = await summarized.start(
        NewRun(user_id=pg_world.user, profile="chat", input_text="Ещё")
    )
    await collect(summarized, pg_world.user, record.id)
    system = gateway.requests[-1].messages[0].content
    assert "сводка 1" in system
    assert [m.content for m in gateway.requests[-1].messages[1:-1]] == ["Вопрос 3", "Ответ 3."]


async def test_summarizers() -> None:
    messages = [FeedMessage("user", "Купи молоко " * 30, 1), FeedMessage("assistant", "Ок", 2)]
    extractive = await ExtractiveFeedSummarizer(line_chars=40).summarize(
        previous="Раньше", messages=messages
    )
    assert extractive.splitlines()[0] == "Раньше"
    assert extractive.splitlines()[1].endswith("…") and len(extractive.splitlines()[1]) < 60

    model = ModelFeedSummarizer(scripted(text_step("Пользователь хочет молоко.")), uuid.uuid4())
    assert await model.summarize(previous=None, messages=messages) == "Пользователь хочет молоко."
    failing = ModelFeedSummarizer(
        scripted(ModelGatewayError("provider_unavailable", "x")),  # type: ignore[arg-type]
        uuid.uuid4(),
    )
    assert (await failing.summarize(previous=None, messages=messages)).startswith("Пользователь:")


async def test_migration_round_trip(pg_factory: async_sessionmaker[AsyncSession]) -> None:
    assert TEST_DATABASE_URL is not None
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    engine = pg_factory.kw["bind"]

    async def state() -> tuple[set[str], set[str], list[str]]:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
            columns = await connection.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("conversations")}
            )
            labels = (
                await connection.execute(text("SELECT unnest(enum_range(NULL::turn_source))::text"))
            ).scalars()
            return tables, columns, list(labels)

    await asyncio.to_thread(command.downgrade, configuration, "20260913_0003")
    tables, columns, labels = await state()
    assert not {"runs", "run_events"} & tables
    assert "is_primary_feed" not in columns and labels == ["android_text", "android_voice"]

    await asyncio.to_thread(command.upgrade, configuration, "head")
    tables, columns, labels = await state()
    assert {"runs", "run_events"} <= tables and "summary_until_sequence" in columns
    assert {"run_voice", "run_chat", "run_job", "run_app"} <= set(labels)
    async with pg_factory() as session:
        assert (await session.scalars(select(RunRow))).all() == []


async def test_engine_schedules_summary_after_completed_run(pg_world: RunWorld) -> None:
    from app.modules.runs import EngineConfig, RunEngine, RunNotifier, RunService

    factory = pg_world.session_factory
    assert factory is not None
    summarizer = RecordingSummarizer()
    gateway = scripted(*(text_step(answer(f"Ответ {index}.")) for index in range(3)))
    service = RunService(
        RunEngine(
            store=pg_world.store,
            notifier=RunNotifier(),
            gateway=gateway,
            config=EngineConfig(limits=TEST_LIMITS),
            feed=FeedService(factory, recent_entries=2, summary_batch=2),
            summarizer_factory=lambda user_id: summarizer,
        )
    )
    for index in range(2):
        record, _ = await service.start(
            NewRun(user_id=pg_world.user, profile="chat", input_text=f"Вопрос {index}")
        )
        await collect(service, pg_world.user, record.id)
    for _ in range(50):
        if summarizer.calls:
            break
        await asyncio.sleep(0.02)
    assert summarizer.calls == [(None, ["Вопрос 0", "Ответ 0."])]
    await service.aclose()
