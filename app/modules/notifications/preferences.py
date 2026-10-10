"""What the user chose to receive, and where (M77, OI-M41-002).

Routing is settings, not code: the inbox always gets the item; the delivery channels are those the
user switched on, filtered by the «event type × channel» table. No row = the default: push is on
for a new user, every other channel is off until its owner asks «Присылать сюда уведомления Mia?»
and the user answers; every cell of the table is on. An explicit ``delivery.channels`` of a job
or a ``notify`` call never reaches this module: it wins over the settings.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.notifications.channels import (
    BUILTIN_CHANNELS,
    ChannelRegistry,
    is_channel_name,
)
from app.modules.notifications.models import NotificationChannelSettingRow, NotificationRouteRow

EventType = Literal["timer_alarm", "job_result", "approval", "plugin_message"]
EVENT_TYPES: tuple[EventType, ...] = get_args(EventType)
EVENT_LABELS: dict[str, str] = {
    "timer_alarm": "Таймеры и будильники",
    "job_result": "Результаты задач",
    "approval": "Подтверждения",
    "plugin_message": "Сообщения плагинов",
}
DEFAULT_ENABLED: tuple[str, ...] = ("push",)
"""Channels that are on for a user who never chose; «inbox» is always on and is not a channel."""
CHANNEL_ORDER = {"push": 0, "telegram": 1}


class PreferencesError(Exception):
    """A public, stable ``code`` (``contracts/mvp/v1/error-codes.json``, owner M77)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(UTC)


def _ordered(names: set[str]) -> list[str]:
    return sorted(names, key=lambda name: (CHANNEL_ORDER.get(name, 99), name))


async def resolve_channels(
    session: AsyncSession, user_id: uuid.UUID, event_type: str
) -> tuple[str, ...]:
    """Delivery channels (without the always-on inbox) for one event of ``event_type``."""
    switches = {
        row.channel: row.enabled
        for row in await session.scalars(
            select(NotificationChannelSettingRow).where(
                NotificationChannelSettingRow.user_id == user_id
            )
        )
    }
    cells = {
        row.channel: row.enabled
        for row in await session.scalars(
            select(NotificationRouteRow).where(
                NotificationRouteRow.user_id == user_id,
                NotificationRouteRow.event_type == event_type,
            )
        )
    }
    return tuple(
        name
        for name in _ordered(set(DEFAULT_ENABLED) | set(switches))
        if switches.get(name, name in DEFAULT_ENABLED) and cells.get(name, True)
    )


class NotificationPreferences:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        channels: ChannelRegistry,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._channels = channels
        self._clock = clock

    def known_channels(self) -> list[str]:
        return _ordered(set(self._channels.names()) | set(BUILTIN_CHANNELS))

    async def route(self, user_id: uuid.UUID, event_type: str) -> tuple[str, ...]:
        async with self._session_factory() as session:
            return await resolve_channels(session, user_id, event_type)

    async def snapshot(self, user_id: uuid.UUID) -> dict[str, Any]:
        """``notifications.schema.json#/$defs/NotificationSettings``."""
        async with self._session_factory() as session:
            switches = {
                row.channel: row
                for row in await session.scalars(
                    select(NotificationChannelSettingRow).where(
                        NotificationChannelSettingRow.user_id == user_id
                    )
                )
            }
            cells = {
                (row.event_type, row.channel): row.enabled
                for row in await session.scalars(
                    select(NotificationRouteRow).where(NotificationRouteRow.user_id == user_id)
                )
            }
        names = _ordered(set(self.known_channels()) | set(switches))
        channels: list[dict[str, Any]] = []
        for name in names:
            connection = await self._channels.connection(name, user_id)
            row = switches.get(name)
            channels.append(
                {
                    "name": name,
                    "label": self._channels.label(name),
                    "status": "connected" if connection.connected else "not_connected",
                    "detail": connection.detail,
                    "enabled": row.enabled if row is not None else name in DEFAULT_ENABLED,
                    "decided": row is not None,
                }
            )
        return {
            "inbox_always": True,
            "channels": channels,
            "event_types": [{"id": kind, "label": EVENT_LABELS[kind]} for kind in EVENT_TYPES],
            "matrix": {
                kind: {name: cells.get((kind, name), True) for name in names}
                for kind in EVENT_TYPES
            },
        }

    async def update(
        self,
        user_id: uuid.UUID,
        *,
        channels: Mapping[str, bool] | None = None,
        matrix: Mapping[str, Mapping[str, bool]] | None = None,
    ) -> dict[str, Any]:
        """Merge the given switches and cells; unknown names are rejected as a whole."""
        known = set(self.known_channels())
        for name in channels or {}:
            if name not in known:
                raise PreferencesError("notification_channel_unknown")
        for kind, row in (matrix or {}).items():
            if kind not in EVENT_TYPES:
                raise PreferencesError("notification_event_type_unknown")
            for name in row:
                if name not in known:
                    raise PreferencesError("notification_channel_unknown")
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            for name, enabled in (channels or {}).items():
                await self._put_switch(session, user_id, name, enabled, now)
            for kind, row in (matrix or {}).items():
                for name, enabled in row.items():
                    statement = insert(NotificationRouteRow).values(
                        user_id=user_id,
                        event_type=kind,
                        channel=name,
                        enabled=enabled,
                        updated_at=now,
                    )
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=["user_id", "event_type", "channel"],
                            set_={"enabled": enabled, "updated_at": now},
                        )
                    )
        return await self.snapshot(user_id)

    @staticmethod
    async def _put_switch(
        session: AsyncSession, user_id: uuid.UUID, channel: str, enabled: bool, now: datetime
    ) -> None:
        statement = insert(NotificationChannelSettingRow).values(
            user_id=user_id, channel=channel, enabled=enabled, decided_at=now
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id", "channel"],
                set_={"enabled": enabled, "decided_at": now},
            )
        )

    async def decided(self, user_id: uuid.UUID, channel: str) -> bool:
        """Did the user (or the channel's question) already choose for this channel?"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(NotificationChannelSettingRow.channel).where(
                    NotificationChannelSettingRow.user_id == user_id,
                    NotificationChannelSettingRow.channel == channel,
                )
            )
        return row is not None

    async def decide(self, user_id: uuid.UUID, channel: str, enabled: bool) -> None:
        """A channel's own question («Присылать сюда уведомления Mia?») was answered."""
        if not is_channel_name(channel):
            raise PreferencesError("notification_channel_unknown")
        async with self._session_factory() as session, session.begin():
            await self._put_switch(session, user_id, channel, enabled, self._clock())

    async def forget(self, user_id: uuid.UUID, channel: str) -> None:
        """The channel was disconnected: the next connection asks again."""
        async with self._session_factory() as session, session.begin():
            await forget_channel(session, user_id, channel)


async def forget_channel(session: AsyncSession, user_id: uuid.UUID, channel: str) -> None:
    await session.execute(
        delete(NotificationChannelSettingRow).where(
            NotificationChannelSettingRow.user_id == user_id,
            NotificationChannelSettingRow.channel == channel,
        )
    )
