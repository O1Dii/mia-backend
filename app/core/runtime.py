"""Application runtime dependencies."""

import asyncio
import logging
from dataclasses import dataclass

from app.core.database import Database
from app.core.metrics import METRICS
from app.core.redis import RedisClient

logger = logging.getLogger(__name__)

READINESS_TIMEOUT_SECONDS = 2.0


class _SchemaNotAtHeadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Runtime:
    """Infrastructure dependencies required for readiness."""

    database: Database
    redis: RedisClient
    expected_schema_revision: str | None = None
    readiness_timeout_seconds: float = READINESS_TIMEOUT_SECONDS

    async def _database_ready(self) -> None:
        await self.database.ping()
        if self.expected_schema_revision is None:
            return
        if await self.database.schema_revision() != self.expected_schema_revision:
            raise _SchemaNotAtHeadError

    async def is_ready(self) -> bool:
        """Check PostgreSQL, schema head and Redis without leaking implementation details."""
        failed: str | None = None
        try:
            async with asyncio.timeout(self.readiness_timeout_seconds):
                database, redis = await asyncio.gather(
                    self._database_ready(), self.redis.ping(), return_exceptions=True
                )
            if isinstance(database, _SchemaNotAtHeadError):
                failed = "schema"
            elif isinstance(database, BaseException):
                failed = "database"
            elif isinstance(redis, BaseException):
                failed = "redis"
        except TimeoutError:
            failed = "timeout"
        METRICS.inc("mia_readiness_checks_total", {"result": "ok" if failed is None else failed})
        if failed is not None:
            logger.warning("readiness_failed", extra={"dependency": failed})
        return failed is None

    async def close(self) -> None:
        """Close dependencies concurrently."""
        await asyncio.gather(self.database.close(), self.redis.close())
