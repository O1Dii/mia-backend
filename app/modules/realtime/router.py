"""FastAPI routes for session creation and the realtime WebSocket."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.limits import BudgetUnavailableError, DailyBudget, record_admission_rejection
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
    RealtimeSessionLimitError,
)


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


def _error(
    *, status_code: int, code: str, message: str, request_id: str, retryable: bool = False
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "retryable": retryable,
            }
        },
    )


def build_realtime_router(
    *,
    store: InMemoryRealtimeSessionStore,
    authenticator: RealtimeAuthenticator,
    gateway: RealtimeGateway,
    websocket_url_base: str | None = None,
    budget: DailyBudget | None = None,
    max_daily_sessions: int = 20,
    voice_enabled: bool = True,
    provider_available: Callable[[], bool] | None = None,
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
        request_id = str(getattr(request.state, "request_id", f"req_{uuid4().hex}"))
        token = _bearer_token(authorization)
        principal = await authenticator.authenticate(token) if token else None
        if principal is None:
            return _error(
                status_code=401,
                code="authentication_required",
                message="Authentication is required.",
                request_id=request_id,
            )
        if not voice_enabled:
            record_admission_rejection("voice_disabled")
            return _error(
                status_code=503,
                code="service_unavailable",
                message="Voice is unavailable.",
                request_id=request_id,
                retryable=True,
            )
        if provider_available is not None and not provider_available():
            record_admission_rejection("provider_circuit_open")
            return _error(
                status_code=503,
                code="realtime_provider_unavailable",
                message="Voice service is temporarily unavailable.",
                request_id=request_id,
                retryable=True,
            )
        if payload.input_audio != INPUT_AUDIO_FORMAT or payload.output_audio != OUTPUT_AUDIO_FORMAT:
            return _error(
                status_code=400,
                code="realtime_protocol_unsupported",
                message="Required PCM audio formats were not selected.",
                request_id=request_id,
            )
        try:
            session, replayed = await store.create(
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
        except RealtimeSessionLimitError:
            record_admission_rejection("active_live_sessions")
            return _error(
                status_code=429,
                code="rate_limit_exceeded",
                message="Active voice limit reached.",
                request_id=request_id,
                retryable=True,
            )
        # Charge only admitted sessions; a denied session is released immediately.
        if budget is not None:
            try:
                allowed, retry_after = await budget.spend(
                    "live_sessions",
                    principal.user_id,
                    limit=max_daily_sessions,
                    idempotency_key=idempotency_key,
                )
            except BudgetUnavailableError:
                if not replayed:
                    await store.finish(session.id, interrupted=True)
                record_admission_rejection("budget_unavailable")
                return _error(
                    status_code=503,
                    code="service_unavailable",
                    message="Voice is unavailable.",
                    request_id=request_id,
                    retryable=True,
                )
            if not allowed:
                if not replayed:
                    await store.finish(session.id, interrupted=True)
                record_admission_rejection("daily_live_sessions", retry_after_seconds=retry_after)
                response = _error(
                    status_code=429,
                    code="rate_limit_exceeded",
                    message="Daily voice limit reached.",
                    request_id=request_id,
                    retryable=True,
                )
                response.headers["Retry-After"] = str(retry_after)
                return response
        public_websocket_url = websocket_url_base
        if public_websocket_url is None:
            scheme = "wss" if request.url.scheme == "https" else "ws"
            public_websocket_url = str(request.url_for("realtime_websocket").replace(scheme=scheme))
        return RealtimeSessionResponse(
            id=session.id,
            websocket_url=f"{public_websocket_url}?session_id={session.id}",
            expires_at=session.expires_at,
        )

    @router.delete(
        "/v1/realtime/sessions/{session_id}",
        status_code=204,
        response_model=None,
    )
    async def close_session(
        session_id: UUID,
        request: Request,
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
                request_id=str(getattr(request.state, "request_id", f"req_{uuid4().hex}")),
            )
        await store.finish(session_id, interrupted=False)
        return None

    @router.websocket("/v1/realtime", name="realtime_websocket")
    async def realtime(websocket: WebSocket) -> None:
        await gateway.handle(websocket)

    return router
