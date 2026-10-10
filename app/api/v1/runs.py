"""``/v1/runs*`` (contracts/mvp/v1/openapi.yaml, ADR-0011): create, read, events, cancel."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.modules.auth.principal import AuthenticationFailure, RequestAuthenticator
from app.modules.conversations.repositories import (
    ConversationNotFoundError,
    IdempotencyConflictError,
)
from app.modules.conversations.services import canonical_request_hash
from app.modules.realtime.sessions import AuthenticatedUser
from app.modules.runs.domain import (
    InvalidRunCursorError,
    NewRun,
    RunCursor,
    RunNotFoundError,
    RunRecord,
    isoformat,
)
from app.modules.runs.service import RunService


class RunsAuthenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None: ...


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=16000)


class CreateRunBody(BaseModel):
    """``CreateRunRequest``: public clients start ``voice`` and ``chat`` runs only."""

    model_config = ConfigDict(extra="forbid")
    profile: Literal["voice", "chat"]
    input: RunInput
    conversation_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    incognito: bool = False


def run_body(record: RunRecord) -> dict[str, Any]:
    """``Run`` schema (+ additive fields documented in openapi.yaml)."""
    body: dict[str, Any] = {
        "id": str(record.id),
        "profile": record.profile,
        "status": record.status,
        "created_at": isoformat(record.created_at),
        "events_url": f"/v1/runs/{record.id}/events",
        "steps": record.steps,
        "tool_calls": record.tool_calls,
    }
    if record.response is not None:
        body["response"] = record.response
    if record.model is not None:
        body["model"] = record.model
    if record.conversation_id is not None and not record.incognito:
        body["conversation_id"] = str(record.conversation_id)
    if record.error is not None:
        body["error"] = record.error
    if record.cancel_reason is not None:
        body["cancel_reason"] = record.cancel_reason
    if record.finished_at is not None:
        body["finished_at"] = isoformat(record.finished_at)
    return body


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _error(
    request: Request, status_code: int, code: str, message: str, *, retryable: bool = False
) -> JSONResponse:
    request_id = _request_id(request)
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


def _bearer(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    return token if separator and scheme.lower() == "bearer" and token else None


class _CrossSite(Exception):
    """A cookie-authenticated mutation from another site."""


class RunsRoute(APIRoute):
    """Validation failures use the public envelope without echoing input."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return _error(request, 422, "validation_error", "The request is invalid.")
            except _CrossSite:
                return _error(request, 403, "permission_denied", "Cross-site request rejected.")

        return handle


def _sse(cursor: str, event_type: str, data: dict[str, Any]) -> bytes:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {cursor}\nevent: {event_type}\ndata: {serialized}\n\n".encode()


def build_runs_router(
    *,
    service: RunService,
    authenticator: RunsAuthenticator,
    web_authenticator: RequestAuthenticator | None = None,
) -> APIRouter:
    """``web_authenticator`` additionally accepts the web ``mia_session`` cookie (M30)."""
    router = APIRouter(prefix="/v1/runs", tags=["Runs"], route_class=RunsRoute)

    async def principal(request: Request, authorization: str | None) -> AuthenticatedUser | None:
        token = _bearer(authorization)
        if token:
            return await authenticator.authenticate(token)
        if authorization is None and web_authenticator is not None:
            if not request.cookies.get(web_authenticator.policy.session_cookie):
                return None
            try:
                web = await web_authenticator.authenticate(request)
            except AuthenticationFailure as failure:
                if failure.status_code == 403:
                    raise _CrossSite from failure
                return None
            return AuthenticatedUser(user_id=uuid.UUID(web.user_id))
        return None

    def unauthorized(request: Request) -> JSONResponse:
        return _error(request, 401, "authentication_required", "Authentication is required.")

    def not_found(request: Request) -> JSONResponse:
        return _error(request, 404, "run_not_found", "The run was not found.")

    @router.post("", status_code=202)
    async def create_run(
        body: CreateRunBody,
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    ) -> JSONResponse:
        user = await principal(request, authorization)
        if user is None:
            return unauthorized(request)
        try:
            record, _ = await service.start(
                NewRun(
                    user_id=user.user_id,
                    profile=body.profile,
                    input_text=body.input.text,
                    conversation_id=body.conversation_id,
                    device_id=body.device_id,
                    incognito=body.incognito,
                ),
                idempotency_key=idempotency_key,
                request_hash=canonical_request_hash(body.model_dump(mode="json")),
            )
        except IdempotencyConflictError:
            return _error(
                request,
                409,
                "idempotency_conflict",
                "The idempotency key was reused for a different request.",
            )
        except ConversationNotFoundError:
            return _error(request, 404, "conversation_not_found", "The conversation was not found.")
        return JSONResponse(
            status_code=202,
            headers={"X-Request-ID": _request_id(request)},
            content=run_body(record),
        )

    @router.get("/{run_id}")
    async def get_run(
        run_id: uuid.UUID, request: Request, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        user = await principal(request, authorization)
        if user is None:
            return unauthorized(request)
        try:
            record = await service.get(user_id=user.user_id, run_id=run_id)
        except RunNotFoundError:
            return not_found(request)
        return JSONResponse(
            headers={"X-Request-ID": _request_id(request)}, content=run_body(record)
        )

    @router.get("/{run_id}/events", response_model=None)
    async def stream_run_events(
        run_id: uuid.UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        last_event_id: str | None = Header(
            default=None, alias="Last-Event-ID", min_length=1, max_length=512
        ),
    ) -> StreamingResponse | JSONResponse:
        user = await principal(request, authorization)
        if user is None:
            return unauthorized(request)
        resume = last_event_id or cursor
        try:
            after = RunCursor.decode(resume, run_id=run_id).sequence if resume else 0
            await service.get(user_id=user.user_id, run_id=run_id)
        except InvalidRunCursorError:
            return _error(request, 400, "invalid_cursor", "The event cursor is invalid.")
        except RunNotFoundError:
            return not_found(request)

        async def body() -> AsyncIterator[bytes]:
            async for event in service.events(
                user_id=user.user_id, run_id=run_id, after_sequence=after
            ):
                if event is None:
                    yield b": keepalive\n\n"
                else:
                    yield _sse(event.cursor, event.type, event.to_wire())

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-ID": _request_id(request),
            },
        )

    @router.post("/{run_id}/cancel", status_code=202)
    async def cancel_run(
        run_id: uuid.UUID,
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    ) -> JSONResponse:
        user = await principal(request, authorization)
        if user is None:
            return unauthorized(request)
        try:
            record = await service.cancel(user_id=user.user_id, run_id=run_id)
        except RunNotFoundError:
            return not_found(request)
        return JSONResponse(
            status_code=202,
            headers={"X-Request-ID": _request_id(request)},
            content=run_body(record),
        )

    return router
