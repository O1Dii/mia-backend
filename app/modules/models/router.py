"""``/v1/model-connections``: CRUD without ever returning a key, "Test API", plan sign-in."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.modules.models.catalog import ConnectionValidationError
from app.modules.models.chatgpt_plan.credentials import PlanCredentialStore
from app.modules.models.chatgpt_plan.signin import SignInCoordinator
from app.modules.models.connections import (
    ConnectionConflictError,
    ConnectionNotFoundError,
    ConnectionService,
)
from app.modules.models.gateway import DefaultModelGateway
from app.modules.models.schemas import (
    ConnectionListResponse,
    ConnectionResponse,
    CreateConnectionRequest,
    SignInStartResponse,
    SignInStatusResponse,
    TestResultResponse,
    UpdateConnectionRequest,
)
from app.modules.realtime.sessions import AuthenticatedUser

TEST_COOLDOWN_SECONDS = 5.0
Authorization = Annotated[str | None, Header()]


class Authenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser | None: ...


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def _error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        headers={"X-Request-ID": request_id, **(headers or {})},
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "retryable": retryable,
            }
        },
    )


class _ApiError(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


class ModelRoute(APIRoute):
    """Validation failures use the v1 envelope and never echo the (secret-bearing) input."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return _error(request, 422, "validation_error", "The request is invalid.")
            except _ApiError as failure:
                return failure.response

        return handle


def build_models_router(
    *,
    authenticator: Authenticator,
    connections: ConnectionService,
    gateway: DefaultModelGateway,
    plan_store: PlanCredentialStore | None = None,
    sign_in: SignInCoordinator | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> APIRouter:
    router = APIRouter(prefix="/v1/model-connections", tags=["Models"], route_class=ModelRoute)
    last_test: dict[uuid.UUID, float] = {}

    async def current_user(request: Request, authorization: str | None) -> uuid.UUID:
        scheme, _, token = (authorization or "").partition(" ")
        user = (
            await authenticator.authenticate(token)
            if scheme.lower() == "bearer" and token
            else None
        )
        if user is None:
            raise _ApiError(
                _error(
                    request,
                    401,
                    "unauthenticated",
                    "Authentication is required.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            )
        return user.user_id

    def not_found(request: Request) -> _ApiError:
        return _ApiError(
            _error(request, 404, "connection_not_found", "The connection was not found.")
        )

    def invalid(request: Request, exc: ConnectionValidationError) -> _ApiError:
        return _ApiError(_error(request, 422, "invalid_connection", str(exc)))

    @router.get("", response_model=ConnectionListResponse)
    async def list_connections(request: Request, authorization: Authorization = None) -> Any:
        user_id = await current_user(request, authorization)
        return ConnectionListResponse(
            items=[ConnectionResponse.of(v) for v in await connections.list_connections(user_id)]
        )

    @router.post("", status_code=201, response_model=ConnectionResponse)
    async def create_connection(
        payload: CreateConnectionRequest, request: Request, authorization: Authorization = None
    ) -> Any:
        user_id = await current_user(request, authorization)
        try:
            view = await connections.create(
                user_id,
                provider=payload.provider,
                label=payload.label,
                model=payload.model,
                api_key=payload.api_key.get_secret_value() if payload.api_key else None,
                base_url=payload.base_url,
                capability=payload.capability,
                make_primary=payload.make_primary,
            )
        except ConnectionValidationError as exc:
            raise invalid(request, exc) from None
        except ConnectionConflictError:
            raise _ApiError(
                _error(request, 409, "connection_conflict", "The connection conflicts.")
            ) from None
        return ConnectionResponse.of(view)

    # Sign in with ChatGPT (registered before the ``/{connection_id}`` routes).
    @router.post("/chatgpt-plan/sign-in", status_code=201, response_model=SignInStartResponse)
    async def start_sign_in(request: Request, authorization: Authorization = None) -> Any:
        user_id = await current_user(request, authorization)
        if sign_in is None:
            raise _ApiError(
                _error(request, 404, "sign_in_unavailable", "ChatGPT sign-in is not enabled.")
            )
        attempt = await sign_in.start(user_id)
        return SignInStartResponse(
            attempt_id=attempt.id,
            authorize_url=attempt.authorize_url,
            expires_at=datetime.fromtimestamp(attempt.expires_at, UTC),
        )

    @router.get("/chatgpt-plan/sign-in/{attempt_id}", response_model=SignInStatusResponse)
    async def sign_in_status(
        attempt_id: str, request: Request, authorization: Authorization = None
    ) -> Any:
        user_id = await current_user(request, authorization)
        attempt = sign_in.status(user_id, attempt_id) if sign_in is not None else None
        if attempt is None:
            raise _ApiError(_error(request, 404, "attempt_not_found", "The attempt was not found."))
        return SignInStatusResponse(
            attempt_id=attempt.id,
            status=attempt.status,
            error_code=attempt.error_code,
            connection_id=attempt.connection_id,
        )

    @router.get("/{connection_id}", response_model=ConnectionResponse)
    async def get_connection(
        connection_id: uuid.UUID, request: Request, authorization: Authorization = None
    ) -> Any:
        user_id = await current_user(request, authorization)
        try:
            return ConnectionResponse.of(await connections.get(user_id, connection_id))
        except ConnectionNotFoundError:
            raise not_found(request) from None

    @router.patch("/{connection_id}", response_model=ConnectionResponse)
    async def update_connection(
        connection_id: uuid.UUID,
        payload: UpdateConnectionRequest,
        request: Request,
        authorization: Authorization = None,
    ) -> Any:
        user_id = await current_user(request, authorization)
        try:
            view = await connections.update(
                user_id,
                connection_id,
                label=payload.label,
                model=payload.model,
                api_key=payload.api_key.get_secret_value() if payload.api_key else None,
                base_url=payload.base_url,
                capability=payload.capability,
                make_primary=payload.make_primary,
            )
        except ConnectionNotFoundError:
            raise not_found(request) from None
        except ConnectionValidationError as exc:
            raise invalid(request, exc) from None
        except ConnectionConflictError:
            raise _ApiError(
                _error(request, 409, "connection_conflict", "The connection conflicts.")
            ) from None
        return ConnectionResponse.of(view)

    @router.delete("/{connection_id}", status_code=204)
    async def delete_connection(
        connection_id: uuid.UUID, request: Request, authorization: Authorization = None
    ) -> Response:
        user_id = await current_user(request, authorization)
        try:
            view = await connections.get(user_id, connection_id)
            if view.provider == "chatgpt-plan" and plan_store is not None:
                await plan_store.sign_out(user_id, connection_id)
            await connections.delete(user_id, connection_id)
        except ConnectionNotFoundError:
            raise not_found(request) from None
        return Response(status_code=204)

    @router.post("/{connection_id}/test", response_model=TestResultResponse)
    async def test_connection(
        connection_id: uuid.UUID, request: Request, authorization: Authorization = None
    ) -> Any:
        user_id = await current_user(request, authorization)
        try:
            await connections.get(user_id, connection_id)
        except ConnectionNotFoundError:
            raise not_found(request) from None
        now = clock()
        previous = last_test.get(connection_id)
        if previous is not None and now - previous < TEST_COOLDOWN_SECONDS:
            raise _ApiError(
                _error(
                    request,
                    429,
                    "rate_limited",
                    "Wait a few seconds before testing again.",
                    retryable=True,
                    headers={"Retry-After": str(int(TEST_COOLDOWN_SECONDS))},
                )
            )
        last_test[connection_id] = now
        result = await gateway.probe(user_id, connection_id)
        return TestResultResponse(
            ok=result.ok, latency_ms=result.latency_ms, error_code=result.error_code
        )

    @router.post("/{connection_id}/resume", response_model=ConnectionResponse)
    async def resume_connection(
        connection_id: uuid.UUID, request: Request, authorization: Authorization = None
    ) -> Any:
        user_id = await current_user(request, authorization)
        try:
            return ConnectionResponse.of(await connections.resume(user_id, connection_id))
        except ConnectionNotFoundError:
            raise not_found(request) from None

    @router.post("/{connection_id}/sign-out", response_model=ConnectionResponse)
    async def sign_out(
        connection_id: uuid.UUID, request: Request, authorization: Authorization = None
    ) -> Any:
        user_id = await current_user(request, authorization)
        try:
            view = await connections.get(user_id, connection_id)
        except ConnectionNotFoundError:
            raise not_found(request) from None
        if view.provider != "chatgpt-plan" or plan_store is None:
            raise _ApiError(
                _error(
                    request, 422, "invalid_connection", "Only a ChatGPT plan connection signs out."
                )
            )
        await plan_store.sign_out(user_id, connection_id)
        return ConnectionResponse.of(await connections.get(user_id, connection_id))

    return router
