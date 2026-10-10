"""``/v1/chats*`` (contracts/mvp/v1/openapi.yaml, M74, ADR-0017): the user's chats.

A chat is a conversation with its own history and model context; memory, plugins, jobs,
inbox and approvals are shared by all chats of the account. The legacy S-era
``/v1/conversations`` API stays untouched, so chats live under their own path.

- ``GET /v1/chats`` — pinned first, then by last activity (keyset ``cursor``);
- ``POST /v1/chats`` — a new empty chat (``Idempotency-Key`` required);
- ``GET /v1/chats/continue`` — the chat whose last answer is < ``within_seconds`` old
  («Продолжить прошлый»), or ``null``;
- ``GET|PUT /v1/chats/settings`` — retention: 30/60/180 days of inactivity or never;
- ``GET|PATCH|DELETE /v1/chats/{chat_id}`` — read, rename/pin, delete (memory stays).

Messages of a chat: ``GET /v1/feed?conversation_id=…``; send: ``POST /v1/runs`` with
``conversation_id`` (voice too). Bearer (Android) and the ``mia_session`` cookie (web)
resolve to the same user (M10).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.auth.principal import RequestAuthenticator
from app.modules.conversations.chats import (
    CONTINUE_WINDOW_S,
    TITLE_MAX,
    ChatService,
    InvalidChatCursorError,
)
from app.modules.conversations.repositories import (
    ConversationNotFoundError,
    IdempotencyConflictError,
)
from app.modules.conversations.services import canonical_request_hash

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]

ERRORS: dict[str, tuple[int, str]] = {
    "conversation_not_found": (404, "The conversation was not found."),
    "invalid_chat_cursor": (400, "The chat list cursor is invalid."),
    "idempotency_conflict": (409, "The idempotency key was reused for a different request."),
}


class CreateChatBody(BaseModel):
    """``CreateChatRequest``."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)


class UpdateChatBody(BaseModel):
    """``UpdateChatRequest``: ``title`` null returns to the automatic title."""

    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    pinned: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def require_a_change(self) -> UpdateChatBody:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        if "pinned" in self.model_fields_set and self.pinned is None:
            raise ValueError("pinned must be a boolean")
        return self


class ChatSettingsBody(BaseModel):
    """``ChatSettings`` (request): ``retention_days`` 30, 60, 180 or null (never)."""

    model_config = ConfigDict(extra="forbid")
    retention_days: int | None = Field(strict=True)

    @model_validator(mode="after")
    def check_choice(self) -> ChatSettingsBody:
        if self.retention_days is not None and self.retention_days not in (30, 60, 180):
            raise ValueError("retention_days must be 30, 60, 180 or null")
        return self


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _json(request: Request, content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"X-Request-ID": _request_id(request), "Cache-Control": "no-store"},
    )


def _error(request: Request, code: str) -> JSONResponse:
    status, message = ERRORS[code]
    return _json(
        request,
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": _request_id(request),
                "retryable": False,
            }
        },
        status,
    )


def build_chats_router(*, service: ChatService, authenticator: RequestAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/chats", tags=["Chats"])

    async def user_of(request: Request) -> uuid.UUID:
        principal = await authenticator.authenticate(request)
        return uuid.UUID(principal.user_id)

    @router.get("")
    async def list_chats(
        request: Request,
        cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        user = await user_of(request)
        try:
            page = await service.list(user_id=user, limit=limit, cursor=cursor)
        except InvalidChatCursorError:
            return _error(request, "invalid_chat_cursor")
        return _json(
            request,
            {"items": [chat.to_wire() for chat in page.items], "next_cursor": page.next_cursor},
        )

    @router.post("", status_code=201)
    async def create_chat(
        request: Request,
        idempotency_key: IdempotencyKey,
        body: Annotated[CreateChatBody | None, Body()] = None,
    ) -> JSONResponse:
        user = await user_of(request)
        payload = (body or CreateChatBody()).model_dump(mode="json", exclude_none=True)
        try:
            chat, _ = await service.create(
                user_id=user,
                title=payload.get("title"),
                idempotency_key=idempotency_key,
                request_hash=canonical_request_hash(payload),
            )
        except IdempotencyConflictError:
            return _error(request, "idempotency_conflict")
        return _json(request, chat.to_wire(), 201)

    @router.get("/continue")
    async def continue_chat(
        request: Request,
        within_seconds: Annotated[int, Query(ge=30, le=3600)] = CONTINUE_WINDOW_S,
    ) -> JSONResponse:
        user = await user_of(request)
        chat = await service.continuable(user_id=user, within_s=within_seconds)
        return _json(
            request,
            {
                "chat": chat.to_wire() if chat is not None else None,
                "within_seconds": within_seconds,
            },
        )

    @router.get("/settings")
    async def get_settings(request: Request) -> JSONResponse:
        user = await user_of(request)
        return _json(request, (await service.settings(user_id=user)).to_wire())

    @router.put("/settings")
    async def put_settings(
        request: Request,
        body: Annotated[ChatSettingsBody, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        user = await user_of(request)
        updated = await service.update_settings(user_id=user, retention_days=body.retention_days)
        return _json(request, updated.to_wire())

    @router.get("/{chat_id}")
    async def get_chat(chat_id: uuid.UUID, request: Request) -> JSONResponse:
        user = await user_of(request)
        try:
            chat = await service.get(user_id=user, chat_id=chat_id)
        except ConversationNotFoundError:
            return _error(request, "conversation_not_found")
        return _json(request, chat.to_wire())

    @router.patch("/{chat_id}")
    async def update_chat(
        chat_id: uuid.UUID,
        request: Request,
        body: Annotated[UpdateChatBody, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        user = await user_of(request)
        changes: dict[str, Any] = {}
        if "title" in body.model_fields_set:
            changes["title"] = body.title
        if body.pinned is not None:
            changes["pinned"] = body.pinned
        try:
            chat = await service.update(user_id=user, chat_id=chat_id, **changes)
        except ConversationNotFoundError:
            return _error(request, "conversation_not_found")
        return _json(request, chat.to_wire())

    @router.delete("/{chat_id}", status_code=204)
    async def delete_chat(
        chat_id: uuid.UUID,
        request: Request,
        idempotency_key: IdempotencyKey,
    ) -> Response:
        user = await user_of(request)
        try:
            await service.delete(user_id=user, chat_id=chat_id)
        except ConversationNotFoundError:
            return _error(request, "conversation_not_found")
        return Response(status_code=204, headers={"X-Request-ID": _request_id(request)})

    return router
