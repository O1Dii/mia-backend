"""``/v1/auth/web/handoff*``: sign the Android WebView in with the app's bearer (M24).

``POST`` is bearer-only and returns a single-use code (<= 60 s). ``GET .../redeem`` is called by
the WebView itself (no credentials): it consumes the code, sets the normal ``mia_session``
cookie and redirects (303) to the allow-listed path that was bound to the code.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.modules.auth.composition import request_authenticator
from app.modules.auth.principal import RequestAuthenticator
from app.modules.auth.router import AuthApiError
from app.modules.auth.tokens import TokenManager
from app.modules.auth.web_handoff import (
    HandoffRedis,
    InvalidLandingPathError,
    WebHandoffService,
)

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
REDEEM_PATH = "/v1/auth/web/handoff/redeem"
RATE_WINDOW_SECONDS = 60


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str | None = Field(default=None, max_length=2048)


def build_web_handoff_router(
    service: WebHandoffService,
    redis: HandoffRedis,
    authenticator: RequestAuthenticator,
    *,
    secure: bool,
    session_cookie: str,
    cookie_path: str,
    issue_limit: int,
    redeem_limit: int,
) -> APIRouter:
    router = APIRouter(prefix="/v1/auth/web/handoff", tags=["Auth"])

    async def enforce(scope: str, identity: str, limit: int) -> None:
        digest = hashlib.sha256(identity.encode()).hexdigest()
        try:
            count, retry_after = await redis.increment_with_expiry(
                f"auth:rate:{scope}:{digest}", ttl_seconds=RATE_WINDOW_SECONDS
            )
        except RedisError as exc:
            raise _unavailable() from exc
        if count > limit:
            raise AuthApiError(
                429,
                "rate_limit_exceeded",
                "Too many requests.",
                retryable=True,
                headers={"Retry-After": str(retry_after)},
            )

    @router.post("", status_code=201)
    async def issue_code(
        request: Request,
        idempotency_key: IdempotencyKey,
        payload: HandoffRequest | None = None,
    ) -> JSONResponse:
        """Bearer only: a cookie session needs no hand-off, and a cookie must not mint codes."""
        del idempotency_key
        await enforce("handoff-issue-ip", _client_ip(request), issue_limit)
        principal = await authenticator.authenticate(request)
        if principal.method != "bearer":
            raise AuthApiError(403, "permission_denied", "A bearer session is required.")
        await enforce("handoff-issue-user", principal.user_id, issue_limit)
        try:
            issued = await service.issue(UUID(principal.user_id), payload.path if payload else None)
        except InvalidLandingPathError as exc:
            raise AuthApiError(
                400, "web_handoff_path_invalid", "The landing path is not allowed."
            ) from exc
        except (RedisError, SQLAlchemyError, RuntimeError) as exc:
            raise _unavailable() from exc
        body: dict[str, Any] = {
            "code": issued.code,
            "expires_at": issued.expires_at.isoformat().replace("+00:00", "Z"),
            "redeem_path": f"{REDEEM_PATH}?code={quote(issued.code, safe='')}",
        }
        return JSONResponse(status_code=201, content=body, headers=_no_store())

    @router.get("/redeem")
    async def redeem_code(
        request: Request, code: Annotated[str, Query(min_length=1, max_length=256)]
    ) -> RedirectResponse:
        await enforce("handoff-redeem-ip", _client_ip(request), redeem_limit)
        try:
            redeemed = await service.redeem(code)
        except (RedisError, SQLAlchemyError) as exc:
            raise _unavailable() from exc
        if redeemed is None:
            raise AuthApiError(
                401, "web_handoff_invalid", "The sign-in code is invalid or has expired."
            )
        signed_in, landing = redeemed
        response = RedirectResponse(landing, status_code=303, headers=_no_store())
        response.set_cookie(
            session_cookie,
            signed_in.session_token,
            max_age=max(1, int((signed_in.expires_at - datetime.now(UTC)).total_seconds())),
            httponly=True,
            secure=secure,
            samesite="strict",
            path=cookie_path,
        )
        return response

    return router


def install_web_handoff(
    app: FastAPI,
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis: HandoffRedis,
) -> WebHandoffService:
    """Mount after ``install_auth``; reuses its request authenticator and cookie policy."""
    authenticator = request_authenticator(app)
    policy = authenticator.policy
    service = WebHandoffService(
        session_factory,
        redis,
        TokenManager(
            settings.auth_jwt_secret.get_secret_value(),
            access_ttl_seconds=settings.auth_access_token_ttl_seconds,
        ),
        web_session_ttl_days=policy.session_ttl_days,
    )
    app.include_router(
        build_web_handoff_router(
            service,
            redis,
            authenticator,
            secure=policy.secure,
            session_cookie=policy.session_cookie,
            cookie_path=policy.path,
            issue_limit=settings.auth_authenticated_rate_limit,
            redeem_limit=settings.auth_exchange_rate_limit,
        )
    )
    return service


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _no_store() -> dict[str, str]:
    return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _unavailable() -> AuthApiError:
    return AuthApiError(
        503, "service_unavailable", "Authentication is temporarily unavailable.", retryable=True
    )
