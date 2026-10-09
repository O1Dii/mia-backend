"""M10 Alembic revision: upgrade/downgrade and model parity."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from app.modules.auth import models as auth_models  # noqa: F401
from app.modules.conversations import models as conversation_models  # noqa: F401
from app.modules.tools import models as tool_models  # noqa: F401
from app.modules.users.models import Base
from app.modules.workspace import models as workspace_models  # noqa: F401
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.workspace.harness import database_url, requires_postgres

pytestmark = requires_postgres
M10_TABLES = {
    "web_sessions",
    "workspaces",
    "installations",
    "plugin_states",
    "widget_instances",
    "plugin_reviews",
    "workspace_mutations",
    "legacy_web_imports",
}


def alembic(action: str, revision: str) -> None:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", database_url())
    getattr(command, action)(configuration, revision)


async def present_tables() -> set[str]:
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {str(row[0]) for row in rows} & M10_TABLES
    finally:
        await engine.dispose()


async def schema_diff() -> list[Any]:
    def compare(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(compare)
    finally:
        await engine.dispose()


async def test_revision_downgrades_and_upgrades_cleanly() -> None:
    await asyncio.to_thread(alembic, "upgrade", "head")
    assert await present_tables() == M10_TABLES
    await asyncio.to_thread(alembic, "downgrade", "20260913_0003")
    assert await present_tables() == set()
    await asyncio.to_thread(alembic, "upgrade", "head")
    assert await present_tables() == M10_TABLES


async def test_models_match_the_migration() -> None:
    await asyncio.to_thread(alembic, "upgrade", "head")
    assert await schema_diff() == []
