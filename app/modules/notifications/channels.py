"""Delivery channels for inbox items: push (FCM) built in, telegram as an extension point.

M41 registers its bot as ``notifications.channels.register(<NotificationChannel named
"telegram">)``. Without a registered channel a delivery ends ``skipped``
(``channel_not_connected``); an inbox item is always stored regardless of channels.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.models import Device
from app.modules.notifications.fcm import FcmClient, PushMessage
from app.modules.notifications.models import PushTokenRow

DeliveryStatus = Literal["sent", "skipped", "retry", "failed"]


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


class NotificationChannel(Protocol):
    name: str
    """``push`` or ``telegram``."""

    async def deliver(self, message: OutboundMessage) -> DeliveryResult: ...


class ChannelRegistry:
    def __init__(self) -> None:
        self._channels: dict[str, NotificationChannel] = {}

    def register(self, channel: NotificationChannel) -> None:
        if channel.name not in ("push", "telegram"):
            raise ValueError("unknown notification channel")
        self._channels[channel.name] = channel

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


class PushChannel:
    """Sends one FCM message per valid device token of the user."""

    name = "push"

    def __init__(self, tokens: PushTokenStore, client: FcmClient | None) -> None:
        self._tokens = tokens
        self._client = client

    @staticmethod
    def payload(message: OutboundMessage) -> PushMessage:
        """FCM ``data`` is the contract for M24 (all values are strings)."""
        return PushMessage(
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
