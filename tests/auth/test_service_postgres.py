from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from app.core.config import Settings
from app.core.database import Database
from app.core.redis import RedisClient
from app.main import create_app
from app.modules.auth.cache import RedisAuthCache
from app.modules.auth.composition import install_auth
from app.modules.auth.google import GoogleIdentity
from app.modules.auth.models import AuthSession
from app.modules.auth.schemas import DeviceInput
from app.modules.auth.service import (
    AuthService,
    InvalidCredentialsError,
    RefreshTokenExpiredError,
    RefreshTokenReuseError,
)
from app.modules.auth.tokens import TokenManager
from app.modules.users.models import User
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.auth.fakes import FakeRedis

TEST_DATABASE_URL = os.environ.get("MIA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="MIA_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
SECRET = "test-only-auth-signing-secret-at-least-32-bytes"


class StaticGoogleVerifier:
    async def verify(self, id_token: str, *, audience: str, nonce: str) -> GoogleIdentity:
        assert id_token == "verified-by-google"
        assert audience == "google-web-client"
        assert len(nonce) >= 32
        return GoogleIdentity(
            subject="stable-google-sub",
            email="person@example.test",
            email_verified=True,
            display_name="Person",
            avatar_url="https://example.test/avatar.png",
            locale="ru",
        )


def database_url() -> str:
    return cast(str, TEST_DATABASE_URL)


@pytest.fixture
async def auth_harness() -> AsyncIterator[tuple[AuthService, async_sessionmaker[AsyncSession]]]:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url())
    await asyncio.to_thread(command.upgrade, configuration, "head")
    engine = create_async_engine(database_url(), pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE users CASCADE"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = AuthService(
        factory,
        RedisAuthCache(FakeRedis()),
        StaticGoogleVerifier(),
        TokenManager(SECRET),
        google_client_id="google-web-client",
    )
    yield service, factory
    await engine.dispose()


def device() -> DeviceInput:
    return DeviceInput(
        platform="android",
        app_version="1.0.0",
        device_name="Test device",
        capabilities={"voice": True},
    )


async def sign_in(service: AuthService) -> tuple[str, str]:
    nonce = await service.create_nonce()
    session = await service.exchange_google_token(
        id_token="verified-by-google", nonce=nonce.nonce, device=device()
    )
    return session.access_token, session.refresh_token


@pytest.mark.asyncio
async def test_google_exchange_upserts_by_sub_and_nonce_cannot_replay(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    service, factory = auth_harness
    nonce = await service.create_nonce()
    first = await service.exchange_google_token(
        id_token="verified-by-google", nonce=nonce.nonce, device=device()
    )

    with pytest.raises(InvalidCredentialsError, match="nonce"):
        await service.exchange_google_token(
            id_token="verified-by-google", nonce=nonce.nonce, device=device()
        )
    principal = await service.authenticate_access(first.access_token)
    assert principal.user.google_sub == "stable-google-sub"
    second_access, _ = await sign_in(service)
    assert (await service.authenticate_access(second_access)).user.id == first.user.id

    async with factory() as session:
        stored_hashes = tuple(await session.scalars(select(AuthSession.refresh_token_hash)))
    assert stored_hashes
    assert all(first.refresh_token not in stored_hash for stored_hash in stored_hashes)


@pytest.mark.asyncio
async def test_rotation_reuse_revokes_the_entire_device_family(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    service, _ = auth_harness
    _, first_refresh = await sign_in(service)
    rotated = await service.refresh(first_refresh)

    assert rotated.refresh_token != first_refresh
    assert (await service.authenticate_access(rotated.access_token)).user.email == (
        "person@example.test"
    )
    with pytest.raises(RefreshTokenReuseError):
        await service.refresh(first_refresh)
    with pytest.raises(InvalidCredentialsError, match="inactive"):
        await service.authenticate_access(rotated.access_token)


@pytest.mark.asyncio
async def test_logout_revokes_only_the_presented_login_family(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    service, _ = auth_harness
    first_access, first_refresh = await sign_in(service)
    second_access, _ = await sign_in(service)
    first_account = await service.authenticate_access(first_access)

    await service.logout(account=first_account, refresh_token=first_refresh)

    with pytest.raises(InvalidCredentialsError, match="inactive"):
        await service.authenticate_access(first_access)
    assert (await service.authenticate_access(second_access)).user.email == "person@example.test"


@pytest.mark.asyncio
async def test_concurrent_rotation_has_one_winner_and_replay_revokes_successor(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    service, factory = auth_harness
    _, refresh = await sign_in(service)
    outcomes = await asyncio.gather(
        service.refresh(refresh), service.refresh(refresh), return_exceptions=True
    )
    assert sum(isinstance(result, RefreshTokenReuseError) for result in outcomes) == 1
    async with factory() as session:
        sessions = list(await session.scalars(select(AuthSession)))
        assert len(sessions) == 2
        assert all(item.revoked_at is not None for item in sessions)


@pytest.mark.asyncio
async def test_old_replay_racing_new_rotation_cannot_leave_active_generation(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    service, factory = auth_harness
    _, old_refresh = await sign_in(service)
    current = await service.refresh(old_refresh)
    await asyncio.gather(
        service.refresh(old_refresh), service.refresh(current.refresh_token), return_exceptions=True
    )
    async with factory() as session:
        active = list(
            await session.scalars(select(AuthSession).where(AuthSession.revoked_at.is_(None)))
        )
    assert active == []


@pytest.mark.asyncio
async def test_logout_racing_rotation_revokes_every_generation(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    service, factory = auth_harness
    access, refresh = await sign_in(service)
    account = await service.authenticate_access(access)
    await asyncio.gather(
        service.refresh(refresh),
        service.logout(account=account, refresh_token=refresh),
        return_exceptions=True,
    )
    async with factory() as session:
        sessions = list(await session.scalars(select(AuthSession)))
        assert all(item.revoked_at is not None for item in sessions)


@pytest.mark.asyncio
async def test_expired_refresh_and_deleted_user_cannot_authenticate(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    service, factory = auth_harness
    _, refresh = await sign_in(service)
    with pytest.raises(RefreshTokenExpiredError):
        await service.refresh(refresh, now=datetime.now(UTC) + timedelta(days=31))
    access, _ = await sign_in(service)
    account = await service.authenticate_access(access)
    async with factory() as session, session.begin():
        user = await session.get(User, account.user.id)
        assert user is not None
        user.deleted_at = datetime.now(UTC)
    with pytest.raises(InvalidCredentialsError):
        await service.authenticate_access(access)
    with pytest.raises(InvalidCredentialsError):
        await sign_in(service)


@pytest.mark.asyncio
async def test_logout_does_not_accept_another_login_refresh_token(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    service, _ = auth_harness
    first_access, _ = await sign_in(service)
    second_access, second_refresh = await sign_in(service)
    account = await service.authenticate_access(first_access)
    with pytest.raises(InvalidCredentialsError):
        await service.logout(account=account, refresh_token=second_refresh)
    assert await service.authenticate_access(second_access)


@pytest.mark.asyncio
async def test_http_sign_in_me_refresh_logout_contract_and_no_token_logging(
    auth_harness: tuple[AuthService, async_sessionmaker[AsyncSession]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _ = auth_harness
    settings = Settings(app_env="test")
    app = create_app(settings, install_modules=False)
    database = Database(database_url())
    redis = RedisClient(settings.redis_url)
    install_auth(
        app,
        settings,
        database=database,
        redis=redis,
        service=service,
        cache=RedisAuthCache(FakeRedis()),
    )
    contract = json.loads(Path("contracts/openapi/v1/openapi.json").read_text())
    validator = Draft202012Validator(
        {"$ref": "#/components/schemas/AuthSession", "components": contract["components"]}
    )
    headers = {"Idempotency-Key": "auth-http-test-request"}
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            nonce = await client.post("/v1/auth/google/nonce", headers=headers)
            login = await client.post(
                "/v1/auth/google",
                headers=headers,
                json={
                    "id_token": "verified-by-google",
                    "nonce": nonce.json()["nonce"],
                    "device": {"platform": "android", "app_version": "1.0"},
                },
            )
            assert login.status_code == 200
            validator.validate(login.json())
            me = await client.get(
                "/v1/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
            )
            assert me.status_code == 200
            refreshed = await client.post(
                "/v1/auth/refresh",
                headers=headers,
                json={"refresh_token": login.json()["refresh_token"]},
            )
            assert refreshed.status_code == 200
            validator.validate(refreshed.json())
            authorization = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
            logout = await client.post(
                "/v1/auth/logout",
                headers={**headers, **authorization},
                json={"refresh_token": refreshed.json()["refresh_token"]},
            )
            assert logout.status_code == 204
            denied = await client.get("/v1/me", headers=authorization)
            assert denied.status_code == 401
            for response in (login, refreshed):
                assert response.json()["access_token"] not in caplog.text
                assert response.json()["refresh_token"] not in caplog.text
    finally:
        await database.close()
        await redis.close()
