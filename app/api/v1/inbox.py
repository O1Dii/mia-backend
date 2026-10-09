"""``/v1/inbox*`` and ``/v1/notify`` (contracts/mvp/v1/openapi.yaml): the assistant inbox."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.jobs import (
    Channel,
    IdempotencyKey,
    empty_response,
    error_response,
    json_response,
)
from app.modules.auth.principal import RequestAuthenticator
from app.modules.jobs.domain import JobError
from app.modules.jobs.service import JobService
from app.modules.notifications.inbox import InboxService, InvalidInboxCursorError


class NotifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)
    channels: list[Channel] | None = Field(default=None, min_length=1)


def build_inbox_router(
    *, inbox: InboxService, jobs: JobService, authenticator: RequestAuthenticator
) -> APIRouter:
    router = APIRouter(tags=["Background"])

    async def user_of(request: Request) -> uuid.UUID:
        principal = await authenticator.authenticate(request)
        return uuid.UUID(principal.user_id)

    @router.get("/v1/inbox")
    async def list_inbox(
        request: Request,
        cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        unread: Annotated[bool | None, Query()] = None,
    ) -> JSONResponse:
        user = await user_of(request)
        try:
            items, following = await inbox.list(
                user_id=user, unread=unread, limit=limit, cursor=cursor
            )
        except InvalidInboxCursorError:
            return error_response(request, "invalid_cursor")
        return json_response(
            request, {"items": [item.to_wire() for item in items], "next_cursor": following}
        )

    @router.post("/v1/inbox/{item_id}/read", status_code=204)
    async def mark_read(
        item_id: str, request: Request, idempotency_key: IdempotencyKey
    ) -> Response:
        user = await user_of(request)
        try:
            parsed = uuid.UUID(item_id)
        except ValueError:
            return error_response(request, "inbox_item_not_found")
        if not await inbox.mark_read(user_id=user, item_id=parsed):
            return error_response(request, "inbox_item_not_found")
        return empty_response(request)

    @router.post("/v1/notify", status_code=202)
    async def notify(
        request: Request, body: Annotated[NotifyBody, Body()], idempotency_key: IdempotencyKey
    ) -> JSONResponse:
        user = await user_of(request)
        try:
            item = await jobs.notify(
                user_id=user,
                text=body.text,
                channels=body.channels,
                source_type="system",
                source_id="api:notify",
            )
        except JobError as error:
            return error_response(request, error.code)
        return json_response(request, item.to_wire(), 202)

    return router
