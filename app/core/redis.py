"""Async Redis lifecycle and readiness probe."""

from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url


class RedisClient:
    """Small Redis adapter; nonce/session usage is introduced by later sessions."""

    def __init__(self, url: str) -> None:
        self._client: Redis = redis_from_url(url, decode_responses=True)  # type: ignore[no-untyped-call]

    async def ping(self) -> None:
        """Raise when Redis cannot answer a ping."""
        await self._client.ping()

    async def set_if_absent(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        """Store a short-lived value only when the key does not already exist."""
        result = await self._client.set(key, value, ex=ttl_seconds, nx=True)
        return bool(result)

    async def consume(self, key: str) -> str | None:
        """Atomically read and remove a one-time value."""
        value = await self._client.getdel(key)
        return str(value) if value is not None else None

    async def increment_with_expiry(self, key: str, *, ttl_seconds: int) -> tuple[int, int]:
        """Atomically increment a fixed-window counter and return count and remaining TTL."""
        script = """
        local count = redis.call('INCR', KEYS[1])
        if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
        local ttl = redis.call('TTL', KEYS[1])
        return {count, ttl}
        """
        result = await cast(
            Awaitable[list[int]], self._client.eval(script, 1, key, str(ttl_seconds))
        )
        return int(result[0]), max(1, int(result[1]))

    async def reserve_daily_budget(
        self, counter_key: str, request_key: str, *, ttl_seconds: int, limit: int
    ) -> tuple[bool, int]:
        """Charge an idempotent request once, without incrementing denied counters."""
        script = """
        if redis.call('EXISTS', KEYS[2]) == 1 then
            return {1, math.max(1, redis.call('TTL', KEYS[1]))}
        end
        local count = tonumber(redis.call('GET', KEYS[1]) or '0')
        if count >= tonumber(ARGV[2]) then
            return {0, math.max(1, redis.call('TTL', KEYS[1]))}
        end
        count = redis.call('INCR', KEYS[1])
        if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
        redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
        return {1, math.max(1, redis.call('TTL', KEYS[1]))}
        """
        result = await cast(
            Awaitable[list[int]],
            self._client.eval(script, 2, counter_key, request_key, str(ttl_seconds), str(limit)),
        )
        return bool(result[0]), max(1, int(result[1]))

    async def close(self) -> None:
        """Close pooled Redis connections during application shutdown."""
        await self._client.aclose()
