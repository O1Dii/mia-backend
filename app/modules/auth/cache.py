"""Redis-backed one-time nonce and fixed-window rate-limit storage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


class AuthRedis(Protocol):
    async def set_if_absent(self, key: str, value: str, *, ttl_seconds: int) -> bool: ...

    async def consume(self, key: str) -> str | None: ...

    async def increment_with_expiry(self, key: str, *, ttl_seconds: int) -> tuple[int, int]: ...


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    retry_after: int


class RedisAuthCache:
    """Keep auth abuse-control state ephemeral and atomic in Redis."""

    def __init__(self, redis: AuthRedis) -> None:
        self._redis = redis

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    async def store_nonce(self, nonce: str, *, ttl_seconds: int) -> bool:
        return await self._redis.set_if_absent(
            f"auth:nonce:{self._digest(nonce)}", "1", ttl_seconds=ttl_seconds
        )

    async def consume_nonce(self, nonce: str) -> bool:
        return await self._redis.consume(f"auth:nonce:{self._digest(nonce)}") is not None

    async def check_rate_limit(
        self, scope: str, identity: str, *, limit: int, window_seconds: int
    ) -> RateLimitResult:
        identity_hash = self._digest(identity)
        count, retry_after = await self._redis.increment_with_expiry(
            f"auth:rate:{scope}:{identity_hash}", ttl_seconds=window_seconds
        )
        return RateLimitResult(allowed=count <= limit, retry_after=retry_after)
