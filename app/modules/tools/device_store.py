"""Persistence of device access (M70): run origins, declarations, grants, settings, log.

``InMemoryDeviceAccessStore`` serves tests and database-less runs; ``SqlDeviceAccessStore``
is the production store (tables of ``20261010_m70_device_access``). Nothing here stores
device data: the access log keeps what was asked, by whom and the decision, never results.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ports.device_tool_channel import DeviceToolDeclaration
from app.core.ports.types import Risk
from app.modules.auth.models import AuthSession, Device, WebSession
from app.modules.tools.device_policy import GrantScope, RemoteSetting, RequesterKind
from app.modules.tools.models import (
    DeviceAccessLogRow,
    DeviceCapabilitySetting,
    DeviceGrant,
    DeviceRunOrigin,
    DeviceToolDeclarationSet,
)

ACCESS_LOG_RETENTION = timedelta(days=90)
ACCESS_LOG_LIMIT = 100
LogDecision = str
"""``pending`` → ``approved``/``denied`` (listed as ``expired`` once lapsed); ``allowed``
(grant), ``blocked`` (setting), ``offline``."""


@dataclass(frozen=True, slots=True)
class RunOrigin:
    run_id: str
    user_id: str
    kind: RequesterKind
    label: str
    device_id: str | None = None
    web_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceDeclarations:
    device_id: str
    name: str | None
    tools: tuple[DeviceToolDeclaration, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class GrantRecord:
    id: str
    user_id: str
    target_device_id: str
    capability: str
    scope: GrantScope
    requester_kind: str
    requester_label: str
    created_at: datetime
    expires_at: datetime
    run_id: str | None = None
    requester_key: str | None = None
    requester_device_id: str | None = None
    requester_web_session_id: str | None = None
    approval_id: str | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AccessLogEntry:
    id: str
    user_id: str
    device_id: str
    capability: str
    tool: str
    requester_kind: str
    requester_label: str
    decision: LogDecision
    created_at: datetime
    run_id: str | None = None
    approval_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GrantMatch:
    run_grant_id: str | None = None
    persistent_grant_id: str | None = None


class DeviceAccessStore(Protocol):
    async def record_origin(self, origin: RunOrigin, *, now: datetime) -> None: ...

    async def origin(self, *, user_id: str, run_id: str) -> RunOrigin | None: ...

    async def save_declarations(
        self,
        *,
        user_id: str,
        device_id: str,
        tools: Sequence[DeviceToolDeclaration],
        now: datetime,
    ) -> bool: ...

    async def declarations(self, *, user_id: str) -> list[DeviceDeclarations]: ...

    async def device_name(self, *, user_id: str, device_id: str) -> str | None: ...

    async def session_device(self, *, user_id: str, family_id: str) -> str | None: ...

    async def web_session_device(self, *, user_id: str, web_session_id: str) -> str | None: ...

    async def find_grants(
        self,
        *,
        user_id: str,
        target_device_id: str,
        capability: str,
        run_id: str,
        requester_key: str | None,
        now: datetime,
    ) -> GrantMatch: ...

    async def add_grant(self, grant: GrantRecord) -> None: ...

    async def touch_grant(self, grant_id: str, *, now: datetime) -> None: ...

    async def list_grants(
        self, *, user_id: str, device_id: str, now: datetime
    ) -> list[GrantRecord]: ...

    async def revoke_grant(
        self, *, user_id: str, device_id: str, grant_id: str, now: datetime
    ) -> bool: ...

    async def remote_settings(self, *, user_id: str, device_id: str) -> dict[str, str]: ...

    async def set_remote(
        self,
        *,
        user_id: str,
        device_id: str,
        capability: str,
        remote: RemoteSetting,
        now: datetime,
    ) -> None: ...

    async def log(self, entry: AccessLogEntry) -> None: ...

    async def resolve_log(self, *, user_id: str, approval_id: str, decision: str) -> None: ...

    async def access_log(
        self, *, user_id: str, device_id: str, limit: int
    ) -> list[AccessLogEntry]: ...


def _uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def declaration_to_json(tool: DeviceToolDeclaration) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
        "risk": tool.risk,
        "voice": tool.voice,
        "requires_unlocked": tool.requires_unlocked,
        "timeout_ms": tool.timeout_ms,
    }


def declaration_from_json(raw: Any) -> DeviceToolDeclaration | None:
    try:
        return DeviceToolDeclaration(
            name=str(raw["name"]),
            description=str(raw["description"]),
            input_schema=dict(raw["input_schema"]),
            risk=cast(Risk, raw["risk"]),
            voice=bool(raw["voice"]),
            requires_unlocked=bool(raw["requires_unlocked"]),
            timeout_ms=int(raw["timeout_ms"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


# -- in memory -------------------------------------------------------------------------


class InMemoryDeviceAccessStore:
    """Process-local store; devices/sessions are registered by tests (or the composition)."""

    def __init__(self) -> None:
        self.origins: dict[str, RunOrigin] = {}
        self.devices: dict[str, tuple[str, str | None]] = {}
        """device_id → (user_id, name)."""
        self.sessions: dict[str, tuple[str, str]] = {}
        """auth family id → (user_id, device_id)."""
        self.web_sessions: dict[str, tuple[str, str | None, bool]] = {}
        """web session id → (user_id, device_id, active)."""
        self._declarations: dict[str, tuple[str, tuple[DeviceToolDeclaration, ...], datetime]] = {}
        self.grants: dict[str, GrantRecord] = {}
        self.settings: dict[tuple[str, str], str] = {}
        self.entries: list[AccessLogEntry] = []

    def add_device(self, user_id: str, device_id: str, name: str | None = None) -> None:
        self.devices[device_id] = (user_id, name)

    async def record_origin(self, origin: RunOrigin, *, now: datetime) -> None:
        self.origins[origin.run_id] = origin

    async def origin(self, *, user_id: str, run_id: str) -> RunOrigin | None:
        found = self.origins.get(run_id)
        return found if found is not None and found.user_id == user_id else None

    async def save_declarations(
        self,
        *,
        user_id: str,
        device_id: str,
        tools: Sequence[DeviceToolDeclaration],
        now: datetime,
    ) -> bool:
        owner = self.devices.get(device_id)
        if owner is None or owner[0] != user_id:
            return False
        self._declarations[device_id] = (user_id, tuple(tools), now)
        return True

    async def declarations(self, *, user_id: str) -> list[DeviceDeclarations]:
        found = [
            DeviceDeclarations(device_id, self.devices.get(device_id, ("", None))[1], tools, at)
            for device_id, (owner, tools, at) in self._declarations.items()
            if owner == user_id
        ]
        return sorted(found, key=lambda item: item.updated_at, reverse=True)

    async def device_name(self, *, user_id: str, device_id: str) -> str | None:
        owner = self.devices.get(device_id)
        return owner[1] if owner is not None and owner[0] == user_id else None

    async def session_device(self, *, user_id: str, family_id: str) -> str | None:
        found = self.sessions.get(family_id)
        return found[1] if found is not None and found[0] == user_id else None

    async def web_session_device(self, *, user_id: str, web_session_id: str) -> str | None:
        found = self.web_sessions.get(web_session_id)
        return found[1] if found is not None and found[0] == user_id and found[2] else None

    def _usable(self, grant: GrantRecord, now: datetime) -> bool:
        if grant.revoked_at is not None or grant.expires_at <= now:
            return False
        session = grant.requester_web_session_id
        return session is None or self.web_sessions.get(session, ("", None, False))[2]

    async def find_grants(
        self,
        *,
        user_id: str,
        target_device_id: str,
        capability: str,
        run_id: str,
        requester_key: str | None,
        now: datetime,
    ) -> GrantMatch:
        run_grant = persistent = None
        for grant in self.grants.values():
            if (
                grant.user_id != user_id
                or grant.target_device_id != target_device_id
                or grant.capability != capability
                or not self._usable(grant, now)
            ):
                continue
            if grant.scope == "run" and grant.run_id == run_id:
                run_grant = grant.id
            elif (
                grant.scope == "persistent"
                and requester_key
                and grant.requester_key == (requester_key)
            ):
                persistent = grant.id
        return GrantMatch(run_grant, persistent)

    async def add_grant(self, grant: GrantRecord) -> None:
        self.grants[grant.id] = grant

    async def touch_grant(self, grant_id: str, *, now: datetime) -> None:
        grant = self.grants.get(grant_id)
        if grant is not None:
            self.grants[grant_id] = dataclasses.replace(grant, last_used_at=now)

    async def list_grants(
        self, *, user_id: str, device_id: str, now: datetime
    ) -> list[GrantRecord]:
        found = [
            grant
            for grant in self.grants.values()
            if grant.user_id == user_id
            and grant.target_device_id == device_id
            and grant.scope == "persistent"
            and self._usable(grant, now)
        ]
        return sorted(found, key=lambda grant: grant.created_at, reverse=True)

    async def revoke_grant(
        self, *, user_id: str, device_id: str, grant_id: str, now: datetime
    ) -> bool:
        grant = self.grants.get(grant_id)
        if (
            grant is None
            or grant.user_id != user_id
            or grant.target_device_id != device_id
            or grant.scope != "persistent"
            or not self._usable(grant, now)
        ):
            return False
        self.grants[grant_id] = dataclasses.replace(grant, revoked_at=now)
        return True

    async def remote_settings(self, *, user_id: str, device_id: str) -> dict[str, str]:
        return {
            capability: value
            for (device, capability), value in self.settings.items()
            if device == device_id and self.devices.get(device, (user_id, None))[0] == user_id
        }

    async def set_remote(
        self,
        *,
        user_id: str,
        device_id: str,
        capability: str,
        remote: RemoteSetting,
        now: datetime,
    ) -> None:
        self.settings[(device_id, capability)] = remote

    async def log(self, entry: AccessLogEntry) -> None:
        self.entries.append(entry)

    async def resolve_log(self, *, user_id: str, approval_id: str, decision: str) -> None:
        self.entries = [
            dataclasses.replace(entry, decision=decision)
            if entry.approval_id == approval_id
            and entry.user_id == user_id
            and entry.decision == "pending"
            else entry
            for entry in self.entries
        ]

    async def access_log(self, *, user_id: str, device_id: str, limit: int) -> list[AccessLogEntry]:
        found = [e for e in self.entries if e.user_id == user_id and e.device_id == device_id]
        return sorted(found, key=lambda entry: entry.created_at, reverse=True)[:limit]


# -- PostgreSQL ------------------------------------------------------------------------


def _grant(row: DeviceGrant) -> GrantRecord:
    return GrantRecord(
        id=str(row.id),
        user_id=str(row.user_id),
        target_device_id=str(row.target_device_id),
        capability=row.capability,
        scope=cast(GrantScope, row.scope),
        requester_kind=row.requester_kind,
        requester_label=row.requester_label,
        created_at=row.created_at,
        expires_at=row.expires_at,
        run_id=str(row.run_id) if row.run_id else None,
        requester_key=row.requester_key,
        requester_device_id=str(row.requester_device_id) if row.requester_device_id else None,
        requester_web_session_id=(
            str(row.requester_web_session_id) if row.requester_web_session_id else None
        ),
        approval_id=str(row.approval_id) if row.approval_id else None,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _entry(row: DeviceAccessLogRow) -> AccessLogEntry:
    return AccessLogEntry(
        id=str(row.id),
        user_id=str(row.user_id),
        device_id=str(row.device_id),
        capability=row.capability,
        tool=row.tool,
        requester_kind=row.requester_kind,
        requester_label=row.requester_label,
        decision=row.decision,
        created_at=row.created_at,
        run_id=str(row.run_id) if row.run_id else None,
        approval_id=str(row.approval_id) if row.approval_id else None,
        expires_at=row.expires_at,
    )


class SqlDeviceAccessStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_origin(self, origin: RunOrigin, *, now: datetime) -> None:
        run, user = _uuid(origin.run_id), _uuid(origin.user_id)
        if run is None or user is None:
            return
        statement = insert(DeviceRunOrigin).values(
            run_id=run,
            user_id=user,
            kind=origin.kind,
            label=origin.label[:128],
            device_id=_uuid(origin.device_id),
            web_session_id=_uuid(origin.web_session_id),
            created_at=now,
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(statement.on_conflict_do_nothing(index_elements=["run_id"]))

    async def origin(self, *, user_id: str, run_id: str) -> RunOrigin | None:
        run, user = _uuid(run_id), _uuid(user_id)
        if run is None or user is None:
            return None
        async with self._session_factory() as session:
            row = await session.scalar(
                select(DeviceRunOrigin).where(
                    DeviceRunOrigin.run_id == run, DeviceRunOrigin.user_id == user
                )
            )
        if row is None:
            return None
        return RunOrigin(
            run_id=str(row.run_id),
            user_id=str(row.user_id),
            kind=cast(RequesterKind, row.kind),
            label=row.label,
            device_id=str(row.device_id) if row.device_id else None,
            web_session_id=str(row.web_session_id) if row.web_session_id else None,
        )

    async def save_declarations(
        self,
        *,
        user_id: str,
        device_id: str,
        tools: Sequence[DeviceToolDeclaration],
        now: datetime,
    ) -> bool:
        user, device = _uuid(user_id), _uuid(device_id)
        if user is None or device is None:
            return False
        body = [declaration_to_json(tool) for tool in tools]
        async with self._session_factory() as session, session.begin():
            owned = await session.scalar(
                select(Device.id).where(Device.id == device, Device.user_id == user)
            )
            if owned is None:
                return False
            statement = insert(DeviceToolDeclarationSet).values(
                device_id=device, user_id=user, tools=body, updated_at=now
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["device_id"],
                    set_={"tools": statement.excluded.tools, "updated_at": now},
                )
            )
        return True

    async def declarations(self, *, user_id: str) -> list[DeviceDeclarations]:
        user = _uuid(user_id)
        if user is None:
            return []
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        DeviceToolDeclarationSet,
                        func.coalesce(Device.display_name, Device.device_name),
                    )
                    .join(Device, Device.id == DeviceToolDeclarationSet.device_id)
                    .where(DeviceToolDeclarationSet.user_id == user, Device.user_id == user)
                    .order_by(DeviceToolDeclarationSet.updated_at.desc())
                )
            ).all()
        result: list[DeviceDeclarations] = []
        for row, name in rows:
            tools = tuple(
                tool
                for tool in (declaration_from_json(raw) for raw in row.tools or [])
                if tool is not None
            )
            result.append(DeviceDeclarations(str(row.device_id), name, tools, row.updated_at))
        return result

    async def device_name(self, *, user_id: str, device_id: str) -> str | None:
        user, device = _uuid(user_id), _uuid(device_id)
        if user is None or device is None:
            return None
        async with self._session_factory() as session:
            # The name chosen on the «Устройства» screen (M84) wins over the model name.
            name = await session.scalar(
                select(func.coalesce(Device.display_name, Device.device_name)).where(
                    Device.id == device, Device.user_id == user
                )
            )
        return name

    async def session_device(self, *, user_id: str, family_id: str) -> str | None:
        user, family = _uuid(user_id), _uuid(family_id)
        if user is None or family is None:
            return None
        async with self._session_factory() as session:
            device = await session.scalar(
                select(AuthSession.device_id)
                .where(AuthSession.family_id == family, AuthSession.user_id == user)
                .limit(1)
            )
        return str(device) if device is not None else None

    async def web_session_device(self, *, user_id: str, web_session_id: str) -> str | None:
        user, ident = _uuid(user_id), _uuid(web_session_id)
        if user is None or ident is None:
            return None
        async with self._session_factory() as session:
            device = await session.scalar(
                select(WebSession.device_id).where(
                    WebSession.id == ident,
                    WebSession.user_id == user,
                    WebSession.revoked_at.is_(None),
                )
            )
        return str(device) if device is not None else None

    async def find_grants(
        self,
        *,
        user_id: str,
        target_device_id: str,
        capability: str,
        run_id: str,
        requester_key: str | None,
        now: datetime,
    ) -> GrantMatch:
        user, target, run = _uuid(user_id), _uuid(target_device_id), _uuid(run_id)
        if user is None or target is None:
            return GrantMatch()
        query = (
            select(DeviceGrant.id, DeviceGrant.scope, DeviceGrant.run_id, DeviceGrant.requester_key)
            .outerjoin(WebSession, WebSession.id == DeviceGrant.requester_web_session_id)
            .where(
                DeviceGrant.user_id == user,
                DeviceGrant.target_device_id == target,
                DeviceGrant.capability == capability,
                DeviceGrant.revoked_at.is_(None),
                DeviceGrant.expires_at > now,
                (DeviceGrant.requester_web_session_id.is_(None))
                | ((WebSession.revoked_at.is_(None)) & (WebSession.expires_at > now)),
            )
        )
        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        run_grant = persistent = None
        for grant_id, scope, grant_run, key in rows:
            if scope == "run" and run is not None and grant_run == run:
                run_grant = str(grant_id)
            elif scope == "persistent" and requester_key and key == requester_key:
                persistent = str(grant_id)
        return GrantMatch(run_grant, persistent)

    async def add_grant(self, grant: GrantRecord) -> None:
        row = DeviceGrant(
            id=uuid.UUID(grant.id),
            user_id=uuid.UUID(grant.user_id),
            target_device_id=uuid.UUID(grant.target_device_id),
            capability=grant.capability,
            scope=grant.scope,
            run_id=_uuid(grant.run_id),
            requester_kind=grant.requester_kind,
            requester_label=grant.requester_label[:128],
            requester_key=grant.requester_key,
            requester_device_id=_uuid(grant.requester_device_id),
            requester_web_session_id=_uuid(grant.requester_web_session_id),
            approval_id=_uuid(grant.approval_id),
            created_at=grant.created_at,
            expires_at=grant.expires_at,
        )
        async with self._session_factory() as session, session.begin():
            session.add(row)

    async def touch_grant(self, grant_id: str, *, now: datetime) -> None:
        ident = _uuid(grant_id)
        if ident is None:
            return
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(DeviceGrant).where(DeviceGrant.id == ident).values(last_used_at=now)
            )

    def _active_persistent(self, user: uuid.UUID, device: uuid.UUID, now: datetime) -> Any:
        return (
            select(DeviceGrant)
            .outerjoin(WebSession, WebSession.id == DeviceGrant.requester_web_session_id)
            .where(
                DeviceGrant.user_id == user,
                DeviceGrant.target_device_id == device,
                DeviceGrant.scope == "persistent",
                DeviceGrant.revoked_at.is_(None),
                DeviceGrant.expires_at > now,
                (DeviceGrant.requester_web_session_id.is_(None))
                | ((WebSession.revoked_at.is_(None)) & (WebSession.expires_at > now)),
            )
        )

    async def list_grants(
        self, *, user_id: str, device_id: str, now: datetime
    ) -> list[GrantRecord]:
        user, device = _uuid(user_id), _uuid(device_id)
        if user is None or device is None:
            return []
        query = self._active_persistent(user, device, now).order_by(DeviceGrant.created_at.desc())
        async with self._session_factory() as session:
            rows = (await session.scalars(query)).all()
        return [_grant(row) for row in rows]

    async def revoke_grant(
        self, *, user_id: str, device_id: str, grant_id: str, now: datetime
    ) -> bool:
        user, device, ident = _uuid(user_id), _uuid(device_id), _uuid(grant_id)
        if user is None or device is None or ident is None:
            return False
        async with self._session_factory() as session, session.begin():
            found = await session.scalar(
                self._active_persistent(user, device, now).where(DeviceGrant.id == ident)
            )
            if found is None:
                return False
            found.revoked_at = now
        return True

    async def remote_settings(self, *, user_id: str, device_id: str) -> dict[str, str]:
        user, device = _uuid(user_id), _uuid(device_id)
        if user is None or device is None:
            return {}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        DeviceCapabilitySetting.capability, DeviceCapabilitySetting.remote
                    ).where(
                        DeviceCapabilitySetting.user_id == user,
                        DeviceCapabilitySetting.device_id == device,
                    )
                )
            ).all()
        return {capability: remote for capability, remote in rows}

    async def set_remote(
        self,
        *,
        user_id: str,
        device_id: str,
        capability: str,
        remote: RemoteSetting,
        now: datetime,
    ) -> None:
        user, device = uuid.UUID(user_id), uuid.UUID(device_id)
        statement = insert(DeviceCapabilitySetting).values(
            device_id=device, capability=capability, user_id=user, remote=remote, updated_at=now
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["device_id", "capability"],
                    set_={"remote": remote, "updated_at": now},
                    where=DeviceCapabilitySetting.user_id == user,
                )
            )

    async def log(self, entry: AccessLogEntry) -> None:
        user, device = uuid.UUID(entry.user_id), uuid.UUID(entry.device_id)
        async with self._session_factory() as session, session.begin():
            session.add(
                DeviceAccessLogRow(
                    id=uuid.UUID(entry.id),
                    user_id=user,
                    device_id=device,
                    capability=entry.capability[:64],
                    tool=entry.tool[:128],
                    requester_kind=entry.requester_kind,
                    requester_label=entry.requester_label[:128],
                    decision=entry.decision,
                    run_id=_uuid(entry.run_id),
                    approval_id=_uuid(entry.approval_id),
                    created_at=entry.created_at,
                    expires_at=entry.expires_at,
                )
            )
            await session.execute(
                delete(DeviceAccessLogRow).where(
                    DeviceAccessLogRow.user_id == user,
                    DeviceAccessLogRow.device_id == device,
                    DeviceAccessLogRow.created_at < entry.created_at - ACCESS_LOG_RETENTION,
                )
            )

    async def resolve_log(self, *, user_id: str, approval_id: str, decision: str) -> None:
        user, approval = _uuid(user_id), _uuid(approval_id)
        if user is None or approval is None:
            return
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(DeviceAccessLogRow)
                .where(
                    DeviceAccessLogRow.user_id == user,
                    DeviceAccessLogRow.approval_id == approval,
                    DeviceAccessLogRow.decision == "pending",
                )
                .values(decision=decision)
            )

    async def access_log(self, *, user_id: str, device_id: str, limit: int) -> list[AccessLogEntry]:
        user, device = _uuid(user_id), _uuid(device_id)
        if user is None or device is None:
            return []
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(DeviceAccessLogRow)
                    .where(
                        DeviceAccessLogRow.user_id == user, DeviceAccessLogRow.device_id == device
                    )
                    .order_by(DeviceAccessLogRow.created_at.desc())
                    .limit(limit)
                )
            ).all()
        return [_entry(row) for row in rows]
