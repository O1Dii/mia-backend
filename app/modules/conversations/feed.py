"""The user's continuous feed with an episodic summary of its older part (Review §5.2(f)).

One primary conversation per user collects voice, chat and job results. Runs
(M12) read a bounded window of recent entries plus ``Conversation.summary``
and append the user's input and the final answer as ordinary entries, so the
existing conversation/entries API keeps working on the same data.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

FEED_TITLE = "Mia"
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
    ) -> None:
        if recent_entries < 1 or summary_batch < 1:
            raise ValueError("feed windows must be positive")
        self._session_factory = session_factory
        self._recent = recent_entries
        self._batch = summary_batch

    async def owns_conversation(self, *, user_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            found = await ConversationRepository(session).get_owned(
                user_id=user_id, conversation_id=conversation_id
            )
            return found is not None

    async def primary_feed_id(self, *, user_id: uuid.UUID) -> uuid.UUID:
        async with self._session_factory() as session, session.begin():
            return await self._primary_feed_id(session, user_id)

    async def _primary_feed_id(self, session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
        await session.execute(
            insert(Conversation)
            .values(id=uuid.uuid4(), user_id=user_id, title=FEED_TITLE, is_primary_feed=True)
            .on_conflict_do_nothing(index_elements=["user_id"], index_where=text("is_primary_feed"))
        )
        feed_id = await session.scalar(
            select(Conversation.id).where(
                Conversation.user_id == user_id, Conversation.is_primary_feed.is_(True)
            )
        )
        if feed_id is None:
            raise RuntimeError("primary feed reservation was not visible")
        return feed_id

    async def context(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID | None = None
    ) -> FeedContext:
        """Recent dialog plus the episodic summary; ``None`` selects the primary feed."""
        async with self._session_factory() as session, session.begin():
            target = conversation_id or await self._primary_feed_id(session, user_id)
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
            await self._touch(session, conversation_id)
            return FeedTurn(conversation_id=conversation_id, turn_id=turn.id)

    async def record_answer(
        self,
        *,
        user_id: uuid.UUID,
        turn: FeedTurn,
        text_value: str,
        response: dict[str, Any],
        modality: Modality = Modality.TEXT,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await ConversationRepository(session).append_entry(
                user_id=user_id,
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                role=EntryRole.ASSISTANT,
                kind=EntryKind.TEXT,
                modality=modality,
                content={"text": text_value, "response": response},
                delivery_status=DeliveryStatus.DELIVERED,
            )
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
