"""FastAPI routes for session creation and the realtime WebSocket."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from app.modules.realtime.contracts import (
    INPUT_AUDIO_FORMAT,
    OUTPUT_AUDIO_FORMAT,
    AudioFormat,
)
from app.modules.realtime.gateway import RealtimeGateway
from app.modules.realtime.sessions import (
    InMemoryRealtimeSessionStore,
    RealtimeAuthenticator,
    RealtimeSessionConflictError,
)
from fastapi import APIRouter, Header, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class CreateRealtimeSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    locale: str = Field(min_length=2, max_length=35)
    input_audio: AudioFormat
    output_audio: AudioFormat


class RealtimeSessionResponse(BaseModel):
    id: UUID
    websocket_url: str
    protocol_version: Literal[1] = 1
    expires_at: datetime


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    return token if separator and scheme.lower() == "bearer" and token else None


def _error(*, status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "retryable": False,
            }
        },
    )


def build_realtime_router(
    *,
    store: InMemoryRealtimeSessionStore,
    authenticator: RealtimeAuthenticator,
    gateway: RealtimeGateway,
    websocket_url_base: str = "wss://localhost/v1/realtime",
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/v1/realtime/sessions",
        status_code=201,
        response_model=RealtimeSessionResponse,
    )
    async def create_session(
        payload: CreateRealtimeSessionRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str = Header(min_length=16, max_length=128, alias="Idempotency-Key"),
    ) -> RealtimeSessionResponse | JSONResponse:
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
        token = _bearer_token(authorization)
        principal = await authenticator.authenticate(token) if token else None
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        if payload.input_audio != INPUT_AUDIO_FORMAT or payload.output_audio != OUTPUT_AUDIO_FORMAT:
            return _error(
                status_code=400,
                code="realtime_protocol_unsupported",
                message="Required PCM audio formats were not selected.",
                request_id=request_id,
            )
        try:
            session, _ = await store.create(
                user_id=principal.user_id,
                conversation_id=payload.conversation_id,
                locale=payload.locale,
                input_audio=payload.input_audio,
                output_audio=payload.output_audio,
                idempotency_key=idempotency_key,
            )
        except RealtimeSessionConflictError:
            return _error(
                status_code=409,
                code="idempotency_conflict",
                message="Idempotency key was used for a different request.",
                request_id=request_id,
            )
        return RealtimeSessionResponse(
            id=session.id,
            websocket_url=f"{websocket_url_base}?session_id={session.id}",
            expires_at=session.expires_at,
        )

    @router.delete(
        "/v1/realtime/sessions/{session_id}",
        status_code=204,
        response_model=None,
    )
    async def close_session(
        session_id: UUID,
        authorization: str | None = Header(default=None),
    ) -> None | JSONResponse:
        token = _bearer_token(authorization)
        principal = await authenticator.authenticate(token) if token else None
        session = await store.get(session_id)
        if principal is None or session is None or session.user_id != principal.user_id:
            return _error(
                status_code=404,
                code="realtime_session_not_found",
                message="Realtime session was not found.",
                request_id=f"req_{uuid4().hex}",
            )
        await store.finish(session_id, interrupted=False)
        return None

    @router.websocket("/v1/realtime")
    async def realtime(websocket: WebSocket) -> None:
        await gateway.handle(websocket)

    return router
