"""Run-engine fixtures: every engine test runs on the in-memory and the Postgres store."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from app.core.ports.approval_service import ApprovalService
from app.core.ports.memory_provider import MemoryProvider
from app.core.ports.model_gateway import ModelGateway
from app.core.ports.tool_router import ToolRouter
from app.modules.conversations.feed import FeedService
from app.modules.runs import (
    EngineConfig,
    InMemoryRunStore,
    ProfileLimits,
    RunEngine,
    RunLimits,
    RunNotifier,
    RunService,
    RunStore,
    SqlRunStore,
)
from app.modules.users import UserRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.fakes import assert_mvp_valid

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)

TEST_LIMITS = RunLimits(
    voice=ProfileLimits(
        max_steps=3, max_tool_calls=2, timeout_s=5, speech_max_chars=250, ack_delay_ms=None
    ),
    chat=ProfileLimits(max_steps=12, max_tool_calls=24, timeout_s=5, stream_deltas=True),
    job=ProfileLimits(max_steps=40, max_tool_calls=80, timeout_s=5),
    app=ProfileLimits(max_steps=8, max_tool_calls=8, timeout_s=5, stream_deltas=True),
)


@dataclass
class RunWorld:
    """A store plus users to run against; ``feed`` is set on Postgres only."""

    store: RunStore
    users: list[uuid.UUID]
    session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def user(self) -> uuid.UUID:
        return self.users[0]

    def feed(self, **kwargs: Any) -> FeedService | None:
        if self.session_factory is None:
            return None
        return FeedService(self.session_factory, **kwargs)

    def service(
        self,
        gateway: ModelGateway,
        *,
        router: ToolRouter | None = None,
        approvals: ApprovalService | None = None,
        memory: MemoryProvider | None = None,
        limits: RunLimits = TEST_LIMITS,
        with_feed: bool = False,
        feed: FeedService | None = None,
        **config: Any,
    ) -> RunService:
        engine = RunEngine(
            store=self.store,
            notifier=RunNotifier(),
            gateway=gateway,
            config=EngineConfig(limits=limits, **config),
            router=router,
            approvals=approvals,
            memory=memory,
            feed=feed or (self.feed() if with_feed else None),
        )
        return RunService(engine, keepalive_s=0.2)


async def _migrated_factory() -> async_sessionmaker[AsyncSession]:
    url = cast(str, TEST_DATABASE_URL)
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", url)
    await asyncio.to_thread(command.upgrade, configuration, "head")
    engine = create_async_engine(url, pool_pre_ping=True, pool_size=30, max_overflow=10)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_users(factory: async_sessionmaker[AsyncSession], count: int) -> list[uuid.UUID]:
    ids = []
    async with factory() as session, session.begin():
        for index in range(count):
            user = await UserRepository(session).create(
                google_sub=f"run-user-{index}-{uuid.uuid4()}",
                email=f"run-user-{index}@example.test",
                email_verified=True,
                locale="ru-BY",
                timezone="Europe/Minsk",
            )
            ids.append(user.id)
    return ids


@pytest.fixture
async def pg_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if TEST_DATABASE_URL is None:
        pytest.skip("MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    factory = await _migrated_factory()
    yield factory
    await factory.kw["bind"].dispose()


@pytest.fixture(params=["memory", pytest.param("postgres", marks=requires_postgres)])
async def world(request: pytest.FixtureRequest) -> AsyncIterator[RunWorld]:
    if request.param == "memory":
        yield RunWorld(store=InMemoryRunStore(), users=[uuid.uuid4(), uuid.uuid4()])
        return
    factory = await _migrated_factory()
    users = await create_users(factory, 2)
    yield RunWorld(store=SqlRunStore(factory), users=users, session_factory=factory)
    await factory.kw["bind"].dispose()


async def collect(
    service: RunService, user_id: uuid.UUID, run_id: uuid.UUID, *, after: int = 0
) -> list[dict[str, Any]]:
    """All events after ``after`` until the terminal one, each validated against the contract."""
    events: list[dict[str, Any]] = []
    async with asyncio.timeout(10):
        async for event in service.events(user_id=user_id, run_id=run_id, after_sequence=after):
            if event is None:
                continue
            wire = event.to_wire()
            assert_mvp_valid(wire, "run-events")
            events.append(wire)
    return events


def assert_well_formed(events: list[dict[str, Any]], *, start: int = 1) -> None:
    """Gap-free sequence, unique cursors, one run, exactly one terminal event at the end."""
    assert [event["sequence"] for event in events] == list(range(start, start + len(events)))
    assert len({event["cursor"] for event in events}) == len(events)
    assert len({event["run_id"] for event in events}) == 1
    terminal = [e for e in events if e["type"] in {"run.completed", "run.failed", "run.cancelled"}]
    assert len(terminal) == 1 and terminal[0] is events[-1]


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def payload_of(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    matches = [event["payload"] for event in events if event["type"] == event_type]
    assert len(matches) == 1, f"{event_type}: {types(events)}"
    return cast(dict[str, Any], matches[0])
