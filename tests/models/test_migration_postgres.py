"""The M11 Alembic revision: up/down round trip, chain position and model parity."""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from app.modules.models import models as model_tables
from app.modules.users.models import Base
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import create_async_engine

URL = os.environ.get("MIA_TEST_DATABASE_URL")
OWN_TABLES = {"user_data_keys", "model_connections", "model_gateway_settings"}
REVISION = "20261009_m11_models"
pytestmark = pytest.mark.skipif(URL is None, reason="MIA_TEST_DATABASE_URL is required")


def config() -> Config:
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", cast(str, URL))
    return configuration


def test_revision_is_chained_after_the_m10_workspace_head() -> None:
    script = ScriptDirectory.from_config(config())
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == "20261009_m10_0001"
    assert model_tables.ModelConnection.__tablename__ in OWN_TABLES


async def test_round_trip_and_model_parity() -> None:
    engine = create_async_engine(cast(str, URL))

    async def tables() -> set[str]:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda c: set(inspect(c).get_table_names()))
        return names & OWN_TABLES

    await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.downgrade, config(), "20260913_0003")  # rebuild from the script
    assert await tables() == set()
    await asyncio.to_thread(command.upgrade, config(), "head")
    assert await tables() == OWN_TABLES

    def diff(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        found = compare_metadata(context, Base.metadata)
        return [d for d in found if _touches_own_tables(d)]

    async with engine.connect() as connection:
        assert await connection.run_sync(diff) == []

    await engine.dispose()


def _touches_own_tables(entry: Any) -> bool:
    for item in entry if isinstance(entry, list) else [entry]:
        for element in item:
            table = getattr(element, "table", None)
            names = (element, getattr(element, "name", None), getattr(table, "name", None))
            if any(name in OWN_TABLES for name in names if isinstance(name, str)):
                return True
    return False
