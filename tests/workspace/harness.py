"""Postgres-backed app harness: real auth + workspace routers, fake Google and Redis."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.core.database import Database
from app.core.redis import RedisClient
from app.main import create_app
from app.modules.auth.cache import RedisAuthCache
from app.modules.auth.composition import install_auth
from app.modules.auth.google import GoogleIdentity, InvalidGoogleTokenError
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import TokenManager
from app.modules.workspace.catalog import CatalogPlugin, StaticPluginCatalog
from app.modules.workspace.composition import install_workspace
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.auth.fakes import FakeRedis

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
SECRET = "test-only-auth-signing-secret-at-least-32-bytes"
CLIENT_ID = "google-web-client"
BASE_URL = "http://mia.test"
ORIGIN = BASE_URL

TIMER = CatalogPlugin(
    plugin_id="mia.timer",
    version="1.0.0",
    tools_digest="sha256:timer-tools-v1",
    grants=("state.read", "state.write"),
    manifest={"id": "mia.timer"},
)
LINEAR = CatalogPlugin(
    plugin_id="team.linear",
    version="2.1.0",
    tools_digest="sha256:linear-tools-v2",
    grants=("tools.use",),
    requires_connection=True,
)


def database_url() -> str:
    return cast(str, TEST_DATABASE_URL)


class TokenGoogleVerifier:
    """``google:<sub>[:<email>]`` → a verified identity with that ``sub``."""

    async def verify(self, id_token: str, *, audience: str, nonce: str) -> GoogleIdentity:
        assert audience == CLIENT_ID
        assert len(nonce) >= 32
        prefix, _, rest = id_token.partition(":")
        if prefix != "google" or not rest:
            raise InvalidGoogleTokenError("bad token")
        subject, _, email = rest.partition(":")
        return GoogleIdentity(
            subject=subject,
            email=email or f"{subject}@example.test",
            email_verified=True,
            display_name=subject.title(),
            avatar_url=None,
            locale="ru",
        )


def migrate_to_head() -> None:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url())
    command.upgrade(configuration, "head")


@dataclass
class Harness:
    app: FastAPI
    service: AuthService
    factory: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def client(self) -> AsyncIterator[AsyncClient]:
        """Each client has its own cookie jar, like a separate browser."""
        async with AsyncClient(transport=ASGITransport(self.app), base_url=BASE_URL) as client:
            yield client


@asynccontextmanager
async def build_harness() -> AsyncIterator[Harness]:
    await asyncio.to_thread(migrate_to_head)
    database = Database(database_url())
    async with database.session_factory() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE users CASCADE"))
    settings = Settings(
        app_env="test",
        auth_nonce_rate_limit=1000,
        auth_exchange_rate_limit=1000,
        auth_authenticated_rate_limit=5000,
    )
    cache = RedisAuthCache(FakeRedis())
    service = AuthService(
        database.session_factory,
        cache,
        TokenGoogleVerifier(),
        TokenManager(SECRET),
        google_client_id=CLIENT_ID,
    )
    app = create_app(settings, install_modules=False)
    redis = RedisClient(settings.redis_url)
    install_auth(app, settings, database=database, redis=redis, service=service, cache=cache)
    install_workspace(app, database=database, catalog=StaticPluginCatalog([TIMER, LINEAR]))
    try:
        yield Harness(app=app, service=service, factory=database.session_factory)
    finally:
        await database.close()
        await redis.close()


_counter = 0


def key() -> dict[str, str]:
    """A fresh Idempotency-Key header."""
    global _counter
    _counter += 1
    return {"Idempotency-Key": f"test-idempotency-{_counter:08d}"}


async def android_login(client: AsyncClient, subject: str, email: str = "") -> dict[str, str]:
    nonce = await client.post("/v1/auth/google/nonce", headers=key())
    assert nonce.status_code == 201
    token = f"google:{subject}:{email}" if email else f"google:{subject}"
    login = await client.post(
        "/v1/auth/google",
        headers=key(),
        json={
            "id_token": token,
            "nonce": nonce.json()["nonce"],
            "device": {"platform": "android", "app_version": "1.0"},
        },
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def web_login(client: AsyncClient, subject: str, email: str = "") -> dict[str, Any]:
    nonce = await client.post("/v1/auth/web/nonce", headers=key())
    assert nonce.status_code == 201
    token = f"google:{subject}:{email}" if email else f"google:{subject}"
    login = await client.post(
        "/v1/auth/web/google", headers={**key(), "Origin": ORIGIN}, json={"credential": token}
    )
    assert login.status_code == 200, login.text
    assert "mia_session" in client.cookies
    return dict(login.json())


def web_headers() -> dict[str, str]:
    """Headers a same-origin browser fetch sends with a mutation."""
    return {**key(), "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
