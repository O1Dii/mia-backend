"""Delivery channels for inbox items: push (FCM) built in, the Telegram bot and plugin channels.

M41 registers its bot as ``notifications.channels.register(<NotificationChannel named
"telegram">)``; a plugin channel (``provides.notificationChannel`` in the manifest, M77) registers
the same way under its own name. Without a registered channel a delivery ends ``skipped``
(``channel_not_connected``); an inbox item is always stored regardless of channels.

A channel may also expose ``label`` and ``async connection(user_id)``: what the settings page shows
(«подключён / не подключён») and why a delivery would be skipped.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.models import Device
from app.modules.notifications.fcm import FcmClient, PushMessage
from app.modules.notifications.models import PushTokenRow

DeliveryStatus = Literal["sent", "skipped", "retry", "failed"]

CHANNEL_NAME = re.compile(r"^[a-z][a-z0-9_.:-]{0,47}$")
BUILTIN_CHANNELS: dict[str, str] = {"push": "Push на телефон", "telegram": "Telegram-бот"}
"""Always listed in the settings, connected or not."""


def is_channel_name(name: str) -> bool:
    """A delivery channel name; ``inbox`` is the always-on list, not a channel."""
    return name != "inbox" and CHANNEL_NAME.fullmatch(name) is not None


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    delivery_id: uuid.UUID
    user_id: uuid.UUID
    inbox_item_id: uuid.UUID
    source_type: str
    source_id: str
    title: str
    text: str
    """``response.speech``: one plain sentence or two."""
    response: dict[str, Any]
    attempt: int
    channel: str = "push"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: DeliveryStatus
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelConnection:
    """Whether the channel can reach this user now; ``detail`` is a stable machine reason."""

    connected: bool
    detail: str | None = None


class NotificationChannel(Protocol):
    name: str
    """``push``, ``telegram`` or a plugin channel (``[a-z][a-z0-9_.:-]*``)."""

    async def deliver(self, message: OutboundMessage) -> DeliveryResult: ...


class ChannelRegistry:
    def __init__(self) -> None:
        self._channels: dict[str, NotificationChannel] = {}

    def register(self, channel: NotificationChannel) -> None:
        if not is_channel_name(channel.name):
            raise ValueError("invalid notification channel name")
        self._channels[channel.name] = channel

    def label(self, name: str) -> str:
        channel = self._channels.get(name)
        label = getattr(channel, "label", None)
        if isinstance(label, str) and label:
            return label
        return BUILTIN_CHANNELS.get(name, name)

    async def connection(self, name: str, user_id: uuid.UUID) -> ChannelConnection:
        channel = self._channels.get(name)
        if channel is None:
            return ChannelConnection(False, "not_configured")
        probe = getattr(channel, "connection", None)
        if probe is None:
            return ChannelConnection(True)
        try:
            result = await probe(user_id)
        except Exception:
            return ChannelConnection(False, "unavailable")
        return result if isinstance(result, ChannelConnection) else ChannelConnection(True)

    def get(self, name: str) -> NotificationChannel | None:
        return self._channels.get(name)

    def names(self) -> list[str]:
        return sorted(self._channels)


class DeviceNotFoundError(LookupError):
    pass


class PushTokenStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def put(
        self, *, user_id: uuid.UUID, device_id: uuid.UUID, provider: str, token: str
    ) -> None:
        """Store the device's token; the same token on another device is moved here."""
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            device = await session.scalar(
                select(Device.id).where(Device.id == device_id, Device.user_id == user_id)
            )
            if device is None:
                raise DeviceNotFoundError("device was not found")
            await session.execute(
                update(PushTokenRow)
                .where(
                    PushTokenRow.provider == provider,
                    PushTokenRow.token == token,
                    PushTokenRow.device_id != device_id,
                )
                .values(invalidated_at=now, updated_at=now)
            )
            statement = insert(PushTokenRow).values(
                id=uuid.uuid4(),
                user_id=user_id,
                device_id=device_id,
                provider=provider,
                token=token,
                created_at=now,
                updated_at=now,
                invalidated_at=None,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_push_tokens_device_provider",
                    set_={
                        "token": statement.excluded.token,
                        "user_id": statement.excluded.user_id,
                        "updated_at": now,
                        "invalidated_at": None,
                    },
                )
            )

    async def active(self, user_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
        """``(token_row_id, token)`` of the user's valid tokens."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(PushTokenRow.id, PushTokenRow.token).where(
                        PushTokenRow.user_id == user_id,
                        PushTokenRow.provider == "fcm",
                        PushTokenRow.invalidated_at.is_(None),
                    )
                )
            ).all()
        return [(row[0], row[1]) for row in rows]

    async def invalidate(self, token_id: uuid.UUID) -> None:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(PushTokenRow)
                .where(PushTokenRow.id == token_id)
                .values(invalidated_at=now, updated_at=now)
            )


_RINGING_SOURCES = ("builtin.alarm:", "builtin.timer:")
_RING_TTL_SECONDS = 600


class PushChannel:
    """Sends one FCM message per valid device token of the user."""

    name = "push"
    label = BUILTIN_CHANNELS["push"]

    def __init__(self, tokens: PushTokenStore, client: FcmClient | None) -> None:
        self._tokens = tokens
        self._client = client

    async def connection(self, user_id: uuid.UUID) -> ChannelConnection:
        if self._client is None:
            return ChannelConnection(False, "push_not_configured")
        if not await self._tokens.active(user_id):
            return ChannelConnection(False, "no_push_token")
        return ChannelConnection(True)

    @staticmethod
    def payload(message: OutboundMessage) -> PushMessage:
        """FCM ``data`` is the contract for M24 (all values are strings).

        A fired cloud alarm or timer is data-only with a short TTL (OI-M71-001): the app
        rings it full screen itself, and a push that arrives late must not ring at all.
        """
        rings = message.source_type == "plugin" and message.source_id.startswith(_RINGING_SOURCES)
        push = PushMessage(
            title=message.title,
            body=message.text,
            data={
                "type": "inbox_item",
                "inbox_item_id": str(message.inbox_item_id),
                "source_type": message.source_type,
                "source_id": message.source_id,
                "title": message.title,
                "speech": message.text,
            },
            collapse_key=str(message.inbox_item_id),
        )
        if rings:
            return replace(push, data_only=True, ttl_seconds=_RING_TTL_SECONDS)
        return push

    async def deliver(self, message: OutboundMessage) -> DeliveryResult:
        if self._client is None:
            return DeliveryResult("skipped", "push_not_configured")
        tokens = await self._tokens.active(message.user_id)
        if not tokens:
            return DeliveryResult("skipped", "no_push_token")
        payload = self.payload(message)
        sent = 0
        retry_error: str | None = None
        last_error: str | None = None
        for token_id, token in tokens:
            result = await self._client.send(token, payload)
            if result.outcome == "sent":
                sent += 1
                continue
            if result.outcome == "unregistered":
                await self._tokens.invalidate(token_id)
            elif result.outcome in ("retry", "auth_failed"):
                retry_error = result.error_code
            last_error = result.error_code
        if sent:
            return DeliveryResult("sent")
        if retry_error is not None:
            return DeliveryResult("retry", retry_error)
        return DeliveryResult("failed", last_error or "push_rejected")
