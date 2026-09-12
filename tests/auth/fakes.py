from __future__ import annotations

from app.modules.auth.cache import RedisAuthCache


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    async def set_if_absent(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        del ttl_seconds
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def consume(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def increment_with_expiry(self, key: str, *, ttl_seconds: int) -> tuple[int, int]:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key], ttl_seconds


def fake_auth_cache() -> RedisAuthCache:
    return RedisAuthCache(FakeRedis())
