import hashlib

import pytest
from app.modules.auth.cache import RedisAuthCache

from tests.auth.fakes import FakeRedis


@pytest.mark.asyncio
async def test_nonce_is_stored_by_digest_and_consumed_once() -> None:
    redis = FakeRedis()
    cache = RedisAuthCache(redis)
    nonce = "one-time-nonce-value"

    assert await cache.store_nonce(nonce, ttl_seconds=300)
    expected_key = f"auth:nonce:{hashlib.sha256(nonce.encode()).hexdigest()}"
    assert redis.values == {expected_key: "1"}
    assert await cache.consume_nonce(nonce)
    assert not await cache.consume_nonce(nonce)


@pytest.mark.asyncio
async def test_rate_limit_is_scoped_and_reports_retry_after() -> None:
    cache = RedisAuthCache(FakeRedis())

    assert (await cache.check_rate_limit("nonce", "ip", limit=1, window_seconds=60)).allowed
    denied = await cache.check_rate_limit("nonce", "ip", limit=1, window_seconds=60)

    assert not denied.allowed
    assert denied.retry_after == 60
