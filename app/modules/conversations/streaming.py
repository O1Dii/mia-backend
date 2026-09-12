"""Durable text-turn lifecycle and provider-neutral SSE projection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4, uuid5

from anyio import CancelScope
from sqlalchemy import select
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
    IdempotencyConflictError,
    IdempotencyRepository,
)
from app.modules.conversations.services import canonical_request_hash
from app.modules.orchestration import (
    ContextBuilder,
    ContextEntry,
    TextOrchestrationService,
    UserProfile,
)
from app.providers.ai.base import (
    AIEvent,
    ProviderError,
    ResponseCompleted,
    TextAIProvider,
    TextDelta,
    ToolCallRequested,
    UsageReported,
)


class TurnAlreadyStreamingError(RuntimeError):
    """The same idempotent turn is still owned by an active stream."""


class StreamDisconnected(Exception):
    """Internal signal used to finalize a client-disconnected stream."""


@dataclass(frozen=True, slots=True)
class PreparedTextTurn:
    user_id: UUID
    conversation_id: UUID
    turn_id: UUID
    assistant_entry_id: UUID
    response_id: UUID
    replayed_completed: bool
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationStreamEvent:
    name: str
    event_id: UUID
    data: dict[str, object]


DisconnectProbe = Callable[[], Awaitable[bool]]


class TextSSEService:
    """Persist one idempotent text turn while projecting normalized AI events to SSE v1."""

    ENDPOINT = "POST /v1/conversations/{conversation_id}/messages:stream"
    STABLE_INSTRUCTION = "You are MIA, a concise and helpful personal assistant."

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        provider: TextAIProvider,
        *,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._orchestration = TextOrchestrationService(
            provider,
            context_builder or ContextBuilder(),
        )

    async def prepare(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: str,
        text: str,
    ) -> PreparedTextTurn:
        endpoint = self.ENDPOINT.format(conversation_id=conversation_id)
        request_hash = canonical_request_hash(
            {"conversation_id": str(conversation_id), "text": text}
        )
        async with self._session_factory() as session, session.begin():
            conversations = ConversationRepository(session)
            if (
                await conversations.get_owned(user_id=user_id, conversation_id=conversation_id)
                is None
            ):
                raise ConversationNotFoundError("conversation was not found")
            claim = await IdempotencyRepository(session).claim(
                user_id=user_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if claim.replayed:
                return await self._prepare_replay(
                    session=session,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                )

            turn = await conversations.create_turn(
                user_id=user_id,
                conversation_id=conversation_id,
                source=TurnSource.ANDROID_TEXT,
                idempotency_key=idempotency_key,
            )
            user_entry = await conversations.append_entry(
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn.id,
                role=EntryRole.USER,
                kind=EntryKind.TEXT,
                modality=Modality.TEXT,
                content={"text": text},
                delivery_status=DeliveryStatus.DELIVERED,
            )
            response_id = uuid4()
            assistant_entry = await conversations.append_entry(
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn.id,
                role=EntryRole.ASSISTANT,
                kind=EntryKind.TEXT,
                modality=Modality.TEXT,
                content={"text": ""},
                delivery_status=DeliveryStatus.PENDING,
                provider_metadata={"response_id": str(response_id)},
            )
            await IdempotencyRepository(session).bind_resource(
                claim.record,
                resource_kind="turn",
                resource_id=turn.id,
            )
            if user_entry.sequence >= assistant_entry.sequence:
                raise RuntimeError("assistant entry must follow the user entry")
            return PreparedTextTurn(
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn.id,
                assistant_entry_id=assistant_entry.id,
                response_id=response_id,
                replayed_completed=False,
            )

    async def _prepare_replay(
        self,
        *,
        session: AsyncSession,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: str,
    ) -> PreparedTextTurn:
        turn = await session.scalar(
            select(Turn)
            .join(Conversation, Conversation.id == Turn.conversation_id)
            .where(
                Conversation.user_id == user_id,
                Conversation.id == conversation_id,
                Turn.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if turn is None:
            raise IdempotencyConflictError("idempotent mutation resource is unavailable")
        assistant_entry = await session.scalar(
            select(Entry).where(
                Entry.turn_id == turn.id,
                Entry.role == EntryRole.ASSISTANT,
                Entry.kind == EntryKind.TEXT,
            )
        )
        if assistant_entry is None:
            raise IdempotencyConflictError("idempotent mutation resource is unavailable")
        raw_response_id = assistant_entry.provider_metadata.get("response_id")
        try:
            response_id = UUID(str(raw_response_id))
        except (TypeError, ValueError) as error:
            raise IdempotencyConflictError("idempotent mutation response is unavailable") from error
        if turn.status == TurnStatus.COMPLETED:
            finish_reason = str(assistant_entry.provider_metadata.get("finish_reason", "stop"))
            return PreparedTextTurn(
                user_id=user_id,
                conversation_id=conversation_id,
                turn_id=turn.id,
                assistant_entry_id=assistant_entry.id,
                response_id=response_id,
                replayed_completed=True,
                finish_reason=finish_reason,
            )
        if turn.status in {TurnStatus.PENDING, TurnStatus.ACTIVE}:
            raise TurnAlreadyStreamingError("the idempotent turn is already streaming")

        response_id = uuid4()
        turn.status = TurnStatus.PENDING
        turn.completed_at = None
        assistant_entry.content = {"text": ""}
        assistant_entry.delivery_status = DeliveryStatus.PENDING
        assistant_entry.provider_metadata = {"response_id": str(response_id)}
        await session.flush()
        return PreparedTextTurn(
            user_id=user_id,
            conversation_id=conversation_id,
            turn_id=turn.id,
            assistant_entry_id=assistant_entry.id,
            response_id=response_id,
            replayed_completed=False,
        )

    async def stream(
        self,
        prepared: PreparedTextTurn,
        *,
        request_id: str,
        disconnected: DisconnectProbe | None = None,
    ) -> AsyncIterator[ConversationStreamEvent]:
        if prepared.replayed_completed:
            yield self._completion_event(prepared, seq=1, replayed=True)
            return

        text_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, object] | None = None
        last_seq = 0
        terminal = False
        events: AsyncGenerator[AIEvent, None] | None = None
        try:
            await self._activate(prepared)
            entries, profile, summary = await self._context(prepared)
            events = cast(
                AsyncGenerator[AIEvent, None],
                self._orchestration.stream_turn(
                    turn_id=prepared.turn_id,
                    response_id=prepared.response_id,
                    stable_instruction=self.STABLE_INSTRUCTION,
                    entries=entries,
                    profile=profile,
                    summary=summary,
                ),
            )
            async for event in events:
                if disconnected is not None and await disconnected():
                    raise StreamDisconnected
                last_seq = max(last_seq, event.seq)
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    yield ConversationStreamEvent(
                        name="message.delta",
                        event_id=event.event_id,
                        data=self._envelope(event, "message.delta", {"text": event.text}),
                    )
                elif isinstance(event, ToolCallRequested):
                    yield ConversationStreamEvent(
                        name="tool.call.started",
                        event_id=event.event_id,
                        data=self._envelope(event, "tool.call.started", event.payload()),
                    )
                elif isinstance(event, ResponseCompleted):
                    finish_reason = event.finish_reason
                elif isinstance(event, UsageReported):
                    usage = event.payload()
                elif isinstance(event, ProviderError):
                    await self._mark_stopped(
                        prepared,
                        status=TurnStatus.FAILED,
                        delivery_status=DeliveryStatus.FAILED,
                        text="".join(text_parts),
                    )
                    terminal = True
                    yield ConversationStreamEvent(
                        name="error",
                        event_id=event.event_id,
                        data=self._error_envelope(event, request_id=request_id),
                    )
                    return
            if finish_reason is None:
                raise RuntimeError("provider stream ended without completion")
            await self._mark_completed(
                prepared,
                text="".join(text_parts),
                finish_reason=finish_reason,
                usage=usage,
            )
            terminal = True
            yield self._completion_event(
                prepared,
                seq=last_seq + 1,
                replayed=False,
                finish_reason=finish_reason,
            )
        except asyncio.CancelledError:
            if not terminal:
                with CancelScope(shield=True):
                    await self._mark_stopped(
                        prepared,
                        status=TurnStatus.INTERRUPTED,
                        delivery_status=DeliveryStatus.INTERRUPTED,
                        text="".join(text_parts),
                    )
            raise
        except (GeneratorExit, StreamDisconnected):
            if not terminal:
                await self._mark_stopped(
                    prepared,
                    status=TurnStatus.INTERRUPTED,
                    delivery_status=DeliveryStatus.INTERRUPTED,
                    text="".join(text_parts),
                )
            return
        except Exception:
            if not terminal:
                await self._mark_stopped(
                    prepared,
                    status=TurnStatus.FAILED,
                    delivery_status=DeliveryStatus.FAILED,
                    text="".join(text_parts),
                )
            event_id = uuid5(prepared.response_id, "sse:error")
            yield ConversationStreamEvent(
                name="error",
                event_id=event_id,
                data={
                    "v": 1,
                    "type": "error",
                    "event_id": str(event_id),
                    "turn_id": str(prepared.turn_id),
                    "response_id": str(prepared.response_id),
                    "seq": last_seq + 1,
                    "at": self._timestamp(),
                    "payload": {
                        "code": "internal_error",
                        "message": "The response could not be completed.",
                        "request_id": request_id,
                        "retryable": True,
                    },
                },
            )
        finally:
            if events is not None:
                with CancelScope(shield=True):
                    await events.aclose()

    async def interrupt(self, prepared: PreparedTextTurn) -> None:
        """Finalize even a disconnect before the response body starts iterating."""
        await self._mark_stopped(
            prepared,
            status=TurnStatus.INTERRUPTED,
            delivery_status=DeliveryStatus.INTERRUPTED,
            text=None,
        )

    async def _activate(self, prepared: PreparedTextTurn) -> None:
        async with self._session_factory() as session, session.begin():
            turn = await session.get(Turn, prepared.turn_id, with_for_update=True)
            entry = await session.get(Entry, prepared.assistant_entry_id)
            if turn is None or entry is None or turn.status != TurnStatus.PENDING:
                raise TurnAlreadyStreamingError("turn is no longer pending")
            if entry.provider_metadata.get("response_id") != str(prepared.response_id):
                raise TurnAlreadyStreamingError("attempt is no longer current")
            turn.status = TurnStatus.ACTIVE
            entry.delivery_status = DeliveryStatus.STREAMING

    async def _context(
        self, prepared: PreparedTextTurn
    ) -> tuple[tuple[ContextEntry, ...], UserProfile, str | None]:
        async with self._session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == prepared.conversation_id,
                    Conversation.user_id == prepared.user_id,
                )
            )
            if conversation is None:
                raise ConversationNotFoundError("conversation was not found")
            from app.modules.users.models import User

            user = await session.get(User, prepared.user_id)
            if user is None or user.deleted_at is not None:
                raise ConversationNotFoundError("conversation was not found")
            rows = tuple(
                (
                    await session.scalars(
                        select(Entry)
                        .where(
                            Entry.conversation_id == prepared.conversation_id,
                            Entry.kind == EntryKind.TEXT,
                            Entry.role.in_((EntryRole.USER, EntryRole.ASSISTANT)),
                            Entry.id != prepared.assistant_entry_id,
                            Entry.delivery_status == DeliveryStatus.DELIVERED,
                            Entry.sequence
                            < select(Entry.sequence)
                            .where(Entry.id == prepared.assistant_entry_id)
                            .scalar_subquery(),
                        )
                        .order_by(Entry.sequence.desc())
                        .limit(100)
                    )
                ).all()
            )
            context_entries = tuple(
                ContextEntry(
                    role=cast(Literal["user", "assistant"], entry.role.value),
                    text=str(entry.content.get("text", "")),
                )
                for entry in reversed(rows)
                if entry.content.get("text")
            )
            return (
                context_entries,
                UserProfile(locale=user.locale, timezone=user.timezone),
                conversation.summary,
            )

    async def _mark_completed(
        self,
        prepared: PreparedTextTurn,
        *,
        text: str,
        finish_reason: str,
        usage: dict[str, object] | None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            turn = await session.get(Turn, prepared.turn_id, with_for_update=True)
            entry = await session.get(Entry, prepared.assistant_entry_id, with_for_update=True)
            if turn is None or entry is None:
                raise RuntimeError("active text turn disappeared")
            if turn.status != TurnStatus.ACTIVE or entry.provider_metadata.get(
                "response_id"
            ) != str(prepared.response_id):
                raise RuntimeError("text attempt is no longer current")
            turn.status = TurnStatus.COMPLETED
            turn.completed_at = datetime.now(UTC)
            entry.content = {"text": text}
            entry.delivery_status = DeliveryStatus.DELIVERED
            entry.provider_metadata = {
                "finish_reason": finish_reason,
                "response_id": str(prepared.response_id),
                **({"usage": usage} if usage is not None else {}),
            }

    async def _mark_stopped(
        self,
        prepared: PreparedTextTurn,
        *,
        status: TurnStatus,
        delivery_status: DeliveryStatus,
        text: str | None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            turn = await session.get(Turn, prepared.turn_id, with_for_update=True)
            entry = await session.get(Entry, prepared.assistant_entry_id, with_for_update=True)
            if turn is None or entry is None:
                return
            if turn.status not in {
                TurnStatus.PENDING,
                TurnStatus.ACTIVE,
            } or entry.provider_metadata.get("response_id") != str(prepared.response_id):
                return
            turn.status = status
            turn.completed_at = datetime.now(UTC)
            if text is not None:
                entry.content = {"text": text}
            entry.delivery_status = delivery_status
            entry.provider_metadata = {
                **entry.provider_metadata,
                "response_id": str(prepared.response_id),
            }

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _envelope(event: AIEvent, event_type: str, payload: dict[str, object]) -> dict[str, object]:
        value = event.to_contract()
        value["type"] = event_type
        value["payload"] = payload
        return value

    @classmethod
    def _completion_event(
        cls,
        prepared: PreparedTextTurn,
        *,
        seq: int,
        replayed: bool,
        finish_reason: str | None = None,
    ) -> ConversationStreamEvent:
        event_id = uuid5(prepared.response_id, "sse:message.completed")
        return ConversationStreamEvent(
            name="message.completed",
            event_id=event_id,
            data={
                "v": 1,
                "type": "message.completed",
                "event_id": str(event_id),
                "turn_id": str(prepared.turn_id),
                "response_id": str(prepared.response_id),
                "seq": seq,
                "at": cls._timestamp(),
                "payload": {
                    "entry_id": str(prepared.assistant_entry_id),
                    "finish_reason": finish_reason or prepared.finish_reason or "stop",
                    "replayed": replayed,
                },
            },
        )

    @classmethod
    def _error_envelope(cls, event: ProviderError, *, request_id: str) -> dict[str, object]:
        public_code = (
            "provider_timeout" if event.code == "provider_timeout" else "service_unavailable"
        )
        return {
            **event.to_contract(),
            "type": "error",
            "payload": {
                "code": public_code,
                "message": event.message,
                "request_id": request_id,
                "retryable": event.retryable,
            },
        }
