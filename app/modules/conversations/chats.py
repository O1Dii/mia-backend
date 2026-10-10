"""Chats (ADR-0017): many conversations per user, each with its own model context.

A chat is a :class:`Conversation` with ``kind`` ``chat`` or ``telegram``; the runs engine
already reads a bounded window plus the summary of *one* conversation, so a chat never
sees its neighbours. Memory, plugins, jobs, inbox and approvals stay account-wide.

- The *default chat* (runs and ``GET /v1/feed`` without ``conversation_id``) is the
  user's most recently active ``chat``; with none, one is created race-free through the
  ``is_primary_feed`` partial unique index (the former single feed, M12).
- ``telegram`` is the dialog with the Mia bot (one per user); ``/new`` turns it into a
  plain chat and the next message opens a fresh one.
- ``background`` is the hidden context of job runs (never listed); their results reach the
  source chat as a card (:func:`append_card`) and the inbox.
- Retention: :class:`ChatRetention` deletes unpinned chats inactive for the user's period
  (30/60/180 days, default 60, or never) and empty chats after a day. Deleting a chat
  removes its turns and entries only; memory has no reference to conversations.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Integer,
    case,
    delete,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.screen_action import is_expired
from app.modules.conversations.models import (
    ChatKind,
    ChatSettingsRow,
    Conversation,
    DeliveryStatus,
    Entry,
    EntryKind,
    EntryRole,
    Modality,
    TitleSource,
    TurnSource,
    TurnStatus,
)
from app.modules.conversations.repositories import (
    ConversationNotFoundError,
    ConversationRepository,
    IdempotencyRepository,
)

logger = logging.getLogger(__name__)

LISTED_KINDS: Final = (ChatKind.CHAT.value, ChatKind.TELEGRAM.value)
RETAINED_KINDS: Final = (*LISTED_KINDS, ChatKind.BACKGROUND.value)
RETENTION_CHOICES: Final = (30, 60, 180)
DEFAULT_RETENTION_DAYS: Final = 60
EMPTY_CHAT_TTL: Final = timedelta(hours=24)
CONTINUE_WINDOW_S: Final = 300
TITLE_MAX: Final = 80
PREVIEW_MAX: Final = 120
TELEGRAM_TITLE: Final = "Telegram"
BACKGROUND_TITLE: Final = "Фоновые задачи"
CREATE_CHAT_ENDPOINT: Final = "POST /v1/chats"
_DIALOG_ROLES = (EntryRole.USER, EntryRole.ASSISTANT)
_TEXT_KINDS = (EntryKind.TEXT, EntryKind.TRANSCRIPT)


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else isoformat(value)


def clip(value: str, limit: int) -> str:
    """Collapse whitespace and cut at ``limit`` characters with an ellipsis."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def provisional_title(input_text: str) -> str | None:
    """Title of a fresh chat from its first message (the first line, clipped)."""
    for line in input_text.splitlines():
        if line.strip():
            return clip(line, 60)
    return None


def activity_at() -> ColumnElement[datetime]:
    return func.coalesce(Conversation.last_message_at, Conversation.created_at)


class InvalidChatCursorError(ValueError):
    """The chat list cursor is malformed."""


@dataclass(frozen=True, slots=True)
class ChatCursor:
    """Keyset position in the chat list (pinned first, then by activity, newest first)."""

    pinned: int
    activity: datetime
    chat_id: uuid.UUID

    def encode(self) -> str:
        raw = json.dumps(
            {"p": self.pinned, "t": isoformat(self.activity), "i": str(self.chat_id), "v": 1},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @classmethod
    def decode(cls, cursor: str) -> ChatCursor:
        if not cursor or len(cursor) > 512:
            raise InvalidChatCursorError("cursor length is invalid")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
            if not isinstance(payload, dict) or set(payload) != {"p", "t", "i", "v"}:
                raise ValueError("shape")
            if payload["v"] != 1 or payload["p"] not in (0, 1):
                raise ValueError("version")
            activity = datetime.fromisoformat(str(payload["t"]))
            if activity.tzinfo is None:
                raise ValueError("naive")
            return cls(int(payload["p"]), activity, uuid.UUID(str(payload["i"])))
        except (UnicodeDecodeError, binascii.Error, json.JSONDecodeError, ValueError) as error:
            raise InvalidChatCursorError("cursor is malformed") from error


@dataclass(frozen=True, slots=True)
class Chat:
    """One chat as ``Chat`` in contracts/mvp/v1/openapi.yaml."""

    id: uuid.UUID
    kind: str
    title: str | None
    title_source: str | None
    pinned_at: datetime | None
    created_at: datetime
    last_message_at: datetime | None
    last_assistant_at: datetime | None
    preview: str | None = None
    preview_role: str | None = None

    @property
    def activity_at(self) -> datetime:
        return self.last_message_at or self.created_at

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "title": self.title,
            "title_source": self.title_source,
            "pinned": self.pinned_at is not None,
            "pinned_at": _iso_or_none(self.pinned_at),
            "created_at": isoformat(self.created_at),
            "last_message_at": _iso_or_none(self.last_message_at),
            "last_assistant_at": _iso_or_none(self.last_assistant_at),
            "preview": self.preview,
            "preview_role": self.preview_role,
        }


@dataclass(frozen=True, slots=True)
class ChatPage:
    items: tuple[Chat, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ChatSettings:
    retention_days: int | None
    """30, 60 or 180; ``None`` = keep forever."""

    def to_wire(self) -> dict[str, Any]:
        return {"retention_days": self.retention_days, "choices": [*RETENTION_CHOICES, None]}


def _chat(row: Conversation, preview: tuple[str, str] | None = None) -> Chat:
    return Chat(
        id=row.id,
        kind=str(row.kind),
        title=row.title,
        title_source=row.title_source,
        pinned_at=row.pinned_at,
        created_at=row.created_at,
        last_message_at=row.last_message_at,
        last_assistant_at=row.last_assistant_at,
        preview=preview[0] if preview else None,
        preview_role=preview[1] if preview else None,
    )


# -- helpers shared with the feed and the jobs module (caller-owned sessions) ---------------


async def default_chat_id(
    session: AsyncSession, user_id: uuid.UUID, *, create: bool
) -> uuid.UUID | None:
    """The most recently active ``chat`` of the user; with none, create one if asked.

    Creation goes through the ``is_primary_feed`` partial unique index, so two concurrent
    first runs of one user end up in the same chat.
    """
    statement = (
        select(Conversation.id)
        .where(Conversation.user_id == user_id, Conversation.kind == ChatKind.CHAT.value)
        .order_by(activity_at().desc(), Conversation.id.desc())
        .limit(1)
    )
    found = await session.scalar(statement)
    if found is not None or not create:
        return found
    await session.execute(
        insert(Conversation)
        .values(id=uuid.uuid4(), user_id=user_id, is_primary_feed=True, kind=ChatKind.CHAT.value)
        .on_conflict_do_nothing(index_elements=["user_id"], index_where=text("is_primary_feed"))
    )
    found = await session.scalar(statement)
    if found is None:
        raise RuntimeError("default chat reservation was not visible")
    return found


async def singleton_chat_id(
    session: AsyncSession, user_id: uuid.UUID, kind: ChatKind, title: str
) -> uuid.UUID:
    """Get or create the user's one ``telegram`` / ``background`` conversation."""
    await session.execute(
        insert(Conversation)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            kind=kind.value,
            title=title,
            title_source=TitleSource.AUTO.value,
        )
        .on_conflict_do_nothing(
            index_elements=["user_id"], index_where=text(f"kind = '{kind.value}'")
        )
    )
    found = await session.scalar(
        select(Conversation.id).where(
            Conversation.user_id == user_id, Conversation.kind == kind.value
        )
    )
    if found is None:
        raise RuntimeError("chat reservation was not visible")
    return found


async def mark_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    at: datetime,
    assistant: bool = False,
    first_text: str | None = None,
) -> None:
    """Record activity; a chat without a title gets a provisional one from ``first_text``."""
    values: dict[str, Any] = {"last_message_at": at, "updated_at": at}
    if assistant:
        values["last_assistant_at"] = at
    await session.execute(
        update(Conversation).where(Conversation.id == conversation_id).values(**values)
    )
    title = provisional_title(first_text) if first_text else None
    if title is not None:
        await session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.kind == ChatKind.CHAT.value,
                Conversation.title.is_(None),
            )
            .values(title=title, title_source=TitleSource.AUTO.value)
        )


async def append_card(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    key: str,
    text_value: str,
    response: dict[str, Any],
    card: dict[str, Any],
    now: datetime,
) -> bool:
    """Post an assistant card (e.g. a job result) into a chat once per ``key``.

    The card's text becomes part of the chat's model history like any answer. Returns
    False when the chat is gone or the card was already posted.
    """
    found = await session.scalar(
        select(Conversation.id)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.kind.in_(LISTED_KINDS),
        )
        .with_for_update()
    )
    if found is None:
        return False
    repository = ConversationRepository(session)
    if await repository.get_turn_by_idempotency(
        user_id=user_id, conversation_id=conversation_id, idempotency_key=key
    ):
        return False
    turn = await repository.create_turn(
        user_id=user_id,
        conversation_id=conversation_id,
        source=TurnSource.RUN_JOB,
        idempotency_key=key,
    )
    turn.status = TurnStatus.COMPLETED
    turn.completed_at = now
    await repository.append_entry(
        user_id=user_id,
        conversation_id=conversation_id,
        turn_id=turn.id,
        role=EntryRole.ASSISTANT,
        kind=EntryKind.TEXT,
        modality=Modality.TEXT,
        content={"text": text_value, "response": response, "card": card},
        delivery_status=DeliveryStatus.DELIVERED,
    )
    await mark_message(session, conversation_id, at=now, assistant=True)
    return True


async def chat_kind_of(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> str | None:
    return await session.scalar(
        select(Conversation.kind).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )


# -- the chats API -------------------------------------------------------------------------


_UNSET: Any = object()


class ChatService:
    """Chat list, lifecycle and settings; every method owns one short transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock

    async def list(
        self, *, user_id: uuid.UUID, limit: int = 50, cursor: str | None = None
    ) -> ChatPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        pinned = case((Conversation.pinned_at.is_not(None), 1), else_=0).cast(Integer)
        statement = (
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.kind.in_(LISTED_KINDS))
            .order_by(pinned.desc(), activity_at().desc(), Conversation.id.desc())
            .limit(limit + 1)
        )
        if cursor is not None:
            position = ChatCursor.decode(cursor)
            statement = statement.where(
                tuple_(pinned, activity_at(), Conversation.id)
                < tuple_(
                    literal(position.pinned, Integer),
                    literal(position.activity),
                    literal(position.chat_id),
                )
            )
        async with self._sessions() as session:
            rows = list((await session.scalars(statement)).all())
            page = rows[:limit]
            previews = await self._previews(session, [row.id for row in page])
        items = tuple(_chat(row, previews.get(row.id)) for row in page)
        following = None
        if len(rows) > limit and items:
            last = items[-1]
            following = ChatCursor(
                1 if last.pinned_at is not None else 0, last.activity_at, last.id
            ).encode()
        return ChatPage(items=items, next_cursor=following)

    @staticmethod
    async def _previews(
        session: AsyncSession, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, str]]:
        if not ids:
            return {}
        rows = await session.execute(
            select(Entry.conversation_id, Entry.role, Entry.content["text"].as_string())
            .where(
                Entry.conversation_id.in_(ids),
                Entry.role.in_(_DIALOG_ROLES),
                Entry.kind.in_(_TEXT_KINDS),
            )
            .distinct(Entry.conversation_id)
            .order_by(Entry.conversation_id, Entry.sequence.desc())
        )
        return {
            conversation: (clip(value, PREVIEW_MAX), role.value)
            for conversation, role, value in rows
            if isinstance(value, str) and value.strip()
        }

    async def _owned(
        self, session: AsyncSession, user_id: uuid.UUID, chat_id: uuid.UUID, *, lock: bool = False
    ) -> Conversation:
        statement = select(Conversation).where(
            Conversation.id == chat_id,
            Conversation.user_id == user_id,
            Conversation.kind.in_(LISTED_KINDS),
        )
        if lock:
            statement = statement.with_for_update()
        row = await session.scalar(statement)
        if row is None:
            raise ConversationNotFoundError("chat was not found")
        return row

    async def get(self, *, user_id: uuid.UUID, chat_id: uuid.UUID) -> Chat:
        async with self._sessions() as session:
            row = await self._owned(session, user_id, chat_id)
            previews = await self._previews(session, [row.id])
        return _chat(row, previews.get(row.id))

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        title: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str = "",
    ) -> tuple[Chat, bool]:
        """A new empty chat; a replayed ``Idempotency-Key`` returns the same chat."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            claim = None
            if idempotency_key is not None:
                claim = await IdempotencyRepository(session).claim(
                    user_id=user_id,
                    endpoint=CREATE_CHAT_ENDPOINT,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if claim.replayed and claim.record.resource_id is not None:
                    existing = await session.scalar(
                        select(Conversation).where(
                            Conversation.id == claim.record.resource_id,
                            Conversation.user_id == user_id,
                        )
                    )
                    if existing is not None and existing.kind in LISTED_KINDS:
                        return _chat(existing), True
            row = Conversation(
                id=uuid.uuid4(),
                user_id=user_id,
                kind=ChatKind.CHAT.value,
                title=clip(title, TITLE_MAX) if title else None,
                title_source=TitleSource.USER.value if title else None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            if claim is not None:
                await IdempotencyRepository(session).bind_resource(
                    claim.record, resource_kind="chat", resource_id=row.id
                )
            return _chat(row), False

    async def update(
        self,
        *,
        user_id: uuid.UUID,
        chat_id: uuid.UUID,
        title: str | None = _UNSET,
        pinned: bool | None = None,
    ) -> Chat:
        """Rename (``title``; ``None`` returns to the automatic title) and/or pin."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            row = await self._owned(session, user_id, chat_id, lock=True)
            if title is not _UNSET:
                if title:
                    row.title = clip(title, TITLE_MAX)
                    row.title_source = TitleSource.USER.value
                elif row.kind == ChatKind.CHAT.value:
                    row.title, row.title_source = None, None
            if pinned is not None:
                if pinned and row.pinned_at is None:
                    row.pinned_at = now
                elif not pinned:
                    row.pinned_at = None
            row.updated_at = now
            await session.flush()
            previews = await self._previews(session, [row.id])
            return _chat(row, previews.get(row.id))

    async def delete(self, *, user_id: uuid.UUID, chat_id: uuid.UUID) -> None:
        """Delete the chat with its turns and entries; memory is never touched."""
        async with self._sessions() as session, session.begin():
            row = await self._owned(session, user_id, chat_id, lock=True)
            await session.execute(delete(Conversation).where(Conversation.id == row.id))

    async def continuable(
        self, *, user_id: uuid.UUID, within_s: int = CONTINUE_WINDOW_S
    ) -> Chat | None:
        """The chat whose last assistant answer is younger than ``within_s`` seconds."""
        since = self._clock() - timedelta(seconds=within_s)
        async with self._sessions() as session:
            row = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.user_id == user_id,
                    Conversation.kind == ChatKind.CHAT.value,
                    Conversation.last_assistant_at >= since,
                )
                .order_by(Conversation.last_assistant_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            previews = await self._previews(session, [row.id])
        return _chat(row, previews.get(row.id))

    async def settings(self, *, user_id: uuid.UUID) -> ChatSettings:
        async with self._sessions() as session:
            row = await session.get(ChatSettingsRow, user_id)
        if row is None:
            return ChatSettings(DEFAULT_RETENTION_DAYS)
        return ChatSettings(row.retention_days)

    async def update_settings(
        self, *, user_id: uuid.UUID, retention_days: int | None
    ) -> ChatSettings:
        if retention_days is not None and retention_days not in RETENTION_CHOICES:
            raise ValueError("retention_days must be 30, 60, 180 or null")
        now = self._clock()
        async with self._sessions() as session, session.begin():
            await session.execute(
                insert(ChatSettingsRow)
                .values(user_id=user_id, retention_days=retention_days, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={"retention_days": retention_days, "updated_at": now},
                )
            )
        return ChatSettings(retention_days)

    # -- Telegram and background conversations --------------------------------------------

    async def telegram_chat_id(self, *, user_id: uuid.UUID) -> uuid.UUID:
        async with self._sessions() as session, session.begin():
            return await singleton_chat_id(session, user_id, ChatKind.TELEGRAM, TELEGRAM_TITLE)

    async def start_new_telegram_chat(self, *, user_id: uuid.UUID) -> bool:
        """``/new``: the current Telegram dialog stays as a plain chat; False if there was none."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(Conversation)
                .where(
                    Conversation.user_id == user_id,
                    Conversation.kind == ChatKind.TELEGRAM.value,
                )
                .values(
                    kind=ChatKind.CHAT.value,
                    title=f"{TELEGRAM_TITLE} · {now.strftime('%d.%m %H:%M')}",
                    title_source=TitleSource.AUTO.value,
                    updated_at=now,
                )
            )
        return bool(getattr(result, "rowcount", 0))

    async def background_conversation_id(self, *, user_id: uuid.UUID) -> uuid.UUID:
        async with self._sessions() as session, session.begin():
            return await singleton_chat_id(session, user_id, ChatKind.BACKGROUND, BACKGROUND_TITLE)


# -- retention -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionReport:
    chats: int
    actions: int


def scrub_actions(raw: object, now: datetime) -> list[dict[str, Any]] | None:
    """Stored actions with expired URLs removed (OI-M41-R1-003); None when unchanged."""
    if not isinstance(raw, list):
        return None
    changed = False
    result: list[dict[str, Any]] = []
    for action in raw:
        if isinstance(action, dict) and action.get("url") and is_expired(action, now):
            kept = {key: action[key] for key in ("kind", "label", "expires_at") if key in action}
            result.append({**kept, "expired": True})
            changed = True
        else:
            result.append(action)
    return result if changed else None


class ChatRetention:
    """Deletes inactive unpinned chats and strips expired one-time links from answers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
        batch: int = 200,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._batch = batch

    async def sweep(self) -> RetentionReport:
        chats = 0
        while True:
            removed = await self._delete_batch()
            chats += removed
            if removed < self._batch:
                break
        actions = await self._scrub_actions()
        if chats or actions:
            logger.info("chat_retention_sweep", extra={"chats": chats, "actions": actions})
        return RetentionReport(chats=chats, actions=actions)

    async def _delete_batch(self) -> int:
        now = self._clock()
        days = func.coalesce(ChatSettingsRow.retention_days, DEFAULT_RETENTION_DAYS)
        keeps_forever = ChatSettingsRow.user_id.is_not(None) & ChatSettingsRow.retention_days.is_(
            None
        )
        expired = (
            Conversation.last_message_at.is_not(None)
            & ~keeps_forever
            & (Conversation.last_message_at < literal(now) - func.make_interval(0, 0, 0, days))
        )
        empty = Conversation.last_message_at.is_(None) & (
            Conversation.created_at < now - EMPTY_CHAT_TTL
        )
        candidates = (
            select(Conversation.id)
            .outerjoin(ChatSettingsRow, ChatSettingsRow.user_id == Conversation.user_id)
            .where(
                Conversation.kind.in_(RETAINED_KINDS),
                Conversation.pinned_at.is_(None),
                or_(expired, empty),
            )
            .limit(self._batch)
            .with_for_update(of=Conversation, skip_locked=True)
        )
        async with self._sessions() as session, session.begin():
            ids = list((await session.scalars(candidates)).all())
            if ids:
                await session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
        return len(ids)

    async def _scrub_actions(self) -> int:
        now = self._clock()
        stamp = isoformat(now)
        has_stale_url = func.jsonb_path_exists(
            Entry.content,
            text("'$.actions[*] ? (exists(@.url) && @.expires_at <= $now)'::jsonpath"),
            func.jsonb_build_object("now", stamp),
        )
        scrubbed = 0
        async with self._sessions() as session, session.begin():
            rows = (
                await session.execute(
                    select(Entry.id, Entry.content)
                    .where(Entry.role == EntryRole.ASSISTANT, has_stale_url)
                    .limit(self._batch)
                )
            ).all()
            for entry_id, content in rows:
                actions = scrub_actions(content.get("actions"), now)
                if actions is None:
                    continue
                await session.execute(
                    update(Entry)
                    .where(Entry.id == entry_id)
                    .values(content={**content, "actions": actions})
                )
                scrubbed += 1
        return scrubbed


class ChatRetentionSweeper:
    """Background loop in the app lifespan; the first sweep runs after one interval."""

    def __init__(self, retention: ChatRetention, interval_s: float) -> None:
        self._retention = retention
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._interval > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._retention.sweep()
            except Exception:
                logger.exception("chat_retention_sweep_failed")
