"""«Устройства» (M84): which devices the account has, what can run on each, and their state.

:class:`DeviceDirectory` joins the device sources (phones, browsers; Personal Nodes in M86)
with presence (:mod:`app.modules.devices.presence`), the device access state of ADR-0016
(capability switches, persistent grants, declarations — :mod:`app.modules.tools.device_store`)
and the installed plugins' «Где работает» (M72). It also records contact: every authenticated
request is observed (``RequestAuthenticator.add_observer``) and the clients' heartbeat keeps an
open app or tab «онлайн».

Bodies follow ``contracts/mvp/v1/devices.schema.json``. No phone numbers and no content: only
names of capabilities, states and times.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from fastapi import Request

from app.modules.auth.principal import RequestPrincipal
from app.modules.devices.presence import (
    HEARTBEAT_SECONDS,
    ONLINE_WINDOW,
    DeviceKind,
    PresenceTracker,
    latest,
    presence_of,
)
from app.modules.devices.report import DeviceStatusReport, clean_text
from app.modules.devices.store import DeviceRecord, DeviceSource, SqlDirectoryStore
from app.modules.plugins.install.consent import device_capabilities, where_it_runs
from app.modules.tools.device_access import DeviceAccess
from app.modules.tools.device_origin import browser_label
from app.modules.tools.device_policy import CAPABILITIES, capability_for, capability_named
from app.modules.tools.device_store import GrantRecord

logger = logging.getLogger(__name__)

NAME_MAX = 64
IDENTITY_CACHE = 4096
KIND_ORDER: dict[str, int] = {"phone": 0, "node": 1, "browser": 2}
CLOUD_LABEL = "Облако Mia"
CLOUD_SUMMARY = (
    "Чат, голос, память, расписания, будильники и уведомления работают на сервере Mia — "
    "даже когда все устройства выключены."
)
READY_PERMISSIONS = frozenset({"granted", "not_required"})
DisconnectResult = Literal["disconnected", "not_found", "current"]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _grant_body(grant: GrantRecord) -> dict[str, Any]:
    """``DeviceGrant`` (``device-access.schema.json``): a permission this phone gave."""
    return {
        "id": grant.id,
        "capability": grant.capability,
        "capability_label": capability_named(grant.capability).label,
        "requester": {"kind": grant.requester_kind, "label": grant.requester_label},
        "created_at": _iso(grant.created_at),
        "expires_at": _iso(grant.expires_at),
        "last_used_at": _iso(grant.last_used_at),
    }


class PluginLister(Protocol):
    """``PluginRegistry.installed`` (M14/M73): the user's plugins with their manifests."""

    async def installed(self, *, user_id: str, workspace_id: str | None) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class Identity:
    """The device a request comes from."""

    kind: DeviceKind
    device_id: str


def clean_name(value: str | None) -> str | None:
    """A user-chosen name: one line, ≤ 64 characters; empty resets to the default name."""
    return clean_text(value, NAME_MAX)


class DeviceDirectory:
    def __init__(
        self,
        store: SqlDirectoryStore,
        sources: Sequence[DeviceSource],
        *,
        tracker: PresenceTracker | None = None,
        access: DeviceAccess | None = None,
        plugins: PluginLister | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.tracker = tracker or PresenceTracker(clock or (lambda: datetime.now(UTC)))
        self._sources: dict[DeviceKind, DeviceSource] = {}
        for source in sources:
            self.add_source(source)
        self._access = access
        self._plugins = plugins
        self._families: OrderedDict[tuple[str, str], str | None] = OrderedDict()
        self._web: OrderedDict[tuple[str, str], tuple[bool, str | None]] = OrderedDict()

    def add_source(self, source: DeviceSource) -> None:
        """Personal Node (M86) registers its nodes here: the list and the card stay the same."""
        self._sources[source.kind] = source

    def now(self) -> datetime:
        return self.tracker.now()

    # -- who is asking ---------------------------------------------------------------

    def _cache(self, cache: OrderedDict[Any, Any], key: Any, value: Any) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > IDENTITY_CACHE:
            cache.popitem(last=False)

    async def identity(self, principal: RequestPrincipal) -> Identity | None:
        user = principal.user_id
        if principal.method == "bearer":
            key = (user, principal.session_id)
            if key in self._families:
                device = self._families[key]
            else:
                device = await self.store.family_device(user_id=user, family_id=key[1])
                self._cache(self._families, key, device)
            return Identity("phone", device) if device is not None else None
        key = (user, principal.session_id)
        if key in self._web:
            found, bound = self._web[key]
        else:
            found, bound = await self.store.web_session_binding(user_id=user, web_session_id=key[1])
            if found:
                self._cache(self._web, key, (found, bound))
        if not found:
            return None
        if bound is not None:
            return Identity("phone", bound)  # the app's WebView is its phone (ADR-0016 §3)
        return Identity("browser", principal.session_id)

    async def observe(self, request: Request, principal: RequestPrincipal) -> None:
        """Every authenticated request is a contact of its device (written once a minute)."""
        identity = await self.identity(principal)
        if identity is not None:
            await self._contact(identity, principal, request.headers.get("user-agent"))

    async def _contact(
        self, identity: Identity, principal: RequestPrincipal, user_agent: str | None
    ) -> None:
        if not self.tracker.touch(identity.kind, identity.device_id):
            return
        now = self.now()
        if identity.kind == "phone":
            await self.store.touch_phone(
                user_id=principal.user_id, device_id=identity.device_id, now=now
            )
        elif identity.kind == "browser":
            await self.store.touch_browser(
                user_id=principal.user_id,
                web_session_id=identity.device_id,
                now=now,
                label=browser_label(user_agent),
            )

    async def heartbeat(
        self,
        principal: RequestPrincipal,
        report: DeviceStatusReport | None,
        *,
        user_agent: str | None = None,
    ) -> Identity | None:
        """The app or tab is open. Only the app itself (bearer) may report its phone's state."""
        identity = await self.identity(principal)
        if identity is None:
            return None
        await self._contact(identity, principal, user_agent)
        if report is not None and principal.method == "bearer" and identity.kind == "phone":
            await self.store.save_report(
                user_id=principal.user_id,
                device_id=identity.device_id,
                report=report.stored(),
                now=self.now(),
            )
        return identity

    # -- reading ---------------------------------------------------------------------

    async def _all(self, user_id: str) -> list[DeviceRecord]:
        now = self.now()
        records: list[DeviceRecord] = []
        for source in self._sources.values():
            records.extend(await source.list(user_id=user_id, now=now))
        return records

    async def _find(self, user_id: str, device_id: str) -> DeviceRecord | None:
        now = self.now()
        for source in self._sources.values():
            found = await source.get(user_id=user_id, device_id=device_id, now=now)
            if found is not None:
                return found
        return None

    async def _declared(self, user_id: str) -> dict[str, set[str]]:
        """Capabilities each phone's app offers (its declared device tools, M23/M70)."""
        if self._access is None:
            return {}
        try:
            declarations = await self._access.store.declarations(user_id=user_id)
        except Exception:
            logger.warning("device_declarations_lookup_failed")
            return {}
        return {
            item.device_id: {capability_for(tool.name).name for tool in item.tools}
            for item in declarations
        }

    async def _installed(self, user_id: str) -> list[Any]:
        if self._plugins is None:
            return []
        try:
            plugins = await self._plugins.installed(user_id=user_id, workspace_id=None)
        except Exception:
            logger.warning("device_plugins_lookup_failed")
            return []
        return [plugin for plugin in plugins if getattr(plugin, "active", False)]

    def _summary(
        self,
        record: DeviceRecord,
        current: Identity | None,
        declared: set[str] | None,
    ) -> dict[str, Any]:
        now = self.now()
        seen = latest(record.last_seen_at, self.tracker.last_contact(record.kind, record.id))
        presence = presence_of(
            kind=record.kind,
            last_seen_at=seen,
            connected=self.tracker.is_connected(record.kind, record.id),
            push=record.push,
            now=now,
        )
        body: dict[str, Any] = {
            "id": record.id,
            "kind": record.kind,
            "name": record.name,
            "default_name": record.default_name,
            "custom_name": record.custom_name is not None,
            "current": current is not None
            and current.kind == record.kind
            and current.device_id == record.id,
            "platform": {
                "os": record.os,
                "model": record.model,
                "app_version": record.app_version,
                "client": record.client,
            },
            "connected_at": _iso(record.connected_at),
            "last_seen_at": _iso(presence.last_seen_at),
            "presence": {"status": presence.status, "via": presence.via},
            "push": record.push,
        }
        if record.kind == "phone":
            rows = self._capability_rows(record, declared or set(), {}, {})
            body["capabilities_ready"] = sum(1 for row in rows if row["ready"])
            body["capabilities_total"] = len(rows)
        return body

    def _capability_rows(
        self,
        record: DeviceRecord,
        declared: set[str],
        remote: Mapping[str, str],
        uses: Mapping[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        names = list(CAPABILITIES) + sorted(name for name in declared if name not in CAPABILITIES)
        rows: list[dict[str, Any]] = []
        for name in names:
            capability = capability_named(name)
            enabled = name in declared
            permission = record.os_permissions.get(name, "unknown")
            rows.append(
                {
                    "capability": name,
                    "label": capability.label,
                    "kind": capability.kind,
                    "model_sees": capability.model_sees,
                    "persistent_allowed": capability.persistent_allowed,
                    "enabled": enabled,
                    "os_permission": permission,
                    "remote": "deny" if remote.get(name) == "deny" else "ask",
                    "ready": enabled and permission in READY_PERMISSIONS,
                    "plugins": uses.get(name, []),
                }
            )
        return rows

    @staticmethod
    def _uses(plugins: Sequence[Any]) -> dict[str, list[dict[str, Any]]]:
        """Capability → installed plugins that may use it on request («Где работает», M72)."""
        uses: dict[str, list[dict[str, Any]]] = {}
        for plugin in plugins:
            manifest = plugin.manifest
            for item in device_capabilities(manifest.raw):
                uses.setdefault(item.capability, []).append(
                    {
                        "plugin_id": manifest.plugin_id,
                        "name": manifest.name,
                        "description": item.description,
                        "optional": item.optional,
                    }
                )
        return uses

    @staticmethod
    def _cloud(plugins: Sequence[Any]) -> dict[str, Any]:
        items = []
        for plugin in plugins:
            manifest = plugin.manifest
            where = where_it_runs(manifest.raw, device_capabilities(manifest.raw))
            items.append(
                {
                    "plugin_id": manifest.plugin_id,
                    "name": manifest.name,
                    "cloud": where.cloud,
                    "device": where.device,
                }
            )
        return {
            "label": CLOUD_LABEL,
            "status": "online",
            "summary": CLOUD_SUMMARY,
            "plugins": items,
        }

    @staticmethod
    def _sort_key(body: dict[str, Any]) -> tuple[int, int, int, float]:
        """This device first, then phones, nodes, browsers; online first; newest contact first."""
        seen = body["last_seen_at"]
        stamp = datetime.fromisoformat(seen.replace("Z", "+00:00")).timestamp() if seen else 0.0
        return (
            0 if body["current"] else 1,
            KIND_ORDER.get(body["kind"], 9),
            0 if body["presence"]["status"] == "online" else 1,
            -stamp,
        )

    async def list(self, principal: RequestPrincipal) -> dict[str, Any]:
        """``DeviceList``."""
        current = await self.identity(principal)
        records = await self._all(principal.user_id)
        declared = await self._declared(principal.user_id)
        plugins = await self._installed(principal.user_id)
        items = [self._summary(record, current, declared.get(record.id)) for record in records]
        items.sort(key=self._sort_key)
        return {
            "items": items,
            "cloud": self._cloud(plugins),
            "server_time": _iso(self.now()),
            "online_window_seconds": int(ONLINE_WINDOW.total_seconds()),
        }

    async def detail(self, principal: RequestPrincipal, device_id: str) -> dict[str, Any] | None:
        """``DeviceDetail``; ``None`` when the account has no such device."""
        user = principal.user_id
        record = await self._find(user, device_id)
        if record is None:
            return None
        current = await self.identity(principal)
        declared = (await self._declared(user)).get(record.id, set())
        body = self._summary(record, current, declared)
        now = self.now()
        capabilities: list[dict[str, Any]] = []
        grants: list[dict[str, Any]] = []
        links: dict[str, str | None] = {"access_log": None}
        if record.kind == "phone":
            plugins = await self._installed(user)
            remote: dict[str, str] = {}
            if self._access is not None:
                remote = await self._access.remote_settings(user_id=user, device_id=record.id)
                grants = [
                    _grant_body(grant)
                    for grant in await self._access.store.list_grants(
                        user_id=user, device_id=record.id, now=now
                    )
                ]
                links["access_log"] = f"/v1/devices/{record.id}/access-log"
            capabilities = self._capability_rows(record, declared, remote, self._uses(plugins))
            body["notifications"] = record.notifications or "unknown"
            body["reported_at"] = _iso(record.reported_at)
        requester_key = f"device:{record.id}" if record.kind == "phone" else f"web:{record.id}"
        received = await self.store.received_grants(
            user_id=user, requester_key=requester_key, now=now
        )
        body["capabilities"] = capabilities
        body["grants"] = grants
        body["received_grants"] = [
            {
                "id": grant.id,
                "target_device_id": grant.target_device_id,
                "target_device_name": grant.target_device_name,
                "capability": grant.capability,
                "capability_label": capability_named(grant.capability).label,
                "created_at": _iso(grant.created_at),
                "expires_at": _iso(grant.expires_at),
                "last_used_at": _iso(grant.last_used_at),
            }
            for grant in received
        ]
        body["links"] = links
        body["actions"] = {"rename": True, "disconnect": not body["current"]}
        return body

    # -- changing --------------------------------------------------------------------

    async def rename(
        self, principal: RequestPrincipal, device_id: str, name: str | None
    ) -> dict[str, Any] | None:
        record = await self._find(principal.user_id, device_id)
        if record is None:
            return None
        source = self._sources[record.kind]
        await source.rename(user_id=principal.user_id, device_id=record.id, name=clean_name(name))
        return await self.detail(principal, record.id)

    async def disconnect(self, principal: RequestPrincipal, device_id: str) -> DisconnectResult:
        """Sign the device out, drop its push token and permissions (not the current one)."""
        record = await self._find(principal.user_id, device_id)
        if record is None:
            return "not_found"
        current = await self.identity(principal)
        if current is not None and current.kind == record.kind and current.device_id == record.id:
            return "current"
        done = await self._sources[record.kind].disconnect(
            user_id=principal.user_id, device_id=record.id, now=self.now()
        )
        if not done:
            return "not_found"
        self.tracker.forget(record.kind, record.id)
        logger.info("device_disconnected", extra={"device_kind": record.kind})
        return "disconnected"

    async def owns_phone(self, *, user_id: str, device_id: str) -> bool:
        return await self.store.owns_phone(user_id=user_id, device_id=device_id)

    @staticmethod
    def heartbeat_body(identity: Identity) -> dict[str, Any]:
        """``DeviceHeartbeat``."""
        return {
            "device_id": identity.device_id,
            "kind": identity.kind,
            "next_heartbeat_seconds": HEARTBEAT_SECONDS,
        }
