"""FastAPI routes for the stable v1 authentication contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.modules.auth.cache import RedisAuthCache
from app.modules.auth.google import (
    GoogleVerificationUnavailableError,
    InvalidGoogleTokenError,
)
from app.modules.auth.schemas import (
    AuthNonceResponse,
    AuthSessionResponse,
    GoogleAuthRequest,
    RefreshRequest,
    UserResponse,
)
from app.modules.auth.service import (
    AuthenticatedAccount,
    AuthRateLimitError,
    AuthService,
    AuthUnavailableError,
    InvalidCredentialsError,
    RefreshTokenExpiredError,
)
from app.modules.auth.tokens import ExpiredAccessTokenError, InvalidAccessTokenError

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
Authorization = Annotated[str | None, Header()]


class AuthApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.headers = headers or {}


@dataclass(frozen=True, slots=True)
class AuthRateLimits:
    window_seconds: int
    nonce: int
    exchange: int
    refresh: int
    authenticated: int


async def auth_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AuthApiError):
        raise exc
    request_id = str(request.state.request_id)
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
                "retryable": exc.retryable,
            }
        },
    )


def create_auth_router(
    service: AuthService, cache: RedisAuthCache, limits: AuthRateLimits
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["Auth"])

    async def enforce_rate(scope: str, identity: str, limit: int) -> None:
        try:
            result = await cache.check_rate_limit(
                scope, identity, limit=limit, window_seconds=limits.window_seconds
            )
        except RedisError as exc:
            raise _unavailable() from exc
        if not result.allowed:
            raise AuthApiError(
                429,
                "rate_limit_exceeded",
                "Too many requests.",
                retryable=True,
                headers={"Retry-After": str(result.retry_after)},
            )

    @router.post("/auth/google/nonce", status_code=201, response_model=AuthNonceResponse)
    async def create_nonce(request: Request, idempotency_key: IdempotencyKey) -> Any:
        del idempotency_key
        await enforce_rate("nonce-ip", _client_ip(request), limits.nonce)
        try:
            return await service.create_nonce()
        except (AuthUnavailableError, GoogleVerificationUnavailableError, SQLAlchemyError) as exc:
            raise _unavailable() from exc

    @router.post("/auth/google", response_model=AuthSessionResponse)
    async def exchange_google_token(
        payload: GoogleAuthRequest,
        request: Request,
        idempotency_key: IdempotencyKey,
    ) -> Any:
        del idempotency_key
        await enforce_rate("exchange-ip", _client_ip(request), limits.exchange)
        if payload.device.id is not None:
            await enforce_rate("exchange-device", str(payload.device.id), limits.exchange)
        try:
            return await service.exchange_google_token(
                id_token=payload.id_token,
                nonce=payload.nonce,
                device=payload.device,
            )
        except (AuthUnavailableError, GoogleVerificationUnavailableError, SQLAlchemyError) as exc:
            raise _unavailable() from exc
        except (InvalidGoogleTokenError, InvalidCredentialsError) as exc:
            raise _invalid_credentials() from exc

    @router.post("/auth/refresh", response_model=AuthSessionResponse)
    async def refresh_session(
        payload: RefreshRequest,
        request: Request,
        idempotency_key: IdempotencyKey,
    ) -> Any:
        del idempotency_key
        await enforce_rate("refresh-ip", _client_ip(request), limits.refresh)
        try:
            return await service.refresh(payload.refresh_token)
        except (AuthUnavailableError, SQLAlchemyError) as exc:
            raise _unavailable() from exc
        except AuthRateLimitError as exc:
            raise AuthApiError(
                429,
                "rate_limit_exceeded",
                "Too many requests.",
                retryable=True,
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        except RefreshTokenExpiredError as exc:
            raise AuthApiError(401, "token_expired", "The refresh token has expired.") from exc
        except InvalidCredentialsError as exc:
            raise _invalid_credentials() from exc

    @router.post("/auth/logout", status_code=204)
    async def logout(
        payload: RefreshRequest,
        request: Request,
        idempotency_key: IdempotencyKey,
        authorization: Authorization = None,
    ) -> Response:
        del idempotency_key
        await enforce_rate("logout-ip", _client_ip(request), limits.authenticated)
        account = await _authenticate(service, authorization)
        await enforce_rate("logout-user", str(account.user.id), limits.authenticated)
        try:
            await service.logout(account=account, refresh_token=payload.refresh_token)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        except InvalidCredentialsError as exc:
            raise _invalid_credentials() from exc
        return Response(status_code=204)

    @router.get("/me", response_model=UserResponse)
    async def me(request: Request, authorization: Authorization = None) -> Any:
        await enforce_rate("me-ip", _client_ip(request), limits.authenticated)
        account = await _authenticate(service, authorization)
        await enforce_rate("me-user", str(account.user.id), limits.authenticated)
        return service.user_response(account.user)

    return router


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise AuthApiError(401, "authentication_required", "Authentication is required.")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise AuthApiError(401, "authentication_required", "Authentication is required.")
    return token


async def _authenticate(service: AuthService, authorization: str | None) -> AuthenticatedAccount:
    token = _bearer_token(authorization)
    try:
        return await service.authenticate_access(token)
    except SQLAlchemyError as exc:
        raise _unavailable() from exc
    except ExpiredAccessTokenError as exc:
        raise AuthApiError(401, "token_expired", "The access token has expired.") from exc
    except (InvalidAccessTokenError, InvalidCredentialsError) as exc:
        raise _invalid_credentials() from exc


def _invalid_credentials() -> AuthApiError:
    return AuthApiError(401, "invalid_credentials", "The credentials are invalid.")


def _unavailable() -> AuthApiError:
    return AuthApiError(
        503,
        "service_unavailable",
        "Authentication is temporarily unavailable.",
        retryable=True,
    )
