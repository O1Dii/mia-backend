"""Bearer -> web session one-time hand-off for the Android WebView (M24)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest
from app.api.v1.web_handoff import install_web_handoff
from app.core.config import Settings
from app.core.redis import RedisClient
from app.modules.auth.web_handoff import (
    InvalidLandingPathError,
    normalize_landing_path,
)
from pydantic import SecretStr
from sqlalchemy import func, select

from tests.auth.fakes import FakeRedis
from tests.workspace.harness import (
    SECRET,
    Harness,
    android_login,
    build_harness,
    key,
    requires_postgres,
)


@dataclass
class Rig:
    harness: Harness
    redis: FakeRedis


@asynccontextmanager
async def rigged(*, exchange_limit: int = 1000) -> AsyncIterator[Rig]:
    async with build_harness() as harness:
        redis = FakeRedis()
        settings = Settings(
            app_env="test",
            auth_jwt_secret=SecretStr(SECRET),
            auth_exchange_rate_limit=exchange_limit,
            auth_authenticated_rate_limit=5000,
        )
        install_web_handoff(harness.app, settings, session_factory=harness.factory, redis=redis)
        yield Rig(harness, redis)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    async with rigged() as value:
        yield value


def code_of(body: dict[str, str]) -> str:
    query = parse_qs(urlsplit(body["redeem_path"]).query)
    assert query["code"] == [body["code"]]
    return body["code"]


pytestmark = requires_postgres


async def test_bearer_code_becomes_a_web_session_and_redirects(rig: Rig) -> None:
    async with rig.harness.client() as android, rig.harness.client() as webview:
        bearer = await android_login(android, "sub-handoff")
        issued = await android.post(
            "/v1/auth/web/handoff", headers={**bearer, **key()}, json={"path": "/store"}
        )
        assert issued.status_code == 201, issued.text
        assert issued.headers["cache-control"] == "no-store"
        body = issued.json()
        assert body["redeem_path"].startswith("/v1/auth/web/handoff/redeem?code=")

        redeemed = await webview.get(body["redeem_path"], follow_redirects=False)
        assert redeemed.status_code == 303
        assert redeemed.headers["location"] == "/store"
        assert redeemed.headers["referrer-policy"] == "no-referrer"
        cookie = redeemed.headers["set-cookie"].lower()
        assert "mia_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie

        me_web = await webview.get("/v1/me")
        me_android = await android.get("/v1/me", headers=bearer)
        assert me_web.status_code == 200
        assert me_web.json()["id"] == me_android.json()["id"]


async def test_code_is_single_use(rig: Rig) -> None:
    async with rig.harness.client() as android, rig.harness.client() as webview:
        bearer = await android_login(android, "sub-once")
        body = (await android.post("/v1/auth/web/handoff", headers={**bearer, **key()})).json()
        first = await webview.get(body["redeem_path"], follow_redirects=False)
        assert first.status_code == 303
        assert first.headers["location"] == "/chat"
        async with rig.harness.client() as other:
            second = await other.get(body["redeem_path"], follow_redirects=False)
            assert second.status_code == 401
            assert second.json()["error"]["code"] == "web_handoff_invalid"
            assert "mia_session" not in other.cookies


async def test_expired_unknown_and_malformed_codes_fail_without_a_session(rig: Rig) -> None:
    async with rig.harness.client() as android, rig.harness.client() as webview:
        bearer = await android_login(android, "sub-expiry")
        body = (await android.post("/v1/auth/web/handoff", headers={**bearer, **key()})).json()
        rig.redis.values.clear()  # what the Redis TTL does after 60 s
        expired = await webview.get(body["redeem_path"], follow_redirects=False)
        assert expired.status_code == 401
        for bad in ("short", "x" * 43, "a" * 129):
            refused = await webview.get(
                "/v1/auth/web/handoff/redeem", params={"code": bad}, follow_redirects=False
            )
            assert refused.status_code == 401
        missing = await webview.get("/v1/auth/web/handoff/redeem", follow_redirects=False)
        assert missing.status_code == 422 or missing.status_code == 400
        assert "mia_session" not in webview.cookies


async def test_only_the_hash_of_the_code_is_stored_with_a_short_ttl(rig: Rig) -> None:
    async with rig.harness.client() as android:
        bearer = await android_login(android, "sub-hash")
        body = (await android.post("/v1/auth/web/handoff", headers={**bearer, **key()})).json()
    assert body["code"] not in "".join(rig.redis.values)
    assert body["code"] not in "".join(rig.redis.values.values())
    assert all(name.startswith("auth:web-handoff:") for name in rig.redis.values)


async def test_issuing_needs_a_bearer_not_a_cookie_or_nothing(rig: Rig) -> None:
    async with rig.harness.client() as anonymous:
        denied = await anonymous.post("/v1/auth/web/handoff", headers=key())
        assert denied.status_code == 401
    async with rig.harness.client() as android, rig.harness.client() as browser:
        bearer = await android_login(android, "sub-cookie")
        body = (await android.post("/v1/auth/web/handoff", headers={**bearer, **key()})).json()
        await browser.get(body["redeem_path"], follow_redirects=False)
        assert "mia_session" in browser.cookies
        forbidden = await browser.post("/v1/auth/web/handoff", headers=key())
        assert forbidden.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/store",
        "//evil.example/store",
        "/\\evil.example",
        "/store#frag",
        "/unknown-page",
        "/store/../../etc",
        "/%2e%2e/store",
        "/store%2f..%2fevil",
        "store",
        "javascript:alert(1)",
        "/store?x=<script>",
        "/" + "a" * 600,
    ],
)
async def test_landing_path_is_never_an_open_redirect(rig: Rig, path: str) -> None:
    async with rig.harness.client() as android:
        bearer = await android_login(android, "sub-redirect")
        refused = await android.post(
            "/v1/auth/web/handoff", headers={**bearer, **key()}, json={"path": path}
        )
        assert refused.status_code == 400, path
        assert refused.json()["error"]["code"] == "web_handoff_path_invalid"
    assert not rig.redis.values


def test_normalizer_accepts_the_web_pages() -> None:
    for path in ("/store", "/store/plugin/mia.timer", "/installed", "/models", "/chat", "/inbox"):
        assert normalize_landing_path(path) == path
    assert normalize_landing_path("/store/connected?connection=ok&plugin=a") == (
        "/store/connected?connection=ok&plugin=a"
    )
    assert normalize_landing_path(None) == "/chat"
    with pytest.raises(InvalidLandingPathError):
        normalize_landing_path("/v1/me")


async def test_foreign_session_is_not_created_for_a_deleted_user(rig: Rig) -> None:
    from app.modules.auth.models import WebSession
    from app.modules.users.models import User

    async with rig.harness.client() as android, rig.harness.client() as webview:
        bearer = await android_login(android, "sub-deleted")
        body = (await android.post("/v1/auth/web/handoff", headers={**bearer, **key()})).json()
        async with rig.harness.factory() as session, session.begin():
            user = (await session.scalars(select(User))).one()
            user.deleted_at = user.created_at
        refused = await webview.get(body["redeem_path"], follow_redirects=False)
        assert refused.status_code == 401
    async with rig.harness.factory() as session:
        assert await session.scalar(select(func.count()).select_from(WebSession)) == 0


async def test_redeem_counts_attempts_per_ip(rig: Rig) -> None:
    async with rig.harness.client() as webview:
        for _ in range(3):
            await webview.get("/v1/auth/web/handoff/redeem", params={"code": "z" * 43})
    keys = [name for name in rig.redis.counts if name.startswith("auth:rate:handoff-redeem-ip:")]
    assert len(keys) == 1 and rig.redis.counts[keys[0]] == 3


async def test_redeem_is_refused_after_the_limit() -> None:
    async with rigged(exchange_limit=2) as limited, limited.harness.client() as webview:
        statuses = [
            (
                await webview.get("/v1/auth/web/handoff/redeem", params={"code": "z" * 43})
            ).status_code
            for _ in range(3)
        ]
    assert statuses == [401, 401, 429]


@pytest.mark.skipif(
    os.environ.get("MIA_TEST_REDIS_URL") is None, reason="MIA_TEST_REDIS_URL is required"
)
async def test_real_redis_code_expires_and_is_consumed_atomically() -> None:
    redis = RedisClient(os.environ["MIA_TEST_REDIS_URL"])
    try:
        async with build_harness() as harness:
            settings = Settings(app_env="test", auth_jwt_secret=SecretStr(SECRET))
            service = install_web_handoff(
                harness.app, settings, session_factory=harness.factory, redis=redis
            )
            async with harness.client() as android:
                bearer = await android_login(android, "sub-real-redis")
                body = (
                    await android.post("/v1/auth/web/handoff", headers={**bearer, **key()})
                ).json()
            digest = hashlib.sha256(body["code"].encode()).hexdigest()
            ttl = await redis._client.ttl(f"auth:web-handoff:{digest}")
            assert 0 < ttl <= 60
            first = await service.redeem(body["code"])
            second = await service.redeem(body["code"])
            assert first is not None and second is None
    finally:
        await redis.close()
