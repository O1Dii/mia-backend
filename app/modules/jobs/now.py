"""«Сейчас» (M74): what is active for the account right now — timers, alarms, focus, jobs.

Read-only view over the stored deadline items of the builtin plugins (the item shape is
the public contract of ``app/modules/plugins/builtin/deadlines.py``: ``id``, ``label``,
``status``, ``deadline_ms``, ``remaining_ms``, ``duration_ms``) and the user's active
jobs. Only items of active installations are listed; expired timers are not. Each item
says how the client cancels it: a tile tool call of its installation (no model involved)
or ``POST /v1/jobs/{id}/cancel``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.conversations.chats import clip, isoformat
from app.modules.jobs.chats import origin_run_id, source_chats
from app.modules.jobs.models import ACTIVE_JOB_STATUSES, JobRow
from app.modules.workspace.models import Installation, PluginState, Workspace


@dataclass(frozen=True, slots=True)
class NowPlugin:
    kind: str
    tile_id: str


NOW_PLUGINS: Final[dict[str, NowPlugin]] = {
    "builtin.timer": NowPlugin("timer", "active-timers"),
    "builtin.alarm": NowPlugin("alarm", "alarms"),
    "builtin.focus": NowPlugin("focus", "focus"),
}
"""Plugin → item kind and the tile whose ``cancel`` tool the client calls (``mia.json``)."""
MAX_JOBS: Final = 20
LABEL_MAX: Final = 120
_ACTIVE_ITEM_STATES = ("running", "scheduled", "paused")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _from_ms(value: int | None) -> str | None:
    return None if value is None else isoformat(datetime.fromtimestamp(value / 1000, UTC))


def plugin_item(
    plugin_id: str, installation_id: uuid.UUID, raw: object, now_ms: int
) -> dict[str, Any] | None:
    """One ``NowItem`` from a stored deadline item; None for expired or malformed ones."""
    plugin = NOW_PLUGINS.get(plugin_id)
    if plugin is None or not isinstance(raw, dict):
        return None
    status, item_id, label = raw.get("status"), raw.get("id"), raw.get("label")
    if status not in _ACTIVE_ITEM_STATES or not isinstance(item_id, str | int):
        return None
    deadline = _int(raw.get("deadline_ms"))
    if status == "paused":
        remaining = _int(raw.get("remaining_ms"))
        if remaining is None or remaining <= 0:
            return None
        deadline = None
    else:
        if deadline is None or deadline <= now_ms:
            return None
        remaining = deadline - now_ms
    return {
        "kind": plugin.kind,
        "id": str(item_id),
        "label": clip(label, LABEL_MAX) if isinstance(label, str) and label.strip() else None,
        "state": status,
        "deadline_at": _from_ms(deadline),
        "remaining_ms": remaining,
        "duration_ms": _int(raw.get("duration_ms")),
        "started_at": _from_ms(_int(raw.get("created_ms"))),
        "plugin_id": plugin_id,
        "installation_id": str(installation_id),
        "job_id": None,
        "run_id": None,
        "conversation_id": None,
        "cancel": {
            "type": "tile_tool",
            "installation_id": str(installation_id),
            "tile_id": plugin.tile_id,
            "name": "cancel",
            "arguments": {"id": str(item_id)},
        },
    }


def _sort_key(item: dict[str, Any]) -> tuple[int, int]:
    if item["kind"] == "job":
        return (2, 0)
    paused = item["state"] == "paused"
    return (1 if paused else 0, int(item["remaining_ms"] or 0))


class NowService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock

    async def now(self, *, user_id: uuid.UUID) -> dict[str, Any]:
        """``NowPage``: deadline items by time left (paused after running), then jobs."""
        now = self._clock()
        now_ms = int(now.timestamp() * 1000)
        async with self._sessions() as session:
            plugin_items = await self._plugin_items(session, user_id, now_ms)
            jobs = await self._jobs(session, user_id)
        items = sorted(plugin_items, key=_sort_key) + jobs
        return {"server_time": isoformat(now), "items": items}

    @staticmethod
    async def _plugin_items(
        session: AsyncSession, user_id: uuid.UUID, now_ms: int
    ) -> list[dict[str, Any]]:
        rows = await session.execute(
            select(PluginState.plugin_id, PluginState.data, Installation.id)
            .join(Workspace, Workspace.id == PluginState.workspace_id)
            .join(
                Installation,
                (Installation.workspace_id == PluginState.workspace_id)
                & (Installation.plugin_id == PluginState.plugin_id),
            )
            .where(
                Workspace.owner_user_id == user_id,
                PluginState.plugin_id.in_(list(NOW_PLUGINS)),
                Installation.status == "active",
            )
        )
        items: list[dict[str, Any]] = []
        for plugin_id, data, installation_id in rows:
            stored = data.get("items") if isinstance(data, dict) else None
            for raw in stored if isinstance(stored, list) else []:
                item = plugin_item(plugin_id, installation_id, raw, now_ms)
                if item is not None:
                    items.append(item)
        return items

    @staticmethod
    async def _jobs(session: AsyncSession, user_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = list(
            (
                await session.scalars(
                    select(JobRow)
                    .where(JobRow.user_id == user_id, JobRow.status.in_(ACTIVE_JOB_STATUSES))
                    .order_by(JobRow.created_at.desc())
                    .limit(MAX_JOBS)
                )
            ).all()
        )
        origins = {row.id: origin_run_id(row.origin_type, row.origin_id) for row in rows}
        chats = await source_chats(
            session, user_id, [run_id for run_id in origins.values() if run_id is not None]
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            origin = origins[row.id]
            chat = chats.get(origin) if origin is not None else None
            items.append(
                {
                    "kind": "job",
                    "id": str(row.id),
                    "label": clip(row.goal, LABEL_MAX),
                    "state": row.status,
                    "deadline_at": None,
                    "remaining_ms": None,
                    "duration_ms": None,
                    "started_at": isoformat(row.started_at or row.created_at),
                    "plugin_id": None,
                    "installation_id": None,
                    "job_id": str(row.id),
                    "run_id": str(row.run_id) if row.run_id is not None else None,
                    "conversation_id": str(chat.conversation_id) if chat else None,
                    "cancel": {"type": "job", "job_id": str(row.id)},
                }
            )
        return items
