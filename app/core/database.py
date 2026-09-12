"""Async PostgreSQL lifecycle and readiness probe."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    """Small database adapter; repositories are introduced by S11."""

    def __init__(self, url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the process-owned session factory for application services."""
        return self._session_factory

    async def ping(self) -> None:
        """Raise when PostgreSQL cannot serve a trivial query."""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        """Release engine resources during application shutdown."""
        await self._engine.dispose()
