"""Bearer -> web-session hand-off for the Android WebView (M24).

The signed-in app asks for a single-use code (``POST /v1/auth/web/handoff``); the WebView
then loads ``GET /v1/auth/web/handoff/redeem?code=...``, which turns the code into the normal
``mia_session`` cookie (M10) and redirects to an allow-listed same-origin path.

Only the SHA-256 of the code is stored (Redis, key TTL = code TTL, ``GETDEL`` = single use).
The landing path is bound to the code at issue time, so the redeem URL carries nothing else and
cannot be turned into an open redirect.

M70 (ADR-0016 §3): the code also carries the device of the app session that asked for it; the
web session created on redeem is bound to that device (``web_sessions.device_id``), so the
WebView's requests count as requests from the phone itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.models import AuthSession, Device
from app.modules.auth.repositories import AuthRepository
from app.modules.auth.service import WebSignIn
from app.modules.auth.tokens import TokenManager

CODE_TTL_SECONDS = 60
DEFAULT_LANDING_PATH = "/chat"
# First path segments of the web app the hand-off may land on (web/src/App.tsx and M31 pages).
ALLOWED_LANDING_ROOTS = frozenset(
    {
        "chat",
        "store",
        "installed",
        "models",
        "approvals",
        "dashboard",
        "memory",
        "inbox",
        "data",
        "more",
    }
)
MAX_LANDING_LENGTH = 512
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*$")
_SAFE_QUERY = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=:@/?-]*$")


class WebHandoffStore(Protocol):
    async def set_if_absent(self, key: str, value: str, *, ttl_seconds: int) -> bool: ...

    async def consume(self, key: str) -> str | None: ...


class HandoffRedis(WebHandoffStore, Protocol):
    async def increment_with_expiry(self, key: str, *, ttl_seconds: int) -> tuple[int, int]: ...


@dataclass(frozen=True, slots=True)
class IssuedHandoff:
    code: str
    """Opaque one-time value; only its hash is stored. Never log it."""
    expires_at: datetime
    landing_path: str


class InvalidLandingPathError(ValueError):
    """The requested landing path is not a same-origin allow-listed page."""


def normalize_landing_path(path: str | None) -> str:
    """Return ``path`` if it is an allow-listed same-origin location, else raise.

    Accepts ``/root[/more][?query]`` where ``root`` is in :data:`ALLOWED_LANDING_ROOTS`. Rejects
    schemes, hosts, ``//``, backslashes, control characters, dot segments and fragments.
    """
    if path is None or path == "":
        return DEFAULT_LANDING_PATH
    if len(path) > MAX_LANDING_LENGTH:
        raise InvalidLandingPathError("landing path is too long")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment or "#" in path or "\\" in path:
        raise InvalidLandingPathError("landing path must be a plain same-origin path")
    location = parts.path
    if not _SAFE_PATH.fullmatch(location) or location.startswith("//"):
        raise InvalidLandingPathError("landing path has unsupported characters")
    segments = location.split("/")[1:]
    if any(segment in {".", ".."} or segment.lower().startswith("%2e") for segment in segments):
        raise InvalidLandingPathError("landing path has dot segments")
    if "%2f" in location.lower() or "%5c" in location.lower():
        raise InvalidLandingPathError("landing path has encoded separators")
    if segments[0] not in ALLOWED_LANDING_ROOTS:
        raise InvalidLandingPathError("landing page is not allowed")
    if parts.query and not _SAFE_QUERY.fullmatch(parts.query):
        raise InvalidLandingPathError("landing query has unsupported characters")
    return location + (f"?{parts.query}" if parts.query else "")


def _key(code: str) -> str:
    return "auth:web-handoff:" + hashlib.sha256(code.encode()).hexdigest()


class WebHandoffService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: WebHandoffStore,
        tokens: TokenManager,
        *,
        code_ttl_seconds: int = CODE_TTL_SECONDS,
        web_session_ttl_days: int = 30,
        max_web_sessions_per_user: int = 32,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._tokens = tokens
        self._code_ttl_seconds = code_ttl_seconds
        self._web_session_ttl = timedelta(days=web_session_ttl_days)
        self._max_web_sessions = max(1, max_web_sessions_per_user)

    async def session_device(self, user_id: UUID, family_id: UUID) -> UUID | None:
        """Device of the app's refresh-token family (the phone asking for a WebView page)."""
        async with self._session_factory() as database_session:
            device = await database_session.scalar(
                select(AuthSession.device_id)
                .where(AuthSession.family_id == family_id, AuthSession.user_id == user_id)
                .limit(1)
            )
        return device

    async def issue(
        self,
        user_id: UUID,
        landing_path: str | None,
        *,
        now: datetime | None = None,
        device_id: UUID | None = None,
    ) -> IssuedHandoff:
        landing = normalize_landing_path(landing_path)
        current_time = now or datetime.now(UTC)
        body = {"u": str(user_id), "p": landing}
        if device_id is not None:
            body["d"] = str(device_id)
        payload = json.dumps(body, separators=(",", ":"))
        for _ in range(3):
            code = secrets.token_urlsafe(32)
            if await self._store.set_if_absent(
                _key(code), payload, ttl_seconds=self._code_ttl_seconds
            ):
                return IssuedHandoff(
                    code=code,
                    expires_at=current_time + timedelta(seconds=self._code_ttl_seconds),
                    landing_path=landing,
                )
        raise RuntimeError("could not store a hand-off code")

    async def redeem(
        self, code: str, *, now: datetime | None = None
    ) -> tuple[WebSignIn, str] | None:
        """Consume ``code`` once; return the new web session and landing path, or ``None``."""
        if not 20 <= len(code) <= 128:
            return None
        raw = await self._store.consume(_key(code))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            user_id = UUID(data["u"])
            landing = normalize_landing_path(data["p"])
            device_id = UUID(data["d"]) if data.get("d") else None
        except (ValueError, KeyError, TypeError):
            return None
        current_time = now or datetime.now(UTC)
        expires_at = current_time + self._web_session_ttl
        session_token = self._tokens.new_refresh_token()
        async with self._session_factory() as database_session, database_session.begin():
            repository = AuthRepository(database_session)
            user = await repository.get_user(user_id)
            if user is None:
                return None
            await repository.prune_web_sessions(
                user_id=user.id, keep=self._max_web_sessions - 1, now=current_time
            )
            web_session = await repository.create_web_session(
                user_id=user.id,
                token_hash=self._tokens.hash_web_session_token(session_token),
                expires_at=expires_at,
                now=current_time,
            )
            if device_id is not None:
                owned = await database_session.scalar(
                    select(Device.id).where(Device.id == device_id, Device.user_id == user.id)
                )
                web_session.device_id = owned
        return WebSignIn(session_token=session_token, user=user, expires_at=expires_at), landing
