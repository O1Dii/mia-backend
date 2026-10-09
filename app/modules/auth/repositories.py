"""Caller-transaction-neutral auth persistence."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Select, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import AuthSession, Device, WebSession
from app.modules.auth.schemas import DeviceInput
from app.modules.users.models import User


class DeviceOwnershipError(ValueError):
    pass


class DeletedGoogleIdentityError(ValueError):
    pass


class AuthRepository:
    """Persist auth state while the service owns commit boundaries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_user(
        self,
        *,
        google_sub: str,
        email: str,
        email_verified: bool,
        display_name: str | None,
        avatar_url: str | None,
        locale: str,
        now: datetime,
    ) -> User:
        statement = (
            insert(User)
            .values(
                google_sub=google_sub,
                email=email,
                email_verified=email_verified,
                display_name=display_name,
                avatar_url=avatar_url,
                locale=locale,
                timezone="UTC",
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_users_google_sub",
                set_={
                    "email": email,
                    "email_verified": email_verified,
                    "display_name": display_name,
                    "avatar_url": avatar_url,
                    "locale": locale,
                    "updated_at": now,
                },
                where=User.deleted_at.is_(None),
            )
            .returning(User)
        )
        user = (await self._session.scalars(statement)).one_or_none()
        if user is None:
            raise DeletedGoogleIdentityError("deleted accounts cannot sign in")
        return user

    async def upsert_device(self, *, user_id: UUID, supplied: DeviceInput, now: datetime) -> Device:
        statement = (
            insert(Device)
            .values(
                id=supplied.id or uuid4(),
                user_id=user_id,
                platform=supplied.platform,
                app_version=supplied.app_version,
                device_name=supplied.device_name,
                capabilities=supplied.capabilities,
                last_seen_at=now,
            )
            .on_conflict_do_update(
                index_elements=[Device.id],
                set_={
                    "app_version": supplied.app_version,
                    "device_name": supplied.device_name,
                    "capabilities": supplied.capabilities,
                    "last_seen_at": now,
                },
                where=Device.user_id == user_id,
            )
            .returning(Device)
        )
        device = (await self._session.scalars(statement)).one_or_none()
        if device is None:
            raise DeviceOwnershipError("device belongs to another user")
        return device

    async def create_session(
        self,
        *,
        user_id: UUID,
        device_id: UUID,
        refresh_token_hash: str,
        expires_at: datetime,
        family_id: UUID | None = None,
        generation: int = 0,
    ) -> AuthSession:
        session = AuthSession(
            family_id=family_id or uuid4(),
            user_id=user_id,
            device_id=device_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
            generation=generation,
        )
        self._session.add(session)
        await self._session.flush()
        return session

    async def get_session_by_refresh_hash(
        self, refresh_token_hash: str, *, for_update: bool = False
    ) -> AuthSession | None:
        statement: Select[tuple[AuthSession]] = select(AuthSession).where(
            AuthSession.refresh_token_hash == refresh_token_hash
        )
        if for_update:
            # Serialize all generations in a family, including an old-token replay
            # racing a refresh/logout of the current generation. Locking only the
            # presented row allows a new active generation to escape revocation.
            family_id = await self._session.scalar(
                select(AuthSession.family_id).where(
                    AuthSession.refresh_token_hash == refresh_token_hash
                )
            )
            if family_id is not None:
                await self._session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": int.from_bytes(family_id.bytes[:8], "big", signed=True)},
                )
            statement = statement.with_for_update().execution_options(populate_existing=True)
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_user(self, user_id: UUID) -> User | None:
        result = await self._session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def family_is_active(self, *, family_id: UUID, user_id: UUID, now: datetime) -> bool:
        session_id = await self._session.scalar(
            select(AuthSession.id).where(
                AuthSession.family_id == family_id,
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )
        return session_id is not None

    async def revoke_family(self, family_id: UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(AuthSession)
            .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def mark_reuse(self, session: AuthSession, *, now: datetime) -> None:
        session.reuse_detected_at = now
        await self._session.flush()

    async def create_web_session(
        self, *, user_id: UUID, token_hash: str, expires_at: datetime, now: datetime
    ) -> WebSession:
        session = WebSession(
            user_id=user_id, token_hash=token_hash, expires_at=expires_at, created_at=now
        )
        self._session.add(session)
        await self._session.flush()
        return session

    async def prune_web_sessions(self, *, user_id: UUID, keep: int, now: datetime) -> None:
        """Drop expired/revoked browser sessions and all but the newest ``keep`` live ones."""
        await self._session.execute(
            delete(WebSession).where(
                WebSession.user_id == user_id,
                (WebSession.expires_at <= now) | WebSession.revoked_at.is_not(None),
            )
        )
        newest = (
            select(WebSession.id)
            .where(WebSession.user_id == user_id)
            .order_by(WebSession.created_at.desc(), WebSession.id.desc())
            .limit(keep)
        )
        await self._session.execute(
            delete(WebSession).where(
                WebSession.user_id == user_id, WebSession.id.not_in(newest.scalar_subquery())
            )
        )

    async def get_active_web_session(self, token_hash: str, *, now: datetime) -> WebSession | None:
        session: WebSession | None = await self._session.scalar(
            select(WebSession).where(
                WebSession.token_hash == token_hash,
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > now,
            )
        )
        return session

    async def revoke_web_session(self, token_hash: str, *, now: datetime) -> None:
        await self._session.execute(
            update(WebSession)
            .where(WebSession.token_hash == token_hash, WebSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
