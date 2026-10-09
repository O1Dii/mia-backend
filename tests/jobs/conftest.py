"""M16 fixtures: a migrated Postgres, a controllable clock, jobs stacks and a fake FCM."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.core.ports.model_gateway import ModelGateway
from app.core.ports.tool_router import ToolRouter
from app.modules.auth.models import Device
from app.modules.conversations.feed import FeedService
from app.modules.jobs.composition import JobsStack, build_jobs
from app.modules.jobs.settings import JobsSettings
from app.modules.notifications.fcm import FcmClient, StaticTokenProvider
from app.modules.runs import EngineConfig, RunEngine, RunNotifier, RunService, SqlRunStore
from app.modules.users import UserRepository
from app.worker.main import Worker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.runs.conftest import TEST_LIMITS

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
FCM_PROJECT = "mia-test"
FCM_ENDPOINT = "https://fcm.test"


class Clock:
    """Wall clock that tests can move forward; starts at the real now (run rows use it too)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        self.now = moment


@dataclass
class FakeFcm:
    """In-memory FCM HTTP v1 endpoint (``httpx.MockTransport``)."""

    responses: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    """token → (status, body) overrides; default 200."""
    sent: list[dict[str, Any]] = field(default_factory=list)
    auth: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/projects/{FCM_PROJECT}/messages:send"
        body = json.loads(request.content)
        self.auth.append(request.headers["authorization"])
        token = body["message"]["token"]
        status, payload = self.responses.get(token, (200, {}))
        if status == 200:
            self.sent.append(body["message"])
            return httpx.Response(
                200, json={"name": f"projects/{FCM_PROJECT}/messages/{uuid.uuid4()}"}
            )
        return httpx.Response(status, json=payload)

    def client(self) -> FcmClient:
        return FcmClient(
            project_id=FCM_PROJECT,
            token_provider=StaticTokenProvider("test-access-token"),
            endpoint=FCM_ENDPOINT,
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
        )


async def migrated_factory() -> async_sessionmaker[AsyncSession]:
    url = cast(str, TEST_DATABASE_URL)
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", url)
    await asyncio.to_thread(command.upgrade, configuration, "head")
    engine = create_async_engine(url, pool_pre_ping=True, pool_size=30, max_overflow=20)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
        await connection.execute(text("TRUNCATE TABLE deadline_fires"))
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_user(factory: async_sessionmaker[AsyncSession], name: str = "u") -> uuid.UUID:
    async with factory() as session, session.begin():
        user = await UserRepository(session).create(
            google_sub=f"jobs-{name}-{uuid.uuid4()}",
            email=f"{name}@example.test",
            email_verified=True,
            locale="ru",
            timezone="Europe/Moscow",
        )
        return user.id


async def create_device(factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> uuid.UUID:
    async with factory() as session, session.begin():
        device = Device(id=uuid.uuid4(), user_id=user_id, platform="android", app_version="1.0")
        session.add(device)
    return device.id


class _Database:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = factory


class _Runtime:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.database = _Database(factory)


def jobs_settings(**overrides: Any) -> JobsSettings:
    values: dict[str, Any] = {
        "jobs_lease_seconds": 6,
        "jobs_retry_base_seconds": 0,
        "jobs_poll_seconds": 0.05,
        "jobs_misfire_grace_seconds": 120,
        "jobs_min_schedule_interval_seconds": 300,
    }
    values.update(overrides)
    return JobsSettings().model_copy(update=values)  # tests may go below production minimums


@dataclass
class JobsWorld:
    factory: async_sessionmaker[AsyncSession]
    clock: Clock
    fcm: FakeFcm
    user: uuid.UUID
    stranger: uuid.UUID

    def run_service(
        self,
        gateway: ModelGateway,
        *,
        router: ToolRouter | None = None,
        with_feed: bool = False,
    ) -> RunService:
        engine = RunEngine(
            store=SqlRunStore(self.factory),
            notifier=RunNotifier(),
            gateway=gateway,
            config=EngineConfig(limits=TEST_LIMITS),
            router=router,
            feed=FeedService(self.factory) if with_feed else None,
        )
        return RunService(engine, keepalive_s=0.2)

    def stack(
        self, runs: RunService, *, settings: JobsSettings | None = None, push: bool = True
    ) -> JobsStack:
        return build_jobs(
            Settings(app_env="test"),
            cast(Any, _Runtime(self.factory)),
            runs=runs,
            jobs_settings=settings or jobs_settings(),
            fcm_client=self.fcm.client() if push else None,
            clock=self.clock,
        )

    def worker(self, stack: JobsStack, name: str = "w1") -> Worker:
        return Worker(stack, worker_id=name)


@pytest.fixture
async def world() -> AsyncIterator[JobsWorld]:
    if TEST_DATABASE_URL is None:
        pytest.skip("MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    factory = await migrated_factory()
    user = await create_user(factory, "owner")
    stranger = await create_user(factory, "stranger")
    yield JobsWorld(factory=factory, clock=Clock(), fcm=FakeFcm(), user=user, stranger=stranger)
    await factory.kw["bind"].dispose()
