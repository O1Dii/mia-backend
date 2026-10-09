"""Fixtures for the model-gateway tests (PostgreSQL ones skip without MIA_TEST_DATABASE_URL)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.core.crypto import MasterKeyring, generate_master_key
from app.modules.models.adapters import AdapterSettings
from app.modules.models.composition import ModelsStack, build_models_stack
from app.modules.users import UserRepository
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
SessionFactory = async_sessionmaker[AsyncSession]


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def no_sleep_settings() -> AdapterSettings:
    return AdapterSettings(pause_seconds=3600, sleep=_no_sleep)


@pytest.fixture
def keyring() -> MasterKeyring:
    return MasterKeyring.from_config(f"k1:{generate_master_key()}")


@pytest.fixture
async def session_factory() -> AsyncIterator[SessionFactory]:
    if TEST_DATABASE_URL is None:
        pytest.skip("MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    await asyncio.to_thread(command.upgrade, configuration, "head")
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
        await connection.execute(text("TRUNCATE TABLE model_gateway_settings"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def create_user(factory: SessionFactory, subject: str | None = None) -> uuid.UUID:
    subject = subject or f"user-{uuid.uuid4().hex[:12]}"
    async with factory() as session, session.begin():
        user = await UserRepository(session).create(
            google_sub=subject, email=f"{subject}@example.test", email_verified=True
        )
        return user.id


@pytest.fixture
def make_user(session_factory: SessionFactory) -> Callable[[], Awaitable[uuid.UUID]]:
    async def make() -> uuid.UUID:
        return await create_user(session_factory)

    return make


@pytest.fixture
def make_stack(
    session_factory: SessionFactory, keyring: MasterKeyring, no_sleep_settings: AdapterSettings
) -> Callable[[httpx.AsyncBaseTransport], ModelsStack]:
    def make(transport: httpx.AsyncBaseTransport) -> ModelsStack:
        return build_models_stack(
            Settings(app_env="test"),
            session_factory,
            keyring=keyring,
            transport=transport,
            adapter_settings=no_sleep_settings,
        )

    return make
