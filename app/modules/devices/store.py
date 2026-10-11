"""Devices of an account as one list (M84): phones and browsers today, Personal Nodes later.

Every kind of device is a :class:`DeviceSource`: it lists, reads, renames and disconnects its
devices and returns them as :class:`DeviceRecord`. :class:`SqlPhoneSource` reads ``devices``
(a phone is listed while it has an active refresh-token family), :class:`SqlBrowserSource`
reads ``web_sessions`` (live browser sessions, not the app's WebView). Personal Node (M86)
adds its own source with ``kind="node"`` and connection-based presence
(:meth:`app.modules.devices.presence.PresenceTracker.connected`) without changing the API.

Nothing here reads device data: names, versions, states and times only.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import and_, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.models import AuthSession, Device, WebSession
from app.modules.devices.presence import DeviceKind, PushState, latest
from app.modules.devices.report import os_permissions, report_text
from app.modules.notifications.models import PushTokenRow
from app.modules.tools.models import DeviceGrant, DeviceToolDeclarationSet

DEFAULT_PHONE_NAME = "Телефон"
DEFAULT_BROWSER_NAME = "Браузер"


def as_uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    id: str
    kind: DeviceKind
    default_name: str
    custom_name: str | None
    connected_at: datetime
    last_seen_at: datetime | None
    push: PushState
    os: str | None = None
    model: str | None = None
    app_version: str | None = None
    client: str | None = None
    notifications: str | None = None
    os_permissions: Mapping[str, str] = field(default_factory=dict)
    reported_at: datetime | None = None

    @property
    def name(self) -> str:
        return self.custom_name or self.default_name


class DeviceSource(Protocol):
    """One kind of device in the «Устройства» list."""

    kind: DeviceKind

    async def list(self, *, user_id: str, now: datetime) -> list[DeviceRecord]: ...

    async def get(self, *, user_id: str, device_id: str, now: datetime) -> DeviceRecord | None: ...

    async def rename(self, *, user_id: str, device_id: str, name: str | None) -> bool: ...

    async def disconnect(self, *, user_id: str, device_id: str, now: datetime) -> bool:
        """Sign the device out, drop its push token and permissions; ``False`` if unknown."""
        ...


# -- phones ----------------------------------------------------------------------------


def _active_families(user: uuid.UUID, now: datetime) -> Any:
    """``(device_id, connected_at)`` of devices with at least one active refresh family."""
    families = (
        select(
            AuthSession.device_id,
            AuthSession.family_id,
            func.min(AuthSession.created_at).label("connected_at"),
        )
        .where(AuthSession.user_id == user)
        .group_by(AuthSession.device_id, AuthSession.family_id)
        .having(func.bool_or(and_(AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)))
        .subquery()
    )
    return (
        select(families.c.device_id, func.min(families.c.connected_at).label("connected_at"))
        .group_by(families.c.device_id)
        .subquery()
    )


class SqlPhoneSource:
    kind: DeviceKind = "phone"

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, push_configured: bool
    ) -> None:
        self._session_factory = session_factory
        self._push_configured = push_configured

    def _query(self, user: uuid.UUID, now: datetime) -> Any:
        active = _active_families(user, now)
        has_token = (
            exists()
            .where(
                PushTokenRow.device_id == Device.id,
                PushTokenRow.user_id == user,
                PushTokenRow.provider == "fcm",
                PushTokenRow.invalidated_at.is_(None),
            )
            .label("has_token")
        )
        return (
            select(Device, active.c.connected_at, has_token)
            .join(active, active.c.device_id == Device.id)
            .where(Device.user_id == user)
        )

    def _record(self, device: Device, connected_at: datetime, has_token: bool) -> DeviceRecord:
        report = device.status_report
        push: PushState = (
            "not_configured"
            if not self._push_configured
            else ("registered" if has_token else "missing")
        )
        model = report_text(report, "model") or device.device_name
        return DeviceRecord(
            id=str(device.id),
            kind="phone",
            default_name=device.device_name or model or DEFAULT_PHONE_NAME,
            custom_name=device.display_name,
            connected_at=connected_at,
            last_seen_at=latest(device.last_seen_at, connected_at),
            push=push,
            os=report_text(report, "os"),
            model=model,
            app_version=report_text(report, "app_version") or device.app_version,
            notifications=report_text(report, "notifications"),
            os_permissions=os_permissions(report),
            reported_at=device.status_reported_at,
        )

    async def list(self, *, user_id: str, now: datetime) -> list[DeviceRecord]:
        user = as_uuid(user_id)
        if user is None:
            return []
        async with self._session_factory() as session:
            rows = (await session.execute(self._query(user, now))).all()
        return [self._record(device, connected, bool(token)) for device, connected, token in rows]

    async def get(self, *, user_id: str, device_id: str, now: datetime) -> DeviceRecord | None:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return None
        async with self._session_factory() as session:
            row = (await session.execute(self._query(user, now).where(Device.id == ident))).first()
        if row is None:
            return None
        device, connected, token = row
        return self._record(device, connected, bool(token))

    async def rename(self, *, user_id: str, device_id: str, name: str | None) -> bool:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return False
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(Device)
                .where(Device.id == ident, Device.user_id == user)
                .values(display_name=name)
                .returning(Device.id)
            )
            return result.first() is not None

    async def disconnect(self, *, user_id: str, device_id: str, now: datetime) -> bool:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return False
        async with self._session_factory() as session, session.begin():
            owned = await session.scalar(
                select(Device.id)
                .where(Device.id == ident, Device.user_id == user)
                .with_for_update()
            )
            if owned is None:
                return False
            # Sign-out of every session of the phone: its bearer answers 401 from now on.
            await session.execute(
                update(AuthSession)
                .where(
                    AuthSession.device_id == ident,
                    AuthSession.user_id == user,
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            # The app's WebView on that phone is the same device (ADR-0016 §3).
            await session.execute(
                update(WebSession)
                .where(
                    WebSession.device_id == ident,
                    WebSession.user_id == user,
                    WebSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            await session.execute(
                delete(PushTokenRow).where(
                    PushTokenRow.device_id == ident, PushTokenRow.user_id == user
                )
            )
            # Permissions it gave to other clients and those other phones gave to it.
            await session.execute(
                update(DeviceGrant)
                .where(
                    DeviceGrant.user_id == user,
                    (DeviceGrant.target_device_id == ident)
                    | (DeviceGrant.requester_device_id == ident),
                    DeviceGrant.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            # No longer a target of device tools until the app declares them again.
            await session.execute(
                delete(DeviceToolDeclarationSet).where(
                    DeviceToolDeclarationSet.device_id == ident,
                    DeviceToolDeclarationSet.user_id == user,
                )
            )
            await session.execute(
                update(Device)
                .where(Device.id == ident)
                .values(status_report=None, status_reported_at=None)
            )
        return True


# -- browsers --------------------------------------------------------------------------


class SqlBrowserSource:
    kind: DeviceKind = "browser"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _query(user: uuid.UUID, now: datetime) -> Any:
        return select(WebSession).where(
            WebSession.user_id == user,
            WebSession.device_id.is_(None),
            WebSession.revoked_at.is_(None),
            WebSession.expires_at > now,
        )

    @staticmethod
    def _record(row: WebSession) -> DeviceRecord:
        return DeviceRecord(
            id=str(row.id),
            kind="browser",
            default_name=row.client_label or DEFAULT_BROWSER_NAME,
            custom_name=row.display_name,
            connected_at=row.created_at,
            last_seen_at=latest(row.last_seen_at, row.created_at),
            push="not_applicable",
            client=row.client_label,
        )

    async def list(self, *, user_id: str, now: datetime) -> list[DeviceRecord]:
        user = as_uuid(user_id)
        if user is None:
            return []
        async with self._session_factory() as session:
            rows = (await session.scalars(self._query(user, now))).all()
        return [self._record(row) for row in rows]

    async def get(self, *, user_id: str, device_id: str, now: datetime) -> DeviceRecord | None:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return None
        async with self._session_factory() as session:
            row = await session.scalar(self._query(user, now).where(WebSession.id == ident))
        return self._record(row) if row is not None else None

    async def rename(self, *, user_id: str, device_id: str, name: str | None) -> bool:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return False
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(WebSession)
                .where(
                    WebSession.id == ident,
                    WebSession.user_id == user,
                    WebSession.device_id.is_(None),
                    WebSession.revoked_at.is_(None),
                )
                .values(display_name=name)
                .returning(WebSession.id)
            )
            return result.first() is not None

    async def disconnect(self, *, user_id: str, device_id: str, now: datetime) -> bool:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return False
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(WebSession)
                .where(
                    WebSession.id == ident,
                    WebSession.user_id == user,
                    WebSession.device_id.is_(None),
                    WebSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
                .returning(WebSession.id)
            )
            if result.first() is None:
                return False
            # Its persistent permissions end with the session anyway (M70); revoke them
            # explicitly so the phone's list is clean at once.
            await session.execute(
                update(DeviceGrant)
                .where(
                    DeviceGrant.user_id == user,
                    DeviceGrant.requester_web_session_id == ident,
                    DeviceGrant.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        return True


# -- identity, contact, reports, received grants ---------------------------------------


@dataclass(frozen=True, slots=True)
class ReceivedGrant:
    """A persistent permission another device gave to this requester (phone or browser)."""

    id: str
    target_device_id: str
    target_device_name: str
    capability: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None


class SqlDirectoryStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def family_device(self, *, user_id: str, family_id: str) -> str | None:
        user, family = as_uuid(user_id), as_uuid(family_id)
        if user is None or family is None:
            return None
        async with self._session_factory() as session:
            device = await session.scalar(
                select(AuthSession.device_id)
                .where(AuthSession.family_id == family, AuthSession.user_id == user)
                .limit(1)
            )
        return str(device) if device is not None else None

    async def web_session_binding(
        self, *, user_id: str, web_session_id: str
    ) -> tuple[bool, str | None]:
        """``(exists, bound device id)``: the WebView's session is bound to its phone."""
        user, ident = as_uuid(user_id), as_uuid(web_session_id)
        if user is None or ident is None:
            return False, None
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(WebSession.id, WebSession.device_id).where(
                        WebSession.id == ident, WebSession.user_id == user
                    )
                )
            ).first()
        if row is None:
            return False, None
        return True, (str(row[1]) if row[1] is not None else None)

    async def touch_phone(self, *, user_id: str, device_id: str, now: datetime) -> None:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(Device)
                .where(Device.id == ident, Device.user_id == user, Device.last_seen_at < now)
                .values(last_seen_at=now)
            )

    async def touch_browser(
        self, *, user_id: str, web_session_id: str, now: datetime, label: str | None
    ) -> None:
        user, ident = as_uuid(user_id), as_uuid(web_session_id)
        if user is None or ident is None:
            return
        values: dict[str, Any] = {"last_seen_at": now}
        if label:
            values["client_label"] = func.coalesce(WebSession.client_label, label)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(WebSession)
                .where(WebSession.id == ident, WebSession.user_id == user)
                .values(**values)
            )

    async def save_report(
        self, *, user_id: str, device_id: str, report: dict[str, Any], now: datetime
    ) -> None:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(Device)
                .where(Device.id == ident, Device.user_id == user)
                .values(status_report=report, status_reported_at=now, last_seen_at=now)
            )

    async def owns_phone(self, *, user_id: str, device_id: str) -> bool:
        user, ident = as_uuid(user_id), as_uuid(device_id)
        if user is None or ident is None:
            return False
        async with self._session_factory() as session:
            found = await session.scalar(
                select(Device.id).where(Device.id == ident, Device.user_id == user)
            )
        return found is not None

    async def received_grants(
        self, *, user_id: str, requester_key: str, now: datetime
    ) -> list[ReceivedGrant]:
        user = as_uuid(user_id)
        if user is None:
            return []
        query = (
            select(DeviceGrant, func.coalesce(Device.display_name, Device.device_name))
            .join(Device, Device.id == DeviceGrant.target_device_id)
            .where(
                DeviceGrant.user_id == user,
                DeviceGrant.scope == "persistent",
                DeviceGrant.requester_key == requester_key,
                DeviceGrant.revoked_at.is_(None),
                DeviceGrant.expires_at > now,
            )
            .order_by(DeviceGrant.created_at.desc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        return [
            ReceivedGrant(
                id=str(grant.id),
                target_device_id=str(grant.target_device_id),
                target_device_name=name or DEFAULT_PHONE_NAME,
                capability=grant.capability,
                created_at=grant.created_at,
                expires_at=grant.expires_at,
                last_used_at=grant.last_used_at,
            )
            for grant, name in rows
        ]
