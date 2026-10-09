"""One request principal for both first-party transports.

Android sends ``Authorization: Bearer <access JWT>``; the web sends the
HttpOnly ``mia_session`` cookie. Both resolve to the same ``users.id`` because
both sign-ins upsert the user by the verified Google ``sub``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from app.modules.auth.service import AuthService, InvalidCredentialsError
from app.modules.auth.tokens import ExpiredAccessTokenError, InvalidAccessTokenError
from app.modules.users.models import User

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AuthenticationFailure(Exception):
    """Raised by :class:`RequestAuthenticator`; the router maps it to the error envelope."""

    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class WebCookiePolicy:
    """Browser session cookie settings (``Secure``/``HttpOnly``/``SameSite=Strict``)."""

    secure: bool = True
    session_cookie: str = "mia_session"
    nonce_cookie: str = "mia_web_nonce"
    path: str = "/"
    session_ttl_days: int = 30
    trusted_origins: frozenset[str] = field(default_factory=frozenset)
    """Extra origins (``https://host``) allowed to send cookie-authenticated mutations."""

    @property
    def base_path(self) -> str:
        """Web path prefix the cookies are scoped to (``""`` or e.g. ``"/mia"``, M62-T1).

        Browser redirects to web pages (hand-off landing, OAuth return) are prefixed with it.
        """
        return self.path.rstrip("/")


@dataclass(frozen=True, slots=True)
class RequestPrincipal:
    """The authenticated MIA user of one request.

    ``user_id`` is ``str(users.id)`` — the string form every port expects
    (``ToolContext.user_id``). ``session_id`` is the refresh family (bearer) or
    the web session id (cookie).
    """

    user_id: str
    method: Literal["bearer", "cookie"]
    session_id: str
    user: User


class RequestAuthenticator:
    """Resolve bearer first (explicit), then the web session cookie."""

    def __init__(self, service: AuthService, policy: WebCookiePolicy | None = None) -> None:
        self._service = service
        self._policy = policy or WebCookiePolicy()

    @property
    def policy(self) -> WebCookiePolicy:
        return self._policy

    async def authenticate(self, request: Request) -> RequestPrincipal:
        authorization = request.headers.get("authorization")
        if authorization is not None:
            return await self._bearer(authorization)
        cookie = request.cookies.get(self._policy.session_cookie)
        if cookie:
            self.enforce_same_origin(request)
            return await self._cookie(cookie)
        raise AuthenticationFailure(401, "authentication_required", "Authentication is required.")

    def enforce_same_origin(self, request: Request) -> None:
        """Reject cross-site cookie-authenticated mutations (CSRF defence in depth).

        ``SameSite=Strict`` already withholds the cookie cross-site; this also
        refuses an ``Origin`` or ``Sec-Fetch-Site`` that is not same-origin.
        """
        if request.method.upper() in SAFE_METHODS:
            return
        origin = request.headers.get("origin")
        if origin is not None and origin in self._policy.trusted_origins:
            return
        if request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
            raise _csrf_rejected()
        if origin is None:
            return
        # Compare host:port only: a TLS-terminating proxy may present http to the app.
        host = request.headers.get("host", request.url.netloc)
        if urlsplit(origin).netloc.lower() != host.lower():
            raise _csrf_rejected()

    async def _bearer(self, authorization: str) -> RequestPrincipal:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise AuthenticationFailure(
                401, "authentication_required", "Authentication is required."
            )
        try:
            account = await self._service.authenticate_access(token)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        except ExpiredAccessTokenError as exc:
            raise AuthenticationFailure(
                401, "token_expired", "The access token has expired."
            ) from exc
        except (InvalidAccessTokenError, InvalidCredentialsError) as exc:
            raise _invalid() from exc
        return RequestPrincipal(
            user_id=str(account.user.id),
            method="bearer",
            session_id=str(account.principal.session_id),
            user=account.user,
        )

    async def _cookie(self, cookie: str) -> RequestPrincipal:
        if len(cookie) > 256:
            raise _invalid()
        try:
            session = await self._service.authenticate_web_session(cookie)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        except InvalidCredentialsError as exc:
            raise _invalid() from exc
        return RequestPrincipal(
            user_id=str(session.user.id),
            method="cookie",
            session_id=str(session.session_id),
            user=session.user,
        )


def _csrf_rejected() -> AuthenticationFailure:
    return AuthenticationFailure(403, "permission_denied", "Cross-site request rejected.")


def _invalid() -> AuthenticationFailure:
    return AuthenticationFailure(401, "invalid_credentials", "The credentials are invalid.")


def _unavailable() -> AuthenticationFailure:
    return AuthenticationFailure(
        503, "service_unavailable", "Authentication is temporarily unavailable.", retryable=True
    )
