"""Auth mounting hook for the shared application composition owner."""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.errors import validation_error
from app.core.config import Settings
from app.core.database import Database
from app.core.redis import RedisClient
from app.modules.auth.cache import RedisAuthCache
from app.modules.auth.google import GoogleIdTokenVerifier
from app.modules.auth.principal import AuthenticationFailure, RequestAuthenticator, WebCookiePolicy
from app.modules.auth.router import (
    AuthApiError,
    AuthRateLimits,
    auth_error_handler,
    create_auth_router,
)
from app.modules.auth.service import AuthService
from app.modules.auth.tokens import TokenManager


def install_auth(
    app: FastAPI,
    settings: Settings,
    *,
    database: Database,
    redis: RedisClient,
    service: AuthService | None = None,
    cache: RedisAuthCache | None = None,
    web_policy: WebCookiePolicy | None = None,
) -> AuthService:
    """Mount before startup; share the application's owned DB/Redis lifecycle.

    The request authenticator (bearer or web cookie) is available afterwards via
    :func:`request_authenticator` for other modules' routers.
    """
    # Under a proxy prefix (PUBLIC_BASE_URL=https://host/mia) the cookies are scoped to "/mia/".
    policy = web_policy or WebCookiePolicy(
        secure=settings.app_env != "test", path=settings.public_path_prefix + "/"
    )
    auth_cache = cache or RedisAuthCache(redis)
    auth_service = service or AuthService(
        database.session_factory,
        auth_cache,
        GoogleIdTokenVerifier(),
        TokenManager(
            settings.auth_jwt_secret.get_secret_value(),
            access_ttl_seconds=settings.auth_access_token_ttl_seconds,
        ),
        google_client_id=settings.google_client_id,
        nonce_ttl_seconds=settings.auth_nonce_ttl_seconds,
        refresh_ttl_days=settings.auth_refresh_token_ttl_days,
        rate_window_seconds=settings.auth_rate_limit_window_seconds,
        refresh_rate_limit=settings.auth_refresh_rate_limit,
        web_session_ttl_days=policy.session_ttl_days,
    )
    authenticator = RequestAuthenticator(auth_service, policy)
    app.add_exception_handler(AuthApiError, auth_error_handler)
    app.add_exception_handler(AuthenticationFailure, auth_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.include_router(
        create_auth_router(
            auth_service,
            auth_cache,
            AuthRateLimits(
                window_seconds=settings.auth_rate_limit_window_seconds,
                nonce=settings.auth_nonce_rate_limit,
                exchange=settings.auth_exchange_rate_limit,
                refresh=settings.auth_refresh_rate_limit,
                authenticated=settings.auth_authenticated_rate_limit,
            ),
            authenticator=authenticator,
        )
    )
    app.state.request_authenticator = authenticator
    return auth_service


def request_authenticator(app: FastAPI) -> RequestAuthenticator:
    """Return the authenticator mounted by :func:`install_auth` (raises if not mounted)."""
    authenticator = getattr(app.state, "request_authenticator", None)
    if not isinstance(authenticator, RequestAuthenticator):
        raise RuntimeError("install_auth must run before modules that need authentication")
    return authenticator
