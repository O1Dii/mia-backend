"""Async Alembic environment for the MIA PostgreSQL schema."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from app.modules.auth import models as auth_models  # noqa: F401
from app.modules.conversations import models as conversation_models  # noqa: F401
from app.modules.tools import models as tool_models  # noqa: F401
from app.modules.users.models import Base
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """Read process configuration only; no dotenv or secret file is consulted."""
    return os.environ.get(
        "MIA_TEST_DATABASE_URL",
        os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url")),
    )


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
