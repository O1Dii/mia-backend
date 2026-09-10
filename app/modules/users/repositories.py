"""Transaction-neutral user repository."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.users.models import User


class UserRepository:
    """Persist users using a caller-owned transaction.

    Repository methods flush when an identifier must be visible, but never
    commit. This lets authentication and conversation services define the
    complete atomic operation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        google_sub: str,
        email: str,
        email_verified: bool = False,
        display_name: str | None = None,
        avatar_url: str | None = None,
        locale: str = "en",
        timezone: str = "UTC",
    ) -> User:
        user = User(
            google_sub=google_sub,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            avatar_url=avatar_url,
            locale=locale,
            timezone=timezone,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def get_by_id(self, user_id: uuid.UUID, *, include_deleted: bool = False) -> User | None:
        statement = select(User).where(User.id == user_id)
        if not include_deleted:
            statement = statement.where(User.deleted_at.is_(None))
        result = await self._session.scalars(statement)
        return result.one_or_none()

    async def get_by_google_sub(
        self, google_sub: str, *, include_deleted: bool = False
    ) -> User | None:
        statement = select(User).where(User.google_sub == google_sub)
        if not include_deleted:
            statement = statement.where(User.deleted_at.is_(None))
        result = await self._session.scalars(statement)
        return result.one_or_none()
