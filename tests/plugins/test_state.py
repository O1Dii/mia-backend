"""Plugin state store: CAS semantics (in memory) and M10's PluginState (PostgreSQL)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from app.core.database import Database
from app.modules.plugins.builtin import (
    BuiltinManifestSource,
    CallIdentity,
    InMemoryPluginStateStore,
    SqlPluginStateStore,
)
from app.modules.plugins.builtin.state import (
    MAX_STATE_BYTES,
    PluginStateAccessError,
    PluginStateConflict,
    StateSnapshot,
    read_state,
    update_state,
)
from app.modules.users.models import User
from app.modules.workspace.catalog import CatalogPlugin, StaticPluginCatalog
from app.modules.workspace.schemas import CatalogInstallRequest
from app.modules.workspace.service import WorkspaceContext, WorkspaceService
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import RequestParamsMeta
from sqlalchemy import text

from tests.plugins.support import Caller, as_meta, connect, make_services, meta, server_for
from tests.workspace.harness import database_url, migrate_to_head, requires_postgres

IDENTITY = CallIdentity("u", "w", "i")


async def test_in_memory_store_is_cas_and_per_installation() -> None:
    store = InMemoryPluginStateStore()
    assert await store.load(IDENTITY, "builtin.notes") == StateSnapshot({}, 0)
    assert await store.save(IDENTITY, "builtin.notes", {"a": 1}, expected_version=0) == 1
    with pytest.raises(PluginStateConflict):
        await store.save(IDENTITY, "builtin.notes", {"a": 2}, expected_version=0)
    snapshot = await store.load(IDENTITY, "builtin.notes")
    snapshot.data["a"] = 99  # a copy: mutating it changes nothing
    assert (await store.load(IDENTITY, "builtin.notes")).data == {"a": 1}
    other = CallIdentity("u", "w", "i2")
    assert (await store.load(other, "builtin.notes")).version == 0
    with pytest.raises(PluginStateAccessError):
        await store.load(IDENTITY, "builtin.timer")


async def test_update_state_retries_on_conflict_and_does_not_save_on_error() -> None:
    store = InMemoryPluginStateStore()
    real_save = store.save
    conflicts = {"left": 2}

    async def flaky(identity: CallIdentity, plugin_id: str, data: dict[str, Any], **kw: int) -> int:
        if conflicts["left"]:
            conflicts["left"] -= 1
            raise PluginStateConflict
        return await real_save(identity, plugin_id, data, **kw)

    store.save = flaky  # type: ignore[method-assign]
    runs: list[int] = []

    def mutate(data: dict[str, Any]) -> int:
        runs.append(1)
        count: int = data.get("n", 0) + 1
        data["n"] = count
        return count

    assert await update_state(store, IDENTITY, "builtin.notes", mutate) == 1
    assert len(runs) == 3

    def boom(data: dict[str, Any]) -> None:
        data["n"] = 100
        raise ToolError("no")

    with pytest.raises(ToolError):
        await update_state(store, IDENTITY, "builtin.notes", boom)
    assert (await read_state(store, IDENTITY, "builtin.notes"))["n"] == 1


async def test_update_state_gives_up_and_limits_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.modules.plugins.builtin.state.random.uniform", lambda a, b: 0.0)
    store = InMemoryPluginStateStore()

    async def always_conflict(*args: Any, **kwargs: Any) -> int:
        raise PluginStateConflict

    store.save = always_conflict  # type: ignore[method-assign]
    with pytest.raises(ToolError, match="слишком активно"):
        await update_state(store, IDENTITY, "p", lambda data: data.update(a=1))
    with pytest.raises(ToolError, match="слишком велики"):
        await update_state(
            InMemoryPluginStateStore(),
            IDENTITY,
            "p",
            lambda data: data.update(blob="x" * MAX_STATE_BYTES),
        )


async def test_access_error_becomes_a_tool_error() -> None:
    store = InMemoryPluginStateStore()
    await store.save(IDENTITY, "builtin.timer", {}, expected_version=0)
    with pytest.raises(ToolError, match="не установлен"):
        await read_state(store, IDENTITY, "builtin.notes")
    with pytest.raises(ToolError, match="не установлен"):
        await update_state(store, IDENTITY, "builtin.notes", lambda data: None)


async def test_installation_bound_to_another_plugin_cannot_be_used_over_mcp() -> None:
    services = make_services()
    async with connect(server_for("builtin.timer", services)) as client:
        await Caller(client, meta(installation="shared-id"))("start", {"seconds": 60, "label": "a"})
    async with connect(server_for("builtin.notes", services)) as client:
        message = await Caller(client, meta(installation="shared-id")).error("list")
    assert "не установлен" in message


# --------------------------------------------------------------------------- PostgreSQL


@dataclass
class World:
    database: Database
    service: WorkspaceService
    ctx_a: WorkspaceContext
    ctx_b: WorkspaceContext
    installs: dict[tuple[str, str], str]
    """(user label, plugin id) -> installation id."""

    def meta(self, user: str, plugin_id: str) -> RequestParamsMeta:
        ctx = self.ctx_a if user == "a" else self.ctx_b
        return as_meta(
            {
                "mia/user_id": ctx.user_id,
                "mia/workspace_id": ctx.workspace_id,
                "mia/installation_id": self.installs[(user, plugin_id)],
            }
        )


@asynccontextmanager
async def world() -> AsyncIterator[World]:
    await asyncio.to_thread(migrate_to_head)
    database = Database(database_url())
    async with database.session_factory() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE users CASCADE"))
        users = [
            User(id=uuid.uuid4(), google_sub=f"m14-{label}", email=f"{label}@example.test")
            for label in ("a", "b")
        ]
        session.add_all(users)
    catalog = StaticPluginCatalog(
        CatalogPlugin(
            plugin_id=plugin.plugin_id,
            version=plugin.version,
            tools_digest="sha256:test",
            manifest=plugin.manifest,
        )
        for plugin in BuiltinManifestSource().list()
    )
    service = WorkspaceService(database.session_factory, catalog)
    contexts = {
        label: await service.context_for(str(user.id))
        for label, user in zip("ab", users, strict=True)
    }
    installs: dict[tuple[str, str], str] = {}
    for label, ctx in contexts.items():
        for plugin_id in ("builtin.timer", "builtin.notes"):
            result = await service.create_installation(
                ctx,
                CatalogInstallRequest(
                    plugin_id=plugin_id, version="1.0.0", tools_digest="sha256:test"
                ),
                idempotency_key=f"m14-{label}-{plugin_id}",
            )
            assert result.body is not None
            installs[(label, plugin_id)] = str(result.body["id"])
    try:
        yield World(database, service, contexts["a"], contexts["b"], installs)
    finally:
        await database.close()


@requires_postgres
async def test_sql_store_persists_in_plugin_state_and_isolates_tenants() -> None:
    async with world() as w:
        services = make_services(state=SqlPluginStateStore(w.database.session_factory))
        async with connect(server_for("builtin.notes", services)) as client:
            a = Caller(client, w.meta("a", "builtin.notes"))
            b = Caller(client, w.meta("b", "builtin.notes"))
            await a("add", {"text": "секрет A"})
            assert [n["text"] for n in (await a("list"))["notes"]] == ["секрет A"]
            assert (await b("list"))["notes"] == []
            # B cannot reach A's installation by guessing its id
            forged = Caller(
                client,
                as_meta(
                    {
                        **w.meta("b", "builtin.notes"),
                        "mia/installation_id": w.installs[("a", "builtin.notes")],
                    }
                ),
            )
            assert "не установлен" in await forged.error("list")
            forged_ws = Caller(
                client,
                as_meta({**w.meta("a", "builtin.notes"), "mia/workspace_id": w.ctx_b.workspace_id}),
            )
            assert "не установлен" in await forged_ws.error("list")
            # an installation of another plugin does not open this plugin's state
            wrong = Caller(client, w.meta("a", "builtin.timer"))
            assert "не установлен" in await wrong.error("list")
            garbage = Caller(
                client,
                as_meta({**w.meta("a", "builtin.notes"), "mia/installation_id": "not-a-uuid"}),
            )
            assert "не установлен" in await garbage.error("list")

        # M10's own API sees the same data under the same version
        view = await w.service.get_plugin_state(
            w.ctx_a, uuid.UUID(w.installs[("a", "builtin.notes")])
        )
        assert view.plugin_id == "builtin.notes"
        assert view.state_version == 1 and view.data["notes"][0]["text"] == "секрет A"
        empty = await w.service.get_plugin_state(
            w.ctx_b, uuid.UUID(w.installs[("b", "builtin.notes")])
        )
        assert empty.data == {} and empty.state_version == 0


@requires_postgres
async def test_sql_store_concurrent_calls_lose_no_updates() -> None:
    async with world() as w:
        services = make_services(state=SqlPluginStateStore(w.database.session_factory))
        async with connect(server_for("builtin.notes", services)) as client:
            call = Caller(client, w.meta("a", "builtin.notes"))
            await asyncio.gather(*(call("add", {"text": f"n{i}"}) for i in range(12)))
            assert len((await call("list"))["notes"]) == 12
        view = await w.service.get_plugin_state(
            w.ctx_a, uuid.UUID(w.installs[("a", "builtin.notes")])
        )
        assert view.state_version == 12


@requires_postgres
async def test_sql_store_state_survives_reinstall_and_inactive_installation_is_refused() -> None:
    async with world() as w:
        store = SqlPluginStateStore(w.database.session_factory)
        identity = CallIdentity(
            w.ctx_a.user_id, w.ctx_a.workspace_id, w.installs[("a", "builtin.timer")]
        )
        assert await store.save(identity, "builtin.timer", {"items": [1]}, expected_version=0) == 1
        with pytest.raises(PluginStateConflict):
            await store.save(identity, "builtin.timer", {"items": []}, expected_version=0)
        assert (await store.load(identity, "builtin.timer")).data == {"items": [1]}
        async with w.database.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE installations SET status = 'disabled' WHERE id = :id"),
                {"id": uuid.UUID(identity.installation_id)},
            )
        with pytest.raises(PluginStateAccessError):
            await store.load(identity, "builtin.timer")
