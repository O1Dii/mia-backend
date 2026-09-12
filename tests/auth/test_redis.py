from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from app.core.redis import RedisClient
from app.modules.auth.cache import RedisAuthCache


@pytest.mark.asyncio
async def test_real_redis_nonce_expiry_atomic_consumption_and_limit_window() -> None:
    url = os.environ.get("MIA_TEST_REDIS_URL")
    if not url:
        pytest.skip("MIA_TEST_REDIS_URL is required for Redis integration tests")
    redis = RedisClient(url)
    cache = RedisAuthCache(redis)
    nonce = str(uuid4())
    try:
        assert await cache.store_nonce(nonce, ttl_seconds=1)
        await asyncio.sleep(1.1)
        assert not await cache.consume_nonce(nonce)
        assert await cache.store_nonce(nonce, ttl_seconds=10)
        results = await asyncio.gather(*(cache.consume_nonce(nonce) for _ in range(10)))
        assert sum(results) == 1
        limits = await asyncio.gather(
            *(cache.check_rate_limit("test", nonce, limit=3, window_seconds=1) for _ in range(10))
        )
        assert sum(result.allowed for result in limits) == 3
        await asyncio.sleep(1.1)
        assert (await cache.check_rate_limit("test", nonce, limit=3, window_seconds=1)).allowed
    finally:
        await redis.close()
