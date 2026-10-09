"""Fire timer/alarm/focus deadlines stored by the M14 builtin plugins.

The plugins only store items ``{id, label, status, deadline_ms, ...}`` in ``PluginState``;
this scanner (run by the worker) finds due ones and turns each into one inbox item plus
its push delivery. ``deadline_fires`` makes firing exactly-once across workers and
restarts; a paused-then-resumed timer gets a new deadline and therefore fires again.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    Text,
    cast,
    delete,
    func,
    literal,
    literal_column,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.jobs.models import DeadlineFireRow
from app.modules.notifications.content import plain, text_response, title
from app.modules.notifications.inbox import DEFAULT_CHANNELS, add_item
from app.modules.workspace.models import Installation, PluginState, Workspace

DEADLINE_PLUGINS: dict[str, str] = {
    "builtin.timer": "Таймер",
    "builtin.alarm": "Будильник",
    "builtin.focus": "Фокус",
}
LATE_AFTER_MS = 5 * 60 * 1000
MAX_LATE_MS = 24 * 3600 * 1000
"""Older deadlines are not fired (the plugins prune expired items after 24 h)."""
FIRE_RETENTION = timedelta(days=7)
"""Must exceed ``MAX_LATE_MS`` so a delivered deadline can never fire again."""
_DUE_PATH = (
    '$.items[*] ? ((@.status == "running" || @.status == "scheduled") && @.deadline_ms <= $now)'
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FiredDeadline:
    user_id: uuid.UUID
    plugin_id: str
    item_id: str
    inbox_item_id: uuid.UUID


def deadline_message(plugin_id: str, item: dict[str, Any], now_ms: int) -> tuple[str, str]:
    """``(title, speech)`` for a fired item."""
    noun = DEADLINE_PLUGINS.get(plugin_id, "Напоминание")
    label = plain(str(item.get("label") or noun), 80, fallback=noun)
    late = now_ms - int(item["deadline_ms"]) > LATE_AFTER_MS
    if plugin_id == "builtin.alarm":
        speech = f"Будильник «{label}»."
    elif plugin_id == "builtin.focus":
        speech = f"Фокус-сессия «{label}» закончилась."
    else:
        speech = f"Таймер «{label}» сработал."
    if late:
        moment = datetime.fromtimestamp(int(item["deadline_ms"]) / 1000, UTC)
        speech += f" Срок был {moment.strftime('%H:%M')} UTC, уведомление опоздало."
    return title(f"{noun}: {label}"), speech


class DeadlineScanner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
        batch: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._batch = batch

    async def run_once(self) -> list[FiredDeadline]:
        """Scan every deadline plugin state page by page; fire each due item once."""
        now = self._clock()
        now_ms = int(now.timestamp() * 1000)
        fired: list[FiredDeadline] = []
        after: tuple[uuid.UUID, str] | None = None
        while True:
            page, after = await self._page(now, now_ms, after, fired)
            if page < self._batch or after is None:
                break
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(DeadlineFireRow).where(DeadlineFireRow.fired_at < now - FIRE_RETENTION)
            )
        return fired

    async def _page(
        self,
        now: datetime,
        now_ms: int,
        after: tuple[uuid.UUID, str] | None,
        fired: list[FiredDeadline],
    ) -> tuple[int, tuple[uuid.UUID, str] | None]:
        since_ms = now_ms - MAX_LATE_MS
        statement = (
            select(PluginState.workspace_id, PluginState.plugin_id, PluginState.data)
            .add_columns(Workspace.owner_user_id)
            .join(Workspace, Workspace.id == PluginState.workspace_id)
            .where(
                PluginState.plugin_id.in_(list(DEADLINE_PLUGINS)),
                func.jsonb_path_exists(
                    PluginState.data,
                    literal_column(f"'{_DUE_PATH}'::jsonpath"),
                    func.jsonb_build_object(
                        cast(literal("now"), Text),
                        cast(literal(now_ms), BigInteger),
                        cast(literal("since"), Text),
                        cast(literal(since_ms), BigInteger),
                    ),
                ),
                select(Installation.id)
                .where(
                    Installation.workspace_id == PluginState.workspace_id,
                    Installation.plugin_id == PluginState.plugin_id,
                    Installation.status == "active",
                )
                .exists(),
            )
            .order_by(PluginState.workspace_id, PluginState.plugin_id)
            .limit(self._batch)
        )
        if after is not None:
            statement = statement.where(
                tuple_(PluginState.workspace_id, PluginState.plugin_id)
                > tuple_(literal(after[0]), literal(after[1]))
            )
        async with self._session_factory() as session, session.begin():
            rows = (await session.execute(statement)).all()
            for workspace_id, plugin_id, data, owner in rows:
                for item in _due_items(data, now_ms, since_ms):
                    inserted = await session.scalar(
                        insert(DeadlineFireRow)
                        .values(
                            workspace_id=workspace_id,
                            plugin_id=plugin_id,
                            item_id=str(item["id"])[:64],
                            deadline_ms=int(item["deadline_ms"]),
                            fired_at=now,
                        )
                        .on_conflict_do_nothing(constraint="pk_deadline_fires")
                        .returning(DeadlineFireRow.item_id)
                    )
                    if inserted is None:
                        continue
                    heading, speech = deadline_message(plugin_id, item, now_ms)
                    inbox = await add_item(
                        session,
                        user_id=owner,
                        source_type="plugin",
                        source_id=f"{plugin_id}:{item['id']}",
                        title=heading,
                        response=text_response(speech),
                        channels=DEFAULT_CHANNELS,
                        now=now,
                    )
                    fired.append(FiredDeadline(owner, plugin_id, str(item["id"]), inbox.id))
        last = (rows[-1][0], rows[-1][1]) if rows else None
        return len(rows), last


def _due_items(data: Any, now_ms: int, since_ms: int) -> list[dict[str, Any]]:
    items = data.get("items") if isinstance(data, dict) else None
    due: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("status") not in ("running", "scheduled"):
            continue
        deadline = item.get("deadline_ms")
        if (
            isinstance(deadline, int)
            and not isinstance(deadline, bool)
            and since_ms < deadline <= now_ms
        ):
            if item.get("id") is not None:
                due.append(item)
    return due
