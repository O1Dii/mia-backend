"""Delivery loop: claim pending channel deliveries (``SKIP LOCKED``), send, retry with backoff.

A claimed row gets ``next_attempt_at = now + lease``; a worker that dies mid-send leaves
it to be retried after the lease (at-least-once to the channel).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.notifications.channels import (
    ChannelRegistry,
    DeliveryResult,
    OutboundMessage,
)
from app.modules.notifications.models import DeliveryRow, InboxItemRow

logger = logging.getLogger(__name__)

CLAIM_LEASE = timedelta(seconds=60)
BACKOFF_BASE_S = 15.0
BACKOFF_MAX_S = 3600.0


def utc_now() -> datetime:
    return datetime.now(UTC)


class DeliveryDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        channels: ChannelRegistry,
        *,
        max_attempts: int = 5,
        clock: Callable[[], datetime] = utc_now,
        batch: int = 20,
    ) -> None:
        self._session_factory = session_factory
        self.channels = channels
        self._max_attempts = max_attempts
        self._clock = clock
        self._batch = batch

    async def claim(self) -> list[OutboundMessage]:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    select(DeliveryRow, InboxItemRow)
                    .join(InboxItemRow, InboxItemRow.id == DeliveryRow.inbox_item_id)
                    .where(DeliveryRow.status == "pending", DeliveryRow.next_attempt_at <= now)
                    .order_by(DeliveryRow.next_attempt_at)
                    .limit(self._batch)
                    .with_for_update(of=DeliveryRow, skip_locked=True)
                )
            ).all()
            messages: list[OutboundMessage] = []
            for delivery, item in rows:
                delivery.attempts += 1
                delivery.next_attempt_at = now + CLAIM_LEASE
                messages.append(
                    OutboundMessage(
                        delivery_id=delivery.id,
                        user_id=delivery.user_id,
                        inbox_item_id=item.id,
                        source_type=item.source_type,
                        source_id=item.source_id,
                        title=item.title,
                        text=str(item.response.get("speech") or item.title),
                        response=dict(item.response),
                        attempt=delivery.attempts,
                        channel=delivery.channel,
                    )
                )
        return messages

    async def run_once(self) -> int:
        """Send every due delivery once; returns how many were attempted."""
        messages = await self.claim()
        if not messages:
            return 0
        await asyncio.gather(*(self._deliver(message) for message in messages))
        return len(messages)

    async def _deliver(self, message: OutboundMessage) -> None:
        channel_name = message.channel
        channel = self.channels.get(channel_name)
        if channel is None:
            result = DeliveryResult("skipped", "channel_not_connected")
        else:
            try:
                result = await channel.deliver(message)
            except Exception:
                logger.exception("notification_channel_failed", extra={"channel": channel_name})
                result = DeliveryResult("retry", "channel_error")
        await self._finish(message, result)

    async def _finish(self, message: OutboundMessage, result: DeliveryResult) -> None:
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            row = await session.get(DeliveryRow, message.delivery_id, with_for_update=True)
            if row is None or row.status != "pending":
                return
            row.last_error = result.error_code
            if result.status == "retry" and row.attempts < self._max_attempts:
                delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (row.attempts - 1))
                row.next_attempt_at = now + timedelta(seconds=delay)
                return
            row.status = "failed" if result.status == "retry" else result.status
            row.finished_at = now
        if result.status != "sent":
            logger.info(
                "notification_not_delivered",
                extra={"status": result.status, "error_code": result.error_code},
            )
