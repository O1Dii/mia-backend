"""Versioned conversation REST routes and text/event-stream transport."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import aclosing
from typing import Any, Protocol, cast
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Receive, Scope, Send

from app.modules.conversations.api_schemas import (
    ConversationPageResponse,
    ConversationResponse,
    CreateConversationRequest,
    EntryPageResponse,
    EntryResponse,
    SendMessageRequest,
    UpdateConversationRequest,
)
from app.modules.conversations.cursors import ConversationCursor, EntryCursor
from app.modules.conversations.repositories import (
    ConversationNotFoundError,
    ConversationRepository,
    IdempotencyConflictError,
)
from app.modules.conversations.services import ConversationService
from app.modules.conversations.streaming import (
    ConversationStreamEvent,
    TextSSEService,
    TurnAlreadyStreamingError,
)
from app.modules.realtime.sessions import AuthenticatedUser


class ConversationAuthenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None: ...


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    return token if separator and scheme.lower() == "bearer" and token else None


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _error(
    *, status_code: int, code: str, message: str, request_id: str, retryable: bool = False
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers={"X-Request-ID": request_id},
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "retryable": retryable,
            }
        },
    )


def _conversation_not_found(request_id: str) -> JSONResponse:
    return _error(
        status_code=404,
        code="conversation_not_found",
        message="The conversation was not found.",
        request_id=request_id,
    )


def _encode_sse(*, name: str, event_id: UUID, data: dict[str, object]) -> bytes:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {name}\ndata: {serialized}\n\n".encode()


class ConversationRoute(APIRoute):
    """Keep validation failures in the versioned error contract without echoing input."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return _error(
                    status_code=422,
                    code="validation_error",
                    message="The request is invalid.",
                    request_id=_request_id(request),
                )

        return handle


class ConversationStreamingResponse(StreamingResponse):
    """Release the iterator and pending claim even if transport exits before iteration."""

    def __init__(
        self,
        content: AsyncGenerator[bytes, None],
        cleanup: Callable[[], Awaitable[None]],
        *,
        headers: dict[str, str],
    ) -> None:
        super().__init__(content, media_type="text/event-stream", headers=headers)
        self._content = content
        self._cleanup = cleanup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                try:
                    await self._content.aclose()
                finally:
                    await self._cleanup()


def build_conversation_router(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    authenticator: ConversationAuthenticator,
    stream_service: TextSSEService,
) -> APIRouter:
    router = APIRouter(
        prefix="/v1/conversations", tags=["Conversations"], route_class=ConversationRoute
    )
    service = ConversationService(session_factory)

    async def authenticate(
        authorization: str | None,
    ) -> AuthenticatedUser | None:
        token = _bearer_token(authorization)
        return await authenticator.authenticate(token) if token else None

    @router.post("", status_code=201, response_model=ConversationResponse)
    async def create_conversation(
        payload: CreateConversationRequest,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
        idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    ) -> ConversationResponse | JSONResponse:
        request_id = _request_id(request)
        principal = await authenticate(authorization)
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        try:
            result = await service.create_conversation(
                user_id=principal.user_id,
                idempotency_key=idempotency_key,
                title=payload.title,
            )
        except IdempotencyConflictError:
            return _error(
                status_code=409,
                code="idempotency_conflict",
                message="The idempotency key was reused for a different request.",
                request_id=request_id,
            )
        response.headers["X-Request-ID"] = request_id
        return ConversationResponse.model_validate(result.value)

    @router.get("", response_model=ConversationPageResponse)
    async def list_conversations(
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> ConversationPageResponse | JSONResponse:
        request_id = _request_id(request)
        principal = await authenticate(authorization)
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        try:
            decoded = ConversationCursor.decode(cursor) if cursor else None
        except ValueError:
            return _error(
                status_code=400,
                code="bad_request",
                message="The pagination cursor is invalid.",
                request_id=request_id,
            )
        async with session_factory() as session:
            page = await ConversationRepository(session).list_owned(
                user_id=principal.user_id,
                cursor=decoded,
                limit=limit,
            )
        response.headers["X-Request-ID"] = request_id
        return ConversationPageResponse(
            items=tuple(ConversationResponse.model_validate(item) for item in page.items),
            next_cursor=page.next_cursor.encode() if page.next_cursor else None,
        )

    @router.get("/{conversation_id}", response_model=ConversationResponse)
    async def get_conversation(
        conversation_id: UUID,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> ConversationResponse | JSONResponse:
        request_id = _request_id(request)
        principal = await authenticate(authorization)
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        async with session_factory() as session:
            conversation = await ConversationRepository(session).get_owned(
                user_id=principal.user_id,
                conversation_id=conversation_id,
            )
        if conversation is None:
            return _conversation_not_found(request_id)
        response.headers["X-Request-ID"] = request_id
        return ConversationResponse.model_validate(conversation)

    @router.patch("/{conversation_id}", response_model=ConversationResponse)
    async def update_conversation(
        conversation_id: UUID,
        payload: UpdateConversationRequest,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
        idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    ) -> ConversationResponse | JSONResponse:
        request_id = _request_id(request)
        principal = await authenticate(authorization)
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        try:
            result = await service.update_conversation(
                user_id=principal.user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                title=payload.title,
                title_provided="title" in payload.model_fields_set,
                archived=payload.archived,
            )
        except ConversationNotFoundError:
            return _conversation_not_found(request_id)
        except IdempotencyConflictError:
            return _error(
                status_code=409,
                code="idempotency_conflict",
                message="The idempotency key was reused for a different request.",
                request_id=request_id,
            )
        response.headers["X-Request-ID"] = request_id
        return ConversationResponse.model_validate(result.value)

    @router.delete("/{conversation_id}", status_code=204, response_model=None)
    async def delete_conversation(
        conversation_id: UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    ) -> Response | JSONResponse:
        request_id = _request_id(request)
        principal = await authenticate(authorization)
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        try:
            await service.delete_conversation(
                user_id=principal.user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
            )
        except ConversationNotFoundError:
            return _conversation_not_found(request_id)
        except IdempotencyConflictError:
            return _error(
                status_code=409,
                code="idempotency_conflict",
                message="The idempotency key was reused for a different request.",
                request_id=request_id,
            )
        return Response(status_code=204, headers={"X-Request-ID": request_id})

    @router.get("/{conversation_id}/entries", response_model=EntryPageResponse)
    async def list_entries(
        conversation_id: UUID,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> EntryPageResponse | JSONResponse:
        request_id = _request_id(request)
        principal = await authenticate(authorization)
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        try:
            decoded = EntryCursor.decode(cursor) if cursor else None
        except ValueError:
            return _error(
                status_code=400,
                code="bad_request",
                message="The pagination cursor is invalid.",
                request_id=request_id,
            )
        async with session_factory() as session:
            repository = ConversationRepository(session)
            if (
                await repository.get_owned(
                    user_id=principal.user_id,
                    conversation_id=conversation_id,
                )
                is None
            ):
                return _conversation_not_found(request_id)
            page = await repository.list_entries(
                user_id=principal.user_id,
                conversation_id=conversation_id,
                cursor=decoded,
                limit=limit,
            )
        response.headers["X-Request-ID"] = request_id
        return EntryPageResponse(
            items=tuple(EntryResponse.model_validate(item) for item in page.items),
            next_cursor=page.next_cursor.encode() if page.next_cursor else None,
        )

    @router.post("/{conversation_id}/messages:stream", response_model=None)
    async def stream_message(
        conversation_id: UUID,
        payload: SendMessageRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    ) -> StreamingResponse | JSONResponse:
        request_id = _request_id(request)
        principal = await authenticate(authorization)
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        try:
            prepared = await stream_service.prepare(
                user_id=principal.user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                text=payload.text,
            )
        except ConversationNotFoundError:
            return _conversation_not_found(request_id)
        except IdempotencyConflictError:
            return _error(
                status_code=409,
                code="idempotency_conflict",
                message="The idempotency key was reused for a different request.",
                request_id=request_id,
            )
        except TurnAlreadyStreamingError:
            return _error(
                status_code=409,
                code="conflict",
                message="The response is already streaming.",
                request_id=request_id,
                retryable=True,
            )

        async def body() -> AsyncGenerator[bytes, None]:
            events = cast(
                AsyncGenerator[ConversationStreamEvent, None],
                stream_service.stream(prepared, request_id=request_id),
            )
            async with aclosing(events):
                async for event in events:
                    yield _encode_sse(name=event.name, event_id=event.event_id, data=event.data)

        async def cleanup() -> None:
            await stream_service.interrupt(prepared)

        return ConversationStreamingResponse(
            body(),
            cleanup,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-ID": request_id,
            },
        )

    return router
