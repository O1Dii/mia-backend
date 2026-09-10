"""Application runtime dependencies."""

import asyncio
from dataclasses import dataclass

from app.core.database import Database
from app.core.redis import RedisClient


@dataclass(frozen=True)
class Runtime:
    """Infrastructure dependencies required for readiness."""

    database: Database
    redis: RedisClient

    async def is_ready(self) -> bool:
        """Check PostgreSQL and Redis concurrently without leaking implementation details."""
        results = await asyncio.gather(
            self.database.ping(), self.redis.ping(), return_exceptions=True
        )
        return not any(isinstance(result, BaseException) for result in results)

    async def close(self) -> None:
        """Close dependencies concurrently."""
        await asyncio.gather(self.database.close(), self.redis.close())
