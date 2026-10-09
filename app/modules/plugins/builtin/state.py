"""Plugin state behind a small protocol: PostgreSQL ``PluginState`` and an in-memory twin.

A server addresses its state by the call identity (``mia/installation_id``). The SQL store
resolves the installation to ``(workspace_id, plugin_id)`` (M10 keys ``PluginState`` that
way, so state survives a reinstall) after checking that the installation belongs to the
calling user's workspace and to the very plugin that asks. Every write is a
compare-and-set; :func:`update_state` retries it, so concurrent calls never lose updates.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.plugins.builtin.identity import CallIdentity
from app.modules.workspace.errors import VersionConflictError
from app.modules.workspace.models import Installation, PluginState, Workspace
from app.modules.workspace.service import compare_and_set_plugin_state

MAX_STATE_BYTES = 262_144
CAS_ATTEMPTS = 20

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    data: dict[str, Any]
    version: int
    """0 means the state does not exist yet."""


class PluginStateConflict(Exception):
    """``expected_version`` is stale."""


class PluginStateAccessError(Exception):
    """Unknown installation, another tenant's installation, a different plugin or inactive."""


class PluginStateStore(Protocol):
    async def load(self, identity: CallIdentity, plugin_id: str) -> StateSnapshot: ...

    async def save(
        self,
        identity: CallIdentity,
        plugin_id: str,
        data: dict[str, Any],
        *,
        expected_version: int,
    ) -> int:
        """Compare-and-set; returns the new version or raises ``PluginStateConflict``."""
        ...


class InMemoryPluginStateStore:
    """Process-local store with the semantics of the SQL one (for tests and dev)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[str, dict[str, Any], int]] = {}

    async def load(self, identity: CallIdentity, plugin_id: str) -> StateSnapshot:
        key = (identity.workspace_id, identity.installation_id)
        row = self._rows.get(key)
        if row is None:
            return StateSnapshot({}, 0)
        owner, data, version = row
        if owner != plugin_id:
            raise PluginStateAccessError("installation belongs to another plugin")
        return StateSnapshot(copy.deepcopy(data), version)

    async def save(
        self,
        identity: CallIdentity,
        plugin_id: str,
        data: dict[str, Any],
        *,
        expected_version: int,
    ) -> int:
        key = (identity.workspace_id, identity.installation_id)
        row = self._rows.get(key)
        if row is not None and row[0] != plugin_id:
            raise PluginStateAccessError("installation belongs to another plugin")
        current = 0 if row is None else row[2]
        if current != expected_version:
            raise PluginStateConflict
        self._rows[key] = (plugin_id, copy.deepcopy(data), current + 1)
        return current + 1


class SqlPluginStateStore:
    """``PluginState`` of M10, addressed through the caller's installation."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load(self, identity: CallIdentity, plugin_id: str) -> StateSnapshot:
        async with self._session_factory() as session:
            workspace_id = await self._authorize(session, identity, plugin_id)
            row = await session.get(PluginState, (workspace_id, plugin_id))
            if row is None:
                return StateSnapshot({}, 0)
            return StateSnapshot(copy.deepcopy(dict(row.data)), row.state_version)

    async def save(
        self,
        identity: CallIdentity,
        plugin_id: str,
        data: dict[str, Any],
        *,
        expected_version: int,
    ) -> int:
        async with self._session_factory() as session, session.begin():
            workspace_id = await self._authorize(session, identity, plugin_id)
            try:
                return await compare_and_set_plugin_state(
                    session,
                    workspace_id=workspace_id,
                    plugin_id=plugin_id,
                    data=data,
                    expected_version=expected_version,
                )
            except VersionConflictError as error:
                raise PluginStateConflict from error

    @staticmethod
    async def _authorize(
        session: AsyncSession, identity: CallIdentity, plugin_id: str
    ) -> uuid.UUID:
        try:
            installation_id = uuid.UUID(identity.installation_id)
            workspace_id = uuid.UUID(identity.workspace_id)
            user_id = uuid.UUID(identity.user_id)
        except ValueError:
            raise PluginStateAccessError("malformed identity") from None
        row = (
            await session.execute(
                select(Installation.plugin_id, Installation.status)
                .join(Workspace, Workspace.id == Installation.workspace_id)
                .where(
                    Installation.id == installation_id,
                    Installation.workspace_id == workspace_id,
                    Workspace.owner_user_id == user_id,
                )
            )
        ).one_or_none()
        if row is None or row.plugin_id != plugin_id or row.status != "active":
            raise PluginStateAccessError("installation is not available")
        return workspace_id


async def update_state(
    store: PluginStateStore,
    identity: CallIdentity,
    plugin_id: str,
    mutate: Callable[[dict[str, Any]], T],
) -> T:
    """Load → ``mutate`` the data in place → CAS-save, retrying on a concurrent change.

    ``mutate`` is synchronous and must be free of side effects: it may run again after a
    conflict. If it raises, nothing is saved. Returns what ``mutate`` returned.
    """
    for attempt in range(CAS_ATTEMPTS):
        snapshot = await _load(store, identity, plugin_id)
        result = mutate(snapshot.data)
        if len(json.dumps(snapshot.data, ensure_ascii=False)) > MAX_STATE_BYTES:
            raise ToolError("Данные плагина слишком велики. Удалите лишнее.")
        try:
            await store.save(identity, plugin_id, snapshot.data, expected_version=snapshot.version)
        except PluginStateConflict:
            # Jittered backoff so concurrent writers of one installation do not starve.
            await asyncio.sleep(random.uniform(0, 0.005 * (attempt + 1)))
            continue
        except PluginStateAccessError:
            raise _access_error() from None
        return result
    raise ToolError("Данные плагина изменяются слишком активно. Повторите вызов.")


async def read_state(
    store: PluginStateStore, identity: CallIdentity, plugin_id: str
) -> dict[str, Any]:
    return (await _load(store, identity, plugin_id)).data


async def _load(store: PluginStateStore, identity: CallIdentity, plugin_id: str) -> StateSnapshot:
    try:
        return await store.load(identity, plugin_id)
    except PluginStateAccessError:
        raise _access_error() from None


def _access_error() -> ToolError:
    return ToolError("Плагин не установлен для этого пользователя или отключён.")
