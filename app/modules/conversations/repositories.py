"""Ownership-safe conversation repositories with caller-owned transactions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.conversations.cursors import ConversationCursor, EntryCursor
from app.modules.conversations.models import (
    Conversation,
    DeliveryStatus,
    Entry,
    EntryKind,
    EntryRole,
    IdempotencyRecord,
    Modality,
    Turn,
    TurnSource,
)


class ConversationNotFoundError(LookupError):
    """Raised for missing and foreign conversations without revealing ownership."""


class TurnNotFoundError(LookupError):
    """Raised for a turn outside the authenticated user's conversation."""


class IdempotencyConflictError(ValueError):
    """Raised when a key is reused for a different canonical request."""


@dataclass(frozen=True, slots=True)
class ConversationPage:
    items: tuple[Conversation, ...]
    next_cursor: ConversationCursor | None


@dataclass(frozen=True, slots=True)
class EntryPage:
    items: tuple[Entry, ...]
    next_cursor: EntryCursor | None


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    record: IdempotencyRecord
    replayed: bool


class IdempotencyRepository:
    """Atomically reserve mutation keys scoped by authenticated user and endpoint."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        *,
        user_id: uuid.UUID,
        endpoint: str,
        idempotency_key: str,
        request_hash: str,
    ) -> IdempotencyClaim:
        statement = (
            insert(IdempotencyRecord)
            .values(
                user_id=user_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_user_endpoint_key")
            .returning(IdempotencyRecord.id)
        )
        inserted_id = await self._session.scalar(statement)
        record = await self._session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == user_id,
                IdempotencyRecord.endpoint == endpoint,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if record is None:
            raise RuntimeError("idempotency reservation was not visible")
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(
                "idempotency key was already used for a different request"
            )
        return IdempotencyClaim(record=record, replayed=inserted_id is None)

    async def bind_resource(
        self,
        record: IdempotencyRecord,
        *,
        resource_kind: str,
        resource_id: uuid.UUID,
    ) -> None:
        record.resource_kind = resource_kind
        record.resource_id = resource_id
        await self._session.flush()


class ConversationRepository:
    """Queries always bind conversation identity and authenticated ownership."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

    def _owned_statement(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        for_update: bool = False,
    ) -> Select[tuple[Conversation]]:
        statement = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
        return statement.with_for_update() if for_update else statement

    async def create(self, *, user_id: uuid.UUID, title: str | None = None) -> Conversation:
        conversation = Conversation(user_id=user_id, title=title)
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def get_owned(
        self, *, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation | None:
        result = await self._session.scalars(
            self._owned_statement(user_id=user_id, conversation_id=conversation_id)
        )
        return result.one_or_none()

    async def update_owned(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        title: str | None,
        title_provided: bool,
        archived: bool | None,
    ) -> Conversation:
        conversation = await self._session.scalar(
            self._owned_statement(
                user_id=user_id,
                conversation_id=conversation_id,
                for_update=True,
            )
        )
        if conversation is None:
            raise ConversationNotFoundError("conversation was not found")
        if title_provided:
            conversation.title = title
        now = datetime.now(UTC)
        if archived is not None:
            conversation.archived_at = now if archived else None
        conversation.updated_at = now
        await self._session.flush()
        return conversation

    async def delete_owned(self, *, user_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        deleted_id = await self._session.scalar(
            delete(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
            .returning(Conversation.id)
        )
        if deleted_id is None:
            raise ConversationNotFoundError("conversation was not found")

    async def list_owned(
        self,
        *,
        user_id: uuid.UUID,
        limit: int = 50,
        cursor: ConversationCursor | None = None,
        include_archived: bool = False,
    ) -> ConversationPage:
        self._validate_limit(limit)
        statement = select(Conversation).where(Conversation.user_id == user_id)
        if not include_archived:
            statement = statement.where(Conversation.archived_at.is_(None))
        if cursor is not None:
            statement = statement.where(
                or_(
                    Conversation.updated_at < cursor.updated_at,
                    and_(
                        Conversation.updated_at == cursor.updated_at,
                        Conversation.id < cursor.conversation_id,
                    ),
                )
            )
        statement = statement.order_by(
            Conversation.updated_at.desc(), Conversation.id.desc()
        ).limit(limit + 1)
        rows = tuple((await self._session.scalars(statement)).all())
        items = rows[:limit]
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = ConversationCursor(updated_at=last.updated_at, conversation_id=last.id)
        return ConversationPage(items=items, next_cursor=next_cursor)

    async def create_turn(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        source: TurnSource,
        idempotency_key: str,
    ) -> Turn:
        owned_id = await self._session.scalar(
            select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        if owned_id is None:
            raise ConversationNotFoundError("conversation was not found")
        turn = Turn(
            conversation_id=conversation_id,
            source=source,
            idempotency_key=idempotency_key,
        )
        self._session.add(turn)
        await self._session.flush()
        return turn

    async def get_turn_by_idempotency(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        idempotency_key: str,
    ) -> Turn | None:
        result = await self._session.scalars(
            select(Turn)
            .join(Conversation, Conversation.id == Turn.conversation_id)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Turn.idempotency_key == idempotency_key,
            )
        )
        return result.one_or_none()

    async def append_entry(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        role: EntryRole,
        kind: EntryKind,
        modality: Modality,
        content: dict[str, Any],
        delivery_status: DeliveryStatus = DeliveryStatus.PENDING,
        provider_metadata: dict[str, Any] | None = None,
    ) -> Entry:
        conversation = await self._session.scalar(
            self._owned_statement(
                user_id=user_id,
                conversation_id=conversation_id,
                for_update=True,
            )
        )
        if conversation is None:
            raise ConversationNotFoundError("conversation was not found")
        owned_turn_id = await self._session.scalar(
            select(Turn.id)
            .join(Conversation, Conversation.id == Turn.conversation_id)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Turn.id == turn_id,
            )
        )
        if owned_turn_id is None:
            raise TurnNotFoundError("turn was not found")

        sequence = conversation.next_sequence
        conversation.next_sequence = sequence + 1
        entry = Entry(
            conversation_id=conversation_id,
            turn_id=turn_id,
            sequence=sequence,
            role=role,
            kind=kind,
            modality=modality,
            content=content,
            delivery_status=delivery_status,
            provider_metadata=provider_metadata or {},
        )
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def list_entries(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        limit: int = 50,
        cursor: EntryCursor | None = None,
    ) -> EntryPage:
        self._validate_limit(limit)
        statement = (
            select(Entry)
            .join(Conversation, Conversation.id == Entry.conversation_id)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        if cursor is not None:
            statement = statement.where(Entry.sequence > cursor.sequence)
        statement = statement.order_by(Entry.sequence).limit(limit + 1)
        rows = tuple((await self._session.scalars(statement)).all())
        items = rows[:limit]
        next_cursor = EntryCursor(items[-1].sequence) if len(rows) > limit and items else None
        return EntryPage(items=items, next_cursor=next_cursor)
