"""Async Redis lifecycle and readiness probe."""

from redis.asyncio import Redis
from redis.asyncio import from_url as redis_from_url


class RedisClient:
    """Small Redis adapter; nonce/session usage is introduced by later sessions."""

    def __init__(self, url: str) -> None:
        self._client: Redis = redis_from_url(url, decode_responses=True)  # type: ignore[no-untyped-call]

    async def ping(self) -> None:
        """Raise when Redis cannot answer a ping."""
        await self._client.ping()

    async def close(self) -> None:
        """Close pooled Redis connections during application shutdown."""
        await self._client.aclose()
