"""Chat feeds with an episodic summary of their older part (M12/M18, ADR-0017).

Every chat (``chats.py``) is one conversation with its own history and summary. Runs
(M12) read a bounded window of recent entries plus ``Conversation.summary`` of *their*
chat and append the user's input and the final answer as ordinary entries. Without an
explicit conversation, runs and ``GET /v1/feed`` use the default chat (the most recently
active one), which keeps single-feed clients working.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.screen_action import ACTION_KINDS, LABEL_MAX, listed_action
from app.modules.conversations.chats import LISTED_KINDS, default_chat_id, mark_message
from app.modules.conversations.models import (
    Conversation,
    DeliveryStatus,
    Entry,
    EntryKind,
    EntryRole,
    Modality,
    Turn,
    TurnSource,
    TurnStatus,
)
from app.modules.conversations.repositories import (
    ConversationNotFoundError,
    ConversationRepository,
)
from app.modules.users.models import User

SUMMARY_MAX_CHARS = 4000
_TEXT_KINDS = (EntryKind.TEXT, EntryKind.TRANSCRIPT)
_DIALOG_ROLES = (EntryRole.USER, EntryRole.ASSISTANT)


@dataclass(frozen=True, slots=True)
class FeedMessage:
    role: Literal["user", "assistant"]
    text: str
    sequence: int


@dataclass(frozen=True, slots=True)
class FeedContext:
    conversation_id: uuid.UUID
    summary: str | None
    messages: tuple[FeedMessage, ...]
    locale: str
    timezone: str


@dataclass(frozen=True, slots=True)
class FeedTurn:
    conversation_id: uuid.UUID
    turn_id: uuid.UUID


class InvalidFeedCursorError(ValueError):
    """The ``before`` cursor is malformed or does not belong to the caller's feed."""


@dataclass(frozen=True, slots=True)
class FeedCursor:
    """Opaque keyset position: entries strictly older than ``sequence`` of one feed.

    Bound to the feed conversation, so a cursor of another user's feed never decodes
    for the caller (and a cursor can never widen what the caller may read).
    """

    conversation_id: uuid.UUID
    sequence: int

    def encode(self) -> str:
        raw = json.dumps(
            {"c": str(self.conversation_id), "s": self.sequence, "v": 1},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @classmethod
    def decode(cls, cursor: str, *, conversation_id: uuid.UUID) -> FeedCursor:
        if not cursor or len(cursor) > 512:
            raise InvalidFeedCursorError("cursor length is invalid")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        except (UnicodeDecodeError, binascii.Error, json.JSONDecodeError, ValueError) as error:
            raise InvalidFeedCursorError("cursor is malformed") from error
        if not isinstance(payload, dict) or set(payload) != {"c", "s", "v"} or payload["v"] != 1:
            raise InvalidFeedCursorError("cursor shape is invalid")
        sequence = payload["s"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise InvalidFeedCursorError("cursor sequence is invalid")
        if payload["c"] != str(conversation_id):
            raise InvalidFeedCursorError("cursor belongs to another feed")
        return cls(conversation_id=conversation_id, sequence=sequence)


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One message of the user's feed (``FeedItem`` in openapi.yaml)."""

    id: uuid.UUID
    role: Literal["user", "assistant"]
    source: str
    text: str
    display: dict[str, Any] | None
    run_id: uuid.UUID | None
    created_at: datetime
    sequence: int
    actions: tuple[dict[str, Any], ...] = ()
    """Screen-only actions of the answer (M41-R1); expired ones come without ``url``."""
    card: dict[str, Any] | None = None
    """A result card posted into the chat (M74), e.g. ``{"type": "job_result", ...}``."""


@dataclass(frozen=True, slots=True)
class FeedPage:
    items: tuple[FeedItem, ...]
    next_cursor: str | None
    conversation_id: uuid.UUID | None = None
    """The chat the page belongs to (the default chat when none was asked for)."""


def _run_id_of(idempotency_key: str) -> uuid.UUID | None:
    prefix, _, value = idempotency_key.partition(":")
    if prefix != "run":
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _stored_action(raw: object, now: datetime) -> dict[str, Any] | None:
    """A stored action as listed; one whose URL retention already removed stays disabled."""
    if isinstance(raw, dict) and raw.get("expired") is True and "url" not in raw:
        kind, label = raw.get("kind"), raw.get("label")
        if kind not in ACTION_KINDS or not isinstance(label, str) or not label.strip():
            return None
        action: dict[str, Any] = {"kind": kind, "label": label.strip()[:LABEL_MAX]}
        if isinstance(raw.get("expires_at"), str):
            action["expires_at"] = raw["expires_at"]
        action["expired"] = True
        return action
    return listed_action(raw, now)


def _actions_of(entry: Entry, now: datetime) -> tuple[dict[str, Any], ...]:
    raw = entry.content.get("actions")
    if entry.role != EntryRole.ASSISTANT or not isinstance(raw, list):
        return ()
    listed = (_stored_action(item, now) for item in raw)
    return tuple(action for action in listed if action is not None)


def _card_of(entry: Entry) -> dict[str, Any] | None:
    card = entry.content.get("card")
    if entry.role != EntryRole.ASSISTANT or not isinstance(card, dict):
        return None
    return dict(card)


def _display_of(entry: Entry) -> dict[str, Any] | None:
    response = entry.content.get("response")
    display = response.get("display") if isinstance(response, dict) else None
    return display if isinstance(display, dict) else None


class FeedSummarizer(Protocol):
    async def summarize(self, *, previous: str | None, messages: Sequence[FeedMessage]) -> str:
        """Return a compact summary of ``previous`` plus ``messages``."""
        ...


class ExtractiveFeedSummarizer:
    """Deterministic fallback: keeps the previous summary and clipped dialog lines."""

    def __init__(self, *, line_chars: int = 160, max_chars: int = SUMMARY_MAX_CHARS) -> None:
        self._line_chars = line_chars
        self._max_chars = max_chars

    async def summarize(self, *, previous: str | None, messages: Sequence[FeedMessage]) -> str:
        lines = [previous.strip()] if previous and previous.strip() else []
        for message in messages:
            clipped = " ".join(message.text.split())
            if len(clipped) > self._line_chars:
                clipped = clipped[: self._line_chars - 1].rstrip() + "…"
            prefix = "Пользователь" if message.role == "user" else "Mia"
            lines.append(f"{prefix}: {clipped}")
        summary = "\n".join(lines)
        if len(summary) > self._max_chars:
            summary = "…" + summary[-(self._max_chars - 1) :]
        return summary


def _entry_text(entry: Entry) -> str | None:
    value = entry.content.get("text")
    return value if isinstance(value, str) and value.strip() else None


def _message(entry: Entry) -> FeedMessage | None:
    text_value = _entry_text(entry)
    if text_value is None:
        return None
    role: Literal["user", "assistant"] = "user" if entry.role == EntryRole.USER else "assistant"
    return FeedMessage(role=role, text=text_value, sequence=entry.sequence)


class FeedService:
    """Feed reads and writes; every method owns one short transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        recent_entries: int = 20,
        summary_batch: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if recent_entries < 1 or summary_batch < 1:
            raise ValueError("feed windows must be positive")
        self._session_factory = session_factory
        self._recent = recent_entries
        self._batch = summary_batch
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    async def owns_conversation(self, *, user_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            found = await ConversationRepository(session).get_owned(
                user_id=user_id, conversation_id=conversation_id
            )
            return found is not None

    async def primary_feed_id(self, *, user_id: uuid.UUID) -> uuid.UUID:
        """The default chat (most recently active), created when the user has none."""
        async with self._session_factory() as session, session.begin():
            return await self._default_chat_id(session, user_id)

    @staticmethod
    async def _default_chat_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
        found = await default_chat_id(session, user_id, create=True)
        if found is None:
            raise RuntimeError("default chat was not created")
        return found

    async def context(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID | None = None
    ) -> FeedContext:
        """Recent dialog plus the episodic summary of one chat; ``None`` = the default chat."""
        async with self._session_factory() as session, session.begin():
            target = conversation_id or await self._default_chat_id(session, user_id)
            conversation = await ConversationRepository(session).get_owned(
                user_id=user_id, conversation_id=target
            )
            if conversation is None:
                raise ConversationNotFoundError("conversation was not found")
            rows = (
                await session.scalars(
                    select(Entry)
                    .where(
                        Entry.conversation_id == target,
                        Entry.sequence > func.coalesce(conversation.summary_until_sequence, 0),
                        Entry.role.in_(_DIALOG_ROLES),
                        Entry.kind.in_(_TEXT_KINDS),
                    )
                    .order_by(Entry.sequence.desc())
                    .limit(self._recent)
                )
            ).all()
            profile = await session.execute(
                select(User.locale, User.timezone).where(User.id == user_id)
            )
            locale, timezone = profile.one_or_none() or ("en", "UTC")
        messages = tuple(
            message for message in (_message(row) for row in reversed(rows)) if message is not None
        )
        return FeedContext(
            conversation_id=target,
            summary=conversation.summary,
            messages=messages,
            locale=locale,
            timezone=timezone,
        )

    async def open_turn(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        source: TurnSource,
        input_text: str,
        modality: Modality = Modality.TEXT,
    ) -> FeedTurn:
        """Create the run's turn and the user's entry; idempotent per ``run_id``."""
        key = f"run:{run_id}"
        async with self._session_factory() as session, session.begin():
            # Take the feed row lock before inserting the turn: the turn's foreign key holds a
            # shared row lock, and two concurrent runs of one user would otherwise each hold
            # it while waiting for the exclusive lock in ``append_entry`` (deadlock, M18).
            await session.execute(
                select(Conversation.id)
                .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
                .with_for_update()
            )
            repository = ConversationRepository(session)
            existing = await repository.get_turn_by_idempotency(
                user_id=user_id, conversation_id=conversation_id, idempotency_key=key
            )
            if existing is not None:
                return FeedTurn(conversation_id=conversation_id, turn_id=existing.id)
            turn = await repository.create_turn(
                user_id=user_id,
                conversation_id=conversation_id,
                source=source,
                idempotency_key=key,
            )
            turn.status = TurnStatus.ACTIVE
            await repository.append_entry(
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn.id,
                role=EntryRole.USER,
                kind=EntryKind.TRANSCRIPT if modality == Modality.AUDIO else EntryKind.TEXT,
                modality=modality,
                content={"text": input_text},
                delivery_status=DeliveryStatus.DELIVERED,
            )
            await mark_message(session, conversation_id, at=self._clock(), first_text=input_text)
            return FeedTurn(conversation_id=conversation_id, turn_id=turn.id)

    async def record_answer(
        self,
        *,
        user_id: uuid.UUID,
        turn: FeedTurn,
        text_value: str,
        response: dict[str, Any],
        modality: Modality = Modality.TEXT,
        actions: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Store the final answer; ``actions`` (screen-only, M41-R1) ride along for the UI.

        The model's history (:meth:`context`) reads only ``content["text"]``.
        """
        content: dict[str, Any] = {"text": text_value, "response": response}
        if actions:
            content["actions"] = [dict(action) for action in actions]
        async with self._session_factory() as session, session.begin():
            await ConversationRepository(session).append_entry(
                user_id=user_id,
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                role=EntryRole.ASSISTANT,
                kind=EntryKind.TEXT,
                modality=modality,
                content=content,
                delivery_status=DeliveryStatus.DELIVERED,
            )
            await mark_message(session, turn.conversation_id, at=self._clock(), assistant=True)
            await self._finish_turn(session, turn, TurnStatus.COMPLETED)

    async def close_turn(self, *, turn: FeedTurn, status: TurnStatus) -> None:
        async with self._session_factory() as session, session.begin():
            await self._finish_turn(session, turn, status)

    async def summarize_if_needed(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID, summarizer: FeedSummarizer
    ) -> bool:
        """Fold everything except the newest ``recent_entries`` into the summary once at
        least ``summary_batch`` extra entries accumulated. Optimistic: a concurrent
        summarization wins and this one is discarded."""
        async with self._session_factory() as session:
            conversation = await ConversationRepository(session).get_owned(
                user_id=user_id, conversation_id=conversation_id
            )
            if conversation is None:
                return False
            covered = conversation.summary_until_sequence
            previous = conversation.summary
            rows = (
                await session.scalars(
                    select(Entry)
                    .where(
                        Entry.conversation_id == conversation_id,
                        Entry.sequence > func.coalesce(covered, 0),
                        Entry.role.in_(_DIALOG_ROLES),
                        Entry.kind.in_(_TEXT_KINDS),
                    )
                    .order_by(Entry.sequence)
                )
            ).all()
        if len(rows) < self._recent + self._batch:
            return False
        older = rows[: len(rows) - self._recent]
        messages = [message for message in (_message(row) for row in older) if message]
        summary = (await summarizer.summarize(previous=previous, messages=messages)).strip()
        if not summary:
            return False
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                    Conversation.summary_until_sequence.is_not_distinct_from(covered),
                )
                .values(
                    summary=summary[:SUMMARY_MAX_CHARS],
                    summary_until_sequence=older[-1].sequence,
                )
            )
            return bool(getattr(result, "rowcount", 0))

    async def history(
        self,
        *,
        user_id: uuid.UUID,
        before: str | None = None,
        limit: int = 50,
        conversation_id: uuid.UUID | None = None,
    ) -> FeedPage:
        """A page of one chat, newest first, older than the ``before`` cursor.

        ``conversation_id`` selects the chat (``ConversationNotFoundError`` for a foreign,
        hidden or missing one); ``None`` reads the default chat. Keyset on
        ``entries.sequence`` (allocated under the conversation row lock, so it is committed
        in order): entries written meanwhile are newer than every cursor already handed out,
        hence pages never skip or repeat an entry. Read-only: a user without chats gets an
        empty page and any cursor is rejected.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._session_factory() as session:
            if conversation_id is not None:
                feed_id = await session.scalar(
                    select(Conversation.id).where(
                        Conversation.id == conversation_id,
                        Conversation.user_id == user_id,
                        Conversation.kind.in_(LISTED_KINDS),
                    )
                )
                if feed_id is None:
                    raise ConversationNotFoundError("conversation was not found")
            else:
                feed_id = await default_chat_id(session, user_id, create=False)
            if feed_id is None:
                if before is not None:
                    raise InvalidFeedCursorError("cursor belongs to another feed")
                return FeedPage(items=(), next_cursor=None)
            statement = (
                select(Entry, Turn.source, Turn.idempotency_key)
                .join(
                    Turn,
                    (Turn.id == Entry.turn_id) & (Turn.conversation_id == Entry.conversation_id),
                )
                .where(
                    Entry.conversation_id == feed_id,
                    Entry.role.in_(_DIALOG_ROLES),
                    Entry.kind.in_(_TEXT_KINDS),
                    func.btrim(Entry.content["text"].as_string()) != "",
                )
                .order_by(Entry.sequence.desc())
                .limit(limit + 1)
            )
            if before is not None:
                position = FeedCursor.decode(before, conversation_id=feed_id)
                statement = statement.where(Entry.sequence < position.sequence)
            rows = (await session.execute(statement)).all()
        page = rows[:limit]
        now = self._clock()
        items = tuple(
            FeedItem(
                id=entry.id,
                role="user" if entry.role == EntryRole.USER else "assistant",
                source=source.value,
                text=str(entry.content["text"]),
                display=_display_of(entry),
                run_id=_run_id_of(key),
                created_at=entry.created_at,
                sequence=entry.sequence,
                actions=_actions_of(entry, now),
                card=_card_of(entry),
            )
            for entry, source, key in page
        )
        next_cursor = (
            FeedCursor(feed_id, items[-1].sequence).encode() if len(rows) > limit else None
        )
        return FeedPage(items=items, next_cursor=next_cursor, conversation_id=feed_id)

    @staticmethod
    async def _touch(session: AsyncSession, conversation_id: uuid.UUID) -> None:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(updated_at=datetime.now(UTC))
        )

    async def _finish_turn(self, session: AsyncSession, turn: FeedTurn, status: TurnStatus) -> None:
        await session.execute(
            update(Turn)
            .where(Turn.id == turn.turn_id, Turn.conversation_id == turn.conversation_id)
            .values(status=status, completed_at=datetime.now(UTC))
        )
        await self._touch(session, turn.conversation_id)
