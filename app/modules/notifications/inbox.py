"""Assistant inbox: results of jobs, fired timers/alarms and ``notify`` messages.

Creating an item also queues its channel deliveries (push, telegram) in the same
transaction, so an item is never lost between "stored" and "sent": the delivery
loop picks pending rows up and retries them.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.notifications.models import DeliveryRow, InboxItemRow

SourceType = Literal["job", "schedule", "plugin", "system"]
Channel = Literal["push", "telegram", "inbox"]
DELIVERY_CHANNELS: tuple[str, ...] = ("push", "telegram")
DEFAULT_CHANNELS: tuple[Channel, ...] = ("inbox", "push")
AWAY_WINDOW = timedelta(days=7)


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class InvalidInboxCursorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InboxItem:
    id: uuid.UUID
    user_id: uuid.UUID
    source_type: str
    source_id: str
    title: str
    response: dict[str, Any]
    created_at: datetime
    read_at: datetime | None

    def to_wire(self) -> dict[str, Any]:
        """``job.schema.json#/$defs/InboxItem``."""
        return {
            "id": str(self.id),
            "source": {"type": self.source_type, "id": self.source_id},
            "title": self.title,
            "response": self.response,
            "created_at": isoformat(self.created_at),
            "read_at": None if self.read_at is None else isoformat(self.read_at),
        }


def _item(row: InboxItemRow) -> InboxItem:
    return InboxItem(
        id=row.id,
        user_id=row.user_id,
        source_type=row.source_type,
        source_id=row.source_id,
        title=row.title,
        response=dict(row.response),
        created_at=row.created_at,
        read_at=row.read_at,
    )


def encode_cursor(created_at: datetime, item_id: uuid.UUID) -> str:
    raw = json.dumps({"t": isoformat(created_at), "i": str(item_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        moment = datetime.fromisoformat(str(payload["t"]).replace("Z", "+00:00"))
        return moment, uuid.UUID(str(payload["i"]))
    except (binascii.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise InvalidInboxCursorError("cursor is malformed") from error


async def add_item(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    source_type: SourceType,
    source_id: str,
    title: str,
    response: dict[str, Any],
    channels: Iterable[str],
    now: datetime,
) -> InboxItem:
    """Insert an item and its pending deliveries inside the caller's transaction."""
    row = InboxItemRow(
        id=uuid.uuid4(),
        user_id=user_id,
        source_type=source_type,
        source_id=source_id[:256],
        title=title[:200],
        response=response,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    for channel in dict.fromkeys(channels):
        if channel not in DELIVERY_CHANNELS:
            continue
        session.add(
            DeliveryRow(
                id=uuid.uuid4(),
                user_id=user_id,
                inbox_item_id=row.id,
                channel=channel,
                status="pending",
                attempts=0,
                next_attempt_at=now,
                created_at=now,
            )
        )
    await session.flush()
    return _item(row)


class InboxService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
        on_created: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self.on_created = on_created
        """Wakes a local delivery loop (optional; the loop also polls)."""

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        source_type: SourceType,
        source_id: str,
        title: str,
        response: dict[str, Any],
        channels: Iterable[str] = DEFAULT_CHANNELS,
    ) -> InboxItem:
        async with self._session_factory() as session, session.begin():
            item = await add_item(
                session,
                user_id=user_id,
                source_type=source_type,
                source_id=source_id,
                title=title,
                response=response,
                channels=channels,
                now=self._clock(),
            )
        self.created()
        return item

    def created(self) -> None:
        if self.on_created is not None:
            self.on_created()

    async def list(
        self,
        *,
        user_id: uuid.UUID,
        unread: bool | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[InboxItem], str | None]:
        statement = select(InboxItemRow).where(InboxItemRow.user_id == user_id)
        if unread is True:
            statement = statement.where(InboxItemRow.read_at.is_(None))
        elif unread is False:
            statement = statement.where(InboxItemRow.read_at.is_not(None))
        if cursor is not None:
            moment, last_id = decode_cursor(cursor)
            statement = statement.where(
                or_(
                    InboxItemRow.created_at < moment,
                    and_(InboxItemRow.created_at == moment, InboxItemRow.id < last_id),
                )
            )
        statement = statement.order_by(
            InboxItemRow.created_at.desc(), InboxItemRow.id.desc()
        ).limit(limit + 1)
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        items = [_item(row) for row in rows[:limit]]
        following = (
            encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > limit and items
            else None
        )
        return items, following

    async def mark_read(self, *, user_id: uuid.UUID, item_id: uuid.UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(InboxItemRow).where(
                    InboxItemRow.id == item_id, InboxItemRow.user_id == user_id
                )
            )
            if row is None:
                return False
            if row.read_at is None:
                row.read_at = self._clock()
            return True

    async def count_since(self, *, user_id: uuid.UUID, since: datetime) -> int:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(InboxItemRow)
                .where(InboxItemRow.user_id == user_id, InboxItemRow.created_at >= since)
            )
        return int(value or 0)

    async def take_unannounced(
        self, *, user_id: uuid.UUID, limit: int = 3
    ) -> tuple[Sequence[InboxItem], int]:
        """Unread items not yet mentioned to the user (last 7 days); marks them announced.

        Returns up to ``limit`` newest items and the total number that was pending.
        """
        now = self._clock()
        async with self._session_factory() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(InboxItemRow)
                        .where(
                            InboxItemRow.user_id == user_id,
                            InboxItemRow.announced_at.is_(None),
                            InboxItemRow.read_at.is_(None),
                            InboxItemRow.created_at >= now - AWAY_WINDOW,
                        )
                        .order_by(InboxItemRow.created_at.desc())
                        .limit(50)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not rows:
                return (), 0
            await session.execute(
                update(InboxItemRow)
                .where(InboxItemRow.id.in_([row.id for row in rows]))
                .values(announced_at=now)
            )
        return [_item(row) for row in rows[:limit]], len(rows)
