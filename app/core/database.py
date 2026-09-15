"""Async PostgreSQL lifecycle and readiness probe."""

from pathlib import Path

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

    async def schema_revision(self) -> str | None:
        """Return the applied Alembic revision, or ``None`` for an unmigrated database."""
        async with self._engine.connect() as connection:
            exists = await connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
            if exists is None:
                return None
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            return str(revision) if revision is not None else None

    async def close(self) -> None:
        """Release engine resources during application shutdown."""
        await self._engine.dispose()


def migration_head(project_root: Path | None = None) -> str | None:
    """Return the packaged Alembic head so readiness can reject an unmigrated schema."""
    root = project_root or Path(__file__).resolve().parents[2]
    script_location = root / "migrations"
    if not (script_location / "env.py").is_file():
        return None
    from alembic.script import ScriptDirectory

    return ScriptDirectory(str(script_location)).get_current_head()
