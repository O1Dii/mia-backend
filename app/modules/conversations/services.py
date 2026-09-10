"""Conversation use cases and explicit transaction boundaries."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.conversations.models import Conversation, Turn, TurnSource
from app.modules.conversations.repositories import (
    ConversationRepository,
    IdempotencyConflictError,
    IdempotencyRepository,
)


@dataclass(frozen=True, slots=True)
class IdempotentResult[T]:
    value: T
    replayed: bool


def canonical_request_hash(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


class ConversationService:
    """Own atomic mutation boundaries while repositories remain commit-neutral."""

    CREATE_ENDPOINT = "POST /v1/conversations"
    TURN_ENDPOINT = "POST /v1/conversations/{conversation_id}/turns"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_conversation(
        self,
        *,
        user_id: uuid.UUID,
        idempotency_key: str,
        title: str | None = None,
    ) -> IdempotentResult[Conversation]:
        request_hash = canonical_request_hash({"title": title})
        async with self._session_factory() as session, session.begin():
            idempotency = IdempotencyRepository(session)
            conversations = ConversationRepository(session)
            claim = await idempotency.claim(
                user_id=user_id,
                endpoint=self.CREATE_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if claim.replayed:
                if claim.record.resource_kind != "conversation" or claim.record.resource_id is None:
                    raise IdempotencyConflictError("idempotent mutation has no completed resource")
                existing = await conversations.get_owned(
                    user_id=user_id,
                    conversation_id=claim.record.resource_id,
                )
                if existing is None:
                    raise IdempotencyConflictError("idempotent mutation resource is unavailable")
                return IdempotentResult(value=existing, replayed=True)

            conversation = await conversations.create(user_id=user_id, title=title)
            await idempotency.bind_resource(
                claim.record,
                resource_kind="conversation",
                resource_id=conversation.id,
            )
            return IdempotentResult(value=conversation, replayed=False)

    async def create_turn(
        self,
        *,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        idempotency_key: str,
        source: TurnSource,
    ) -> IdempotentResult[Turn]:
        request_hash = canonical_request_hash(
            {"conversation_id": str(conversation_id), "source": source.value}
        )
        endpoint = self.TURN_ENDPOINT.format(conversation_id=conversation_id)
        async with self._session_factory() as session, session.begin():
            idempotency = IdempotencyRepository(session)
            conversations = ConversationRepository(session)
            claim = await idempotency.claim(
                user_id=user_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if claim.replayed:
                existing = await conversations.get_turn_by_idempotency(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                )
                if existing is None:
                    raise IdempotencyConflictError("idempotent mutation resource is unavailable")
                return IdempotentResult(value=existing, replayed=True)

            turn = await conversations.create_turn(
                user_id=user_id,
                conversation_id=conversation_id,
                source=source,
                idempotency_key=idempotency_key,
            )
            await idempotency.bind_resource(
                claim.record,
                resource_kind="turn",
                resource_id=turn.id,
            )
            return IdempotentResult(value=turn, replayed=False)
