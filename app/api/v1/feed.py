"""``GET /v1/feed`` (contracts/mvp/v1/openapi.yaml, M18): history of the user's main feed.

One continuous feed per user holds voice and chat messages (M12). Items come newest
first; ``before`` is the opaque ``next_cursor`` of the previous page. Android (bearer)
and web (``mia_session`` cookie) resolve to the same principal (M10); only the caller's
own feed is reachable, and a cursor of any other feed answers ``400 invalid_feed_cursor``.
Incognito runs never write to the feed, so they are never listed.
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


class FeedPrincipal(Protocol):
    @property
    def user_id(self) -> str: ...


class FeedAuthenticator(Protocol):
    """Satisfied by ``RequestAuthenticator``: bearer first, then the web session cookie."""

    async def authenticate(self, request: Request) -> FeedPrincipal: ...


class FeedBackend(Protocol):
    async def history(
        self, *, user_id: uuid.UUID, before: str | None = None, limit: int = 50
    ) -> FeedPage: ...


def _iso(value: Any) -> str:
    return str(value.isoformat()).replace("+00:00", "Z")


def feed_item_body(item: FeedItem) -> dict[str, Any]:
    """``FeedItem`` schema."""
    return {
        "id": str(item.id),
        "role": item.role,
        "source": item.source,
        "text": item.text,
        "display": item.display,
        "run_id": str(item.run_id) if item.run_id is not None else None,
        "created_at": _iso(item.created_at),
    }


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
                user_id=uuid.UUID(principal.user_id), before=before, limit=limit
            )
        except InvalidFeedCursorError:
            return _error(request, 400, "invalid_feed_cursor", "The feed cursor is invalid.")
        return JSONResponse(
            headers={"X-Request-ID": _request_id(request), "Cache-Control": "no-store"},
            content={
                "items": [feed_item_body(item) for item in page.items],
                "next_cursor": page.next_cursor,
            },
        )

    return router
