"""``GET /v1/feed`` (contracts/mvp/v1/openapi.yaml, M18/M74): history of one chat.

``conversation_id`` selects a chat (ADR-0017); without it the default chat (the most
recently active one) is read, so single-feed clients keep working, and the page says which
chat it is. Items come newest first; ``before`` is the opaque ``next_cursor`` of the
previous page. Android (bearer) and web (``mia_session`` cookie) resolve to the same
principal (M10); only the caller's own chats are reachable (``404 conversation_not_found``),
and a cursor of any other chat answers ``400 invalid_feed_cursor``. Incognito runs never
write to a chat, so they are never listed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.modules.auth.principal import AuthenticationFailure
from app.modules.conversations.feed import FeedItem, FeedPage, InvalidFeedCursorError
from app.modules.conversations.repositories import ConversationNotFoundError


class FeedPrincipal(Protocol):
    @property
    def user_id(self) -> str: ...


class FeedAuthenticator(Protocol):
    """Satisfied by ``RequestAuthenticator``: bearer first, then the web session cookie."""

    async def authenticate(self, request: Request) -> FeedPrincipal: ...


class FeedBackend(Protocol):
    async def history(
        self,
        *,
        user_id: uuid.UUID,
        before: str | None = None,
        limit: int = 50,
        conversation_id: uuid.UUID | None = None,
    ) -> FeedPage: ...


def _iso(value: Any) -> str:
    return str(value.isoformat()).replace("+00:00", "Z")


def feed_item_body(item: FeedItem) -> dict[str, Any]:
    """``FeedItem`` schema (``actions`` only when the answer has screen actions, M41-R1;
    ``card`` only on result cards, M74)."""
    body: dict[str, Any] = {
        "id": str(item.id),
        "role": item.role,
        "source": item.source,
        "text": item.text,
        "display": item.display,
        "run_id": str(item.run_id) if item.run_id is not None else None,
        "created_at": _iso(item.created_at),
    }
    if item.actions:
        body["actions"] = [dict(action) for action in item.actions]
    if item.card is not None:
        body["card"] = dict(item.card)
    return body


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _error(
    request: Request, status_code: int, code: str, message: str, *, retryable: bool = False
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "retryable": retryable,
            }
        },
    )


class FeedRoute(APIRoute):
    """Validation failures use the public envelope without echoing input."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return _error(request, 422, "validation_error", "The request is invalid.")

        return handle


def build_feed_router(*, service: FeedBackend, authenticator: FeedAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/feed", tags=["Feed"], route_class=FeedRoute)

    @router.get("")
    async def list_feed(
        request: Request,
        before: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        conversation_id: uuid.UUID | None = None,
    ) -> JSONResponse:
        try:
            principal = await authenticator.authenticate(request)
        except AuthenticationFailure as failure:
            return _error(
                request,
                failure.status_code,
                failure.code,
                failure.message,
                retryable=failure.retryable,
            )
        try:
            page = await service.history(
                user_id=uuid.UUID(principal.user_id),
                before=before,
                limit=limit,
                conversation_id=conversation_id,
            )
        except InvalidFeedCursorError:
            return _error(request, 400, "invalid_feed_cursor", "The feed cursor is invalid.")
        except ConversationNotFoundError:
            return _error(request, 404, "conversation_not_found", "The conversation was not found.")
        conversation = page.conversation_id
        return JSONResponse(
            headers={"X-Request-ID": _request_id(request), "Cache-Control": "no-store"},
            content={
                "items": [feed_item_body(item) for item in page.items],
                "next_cursor": page.next_cursor,
                "conversation_id": str(conversation) if conversation is not None else None,
            },
        )

    return router
