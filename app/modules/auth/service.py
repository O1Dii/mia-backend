"""Authentication transaction boundaries and rotation policy."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.cache import RedisAuthCache
from app.modules.auth.google import GoogleVerifier
from app.modules.auth.repositories import (
    AuthRepository,
    DeletedGoogleIdentityError,
    DeviceOwnershipError,
)
from app.modules.auth.schemas import (
    AuthNonceResponse,
    AuthSessionResponse,
    DeviceInput,
    UserResponse,
)
from app.modules.auth.tokens import AccessPrincipal, TokenManager
from app.modules.users.models import User


class InvalidCredentialsError(ValueError):
    pass


class RefreshTokenExpiredError(InvalidCredentialsError):
    pass


class RefreshTokenReuseError(InvalidCredentialsError):
    pass


class AuthUnavailableError(RuntimeError):
    pass


class AuthRateLimitError(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("authentication rate limit exceeded")


@dataclass(frozen=True, slots=True)
class AuthenticatedAccount:
    principal: AccessPrincipal
    user: User


class AuthService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cache: RedisAuthCache,
        verifier: GoogleVerifier,
        tokens: TokenManager,
        *,
        google_client_id: str | None,
        nonce_ttl_seconds: int = 300,
        refresh_ttl_days: int = 30,
        rate_window_seconds: int = 60,
        refresh_rate_limit: int = 30,
    ) -> None:
        self._session_factory = session_factory
        self._cache = cache
        self._verifier = verifier
        self._tokens = tokens
        self._google_client_id = google_client_id
        self._nonce_ttl_seconds = nonce_ttl_seconds
        self._refresh_ttl = timedelta(days=refresh_ttl_days)
        self._rate_window_seconds = rate_window_seconds
        self._refresh_rate_limit = refresh_rate_limit

    async def create_nonce(self, *, now: datetime | None = None) -> AuthNonceResponse:
        current_time = now or datetime.now(UTC)
        try:
            for _ in range(3):
                nonce = secrets.token_urlsafe(32)
                if await self._cache.store_nonce(nonce, ttl_seconds=self._nonce_ttl_seconds):
                    return AuthNonceResponse(
                        nonce=nonce,
                        expires_at=current_time + timedelta(seconds=self._nonce_ttl_seconds),
                    )
        except RedisError as exc:
            raise AuthUnavailableError("authentication nonce storage is unavailable") from exc
        raise AuthUnavailableError("could not allocate a unique authentication nonce")

    async def exchange_google_token(
        self,
        *,
        id_token: str,
        nonce: str,
        device: DeviceInput,
        now: datetime | None = None,
    ) -> AuthSessionResponse:
        if not self._google_client_id:
            raise AuthUnavailableError("Google authentication is not configured")
        identity = await self._verifier.verify(
            id_token, audience=self._google_client_id, nonce=nonce
        )
        try:
            consumed = await self._cache.consume_nonce(nonce)
        except RedisError as exc:
            raise AuthUnavailableError("authentication nonce storage is unavailable") from exc
        if not consumed:
            raise InvalidCredentialsError("authentication nonce is invalid or already used")
        current_time = now or datetime.now(UTC)
        refresh_token = self._tokens.new_refresh_token()
        try:
            async with self._session_factory() as database_session, database_session.begin():
                repository = AuthRepository(database_session)
                user = await repository.upsert_user(
                    google_sub=identity.subject,
                    email=identity.email,
                    email_verified=identity.email_verified,
                    display_name=identity.display_name,
                    avatar_url=identity.avatar_url,
                    locale=identity.locale,
                    now=current_time,
                )
                persisted_device = await repository.upsert_device(
                    user_id=user.id, supplied=device, now=current_time
                )
                auth_session = await repository.create_session(
                    user_id=user.id,
                    device_id=persisted_device.id,
                    refresh_token_hash=self._tokens.hash_refresh_token(refresh_token),
                    expires_at=current_time + self._refresh_ttl,
                )
        except (DeletedGoogleIdentityError, DeviceOwnershipError) as exc:
            raise InvalidCredentialsError("credentials cannot be used") from exc
        return self._response(user, auth_session.family_id, refresh_token, now=current_time)

    async def refresh(
        self, refresh_token: str, *, now: datetime | None = None
    ) -> AuthSessionResponse:
        current_time = now or datetime.now(UTC)
        refresh_hash = self._tokens.hash_refresh_token(refresh_token)
        next_refresh_token = self._tokens.new_refresh_token()
        failure: InvalidCredentialsError | None = None
        user: User | None = None
        replacement_family_id: UUID | None = None
        async with self._session_factory() as database_session, database_session.begin():
            repository = AuthRepository(database_session)
            previous = await repository.get_session_by_refresh_hash(refresh_hash, for_update=True)
            if previous is None:
                failure = InvalidCredentialsError("refresh token is invalid")
            elif previous.revoked_at is not None or previous.replaced_by_session_id is not None:
                await repository.revoke_family(previous.family_id, now=current_time)
                await repository.mark_reuse(previous, now=current_time)
                failure = RefreshTokenReuseError("refresh token reuse was detected")
            elif previous.expires_at <= current_time:
                previous.revoked_at = current_time
                failure = RefreshTokenExpiredError("refresh token has expired")
            else:
                await self._limit_refresh("refresh-user", str(previous.user_id))
                await self._limit_refresh("refresh-device", str(previous.device_id))
                user = await repository.get_user(previous.user_id)
                if user is None:
                    await repository.revoke_family(previous.family_id, now=current_time)
                    failure = InvalidCredentialsError("refresh token is invalid")
                else:
                    replacement = await repository.create_session(
                        user_id=previous.user_id,
                        device_id=previous.device_id,
                        family_id=previous.family_id,
                        generation=previous.generation + 1,
                        refresh_token_hash=self._tokens.hash_refresh_token(next_refresh_token),
                        expires_at=current_time + self._refresh_ttl,
                    )
                    previous.revoked_at = current_time
                    previous.last_used_at = current_time
                    previous.replaced_by_session_id = replacement.id
                    replacement_family_id = replacement.family_id
        if failure is not None:
            raise failure
        if user is None or replacement_family_id is None:
            raise InvalidCredentialsError("refresh token is invalid")
        return self._response(user, replacement_family_id, next_refresh_token, now=current_time)

    async def _limit_refresh(self, scope: str, identity: str) -> None:
        try:
            result = await self._cache.check_rate_limit(
                scope,
                identity,
                limit=self._refresh_rate_limit,
                window_seconds=self._rate_window_seconds,
            )
        except RedisError as exc:
            raise AuthUnavailableError("authentication rate storage is unavailable") from exc
        if not result.allowed:
            raise AuthRateLimitError(result.retry_after)

    async def authenticate_access(
        self, access_token: str, *, now: datetime | None = None
    ) -> AuthenticatedAccount:
        current_time = now or datetime.now(UTC)
        principal = self._tokens.verify_access(access_token, now=current_time)
        async with self._session_factory() as database_session:
            repository = AuthRepository(database_session)
            if not await repository.family_is_active(
                family_id=principal.session_id, user_id=principal.user_id, now=current_time
            ):
                raise InvalidCredentialsError("access session is inactive")
            user = await repository.get_user(principal.user_id)
            if user is None:
                raise InvalidCredentialsError("access session is inactive")
        return AuthenticatedAccount(principal=principal, user=user)

    async def logout(
        self, *, account: AuthenticatedAccount, refresh_token: str, now: datetime | None = None
    ) -> None:
        current_time = now or datetime.now(UTC)
        refresh_hash = self._tokens.hash_refresh_token(refresh_token)
        async with self._session_factory() as database_session, database_session.begin():
            repository = AuthRepository(database_session)
            session = await repository.get_session_by_refresh_hash(refresh_hash, for_update=True)
            if (
                session is None
                or session.user_id != account.principal.user_id
                or session.family_id != account.principal.session_id
            ):
                raise InvalidCredentialsError("refresh token is invalid")
            await repository.revoke_family(session.family_id, now=current_time)

    @staticmethod
    def user_response(user: User) -> UserResponse:
        return UserResponse(
            id=user.id,
            email=user.email,
            email_verified=user.email_verified,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            locale=user.locale,
            timezone=user.timezone,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

    def _response(
        self, user: User, family_id: UUID, refresh_token: str, *, now: datetime
    ) -> AuthSessionResponse:
        access_token, expires_in = self._tokens.issue_access(user.id, family_id, now=now)
        return AuthSessionResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            user=self.user_response(user),
        )
