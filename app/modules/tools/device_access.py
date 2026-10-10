"""Device access service (ADR-0016, M70): who asks, which device, policy, consent, audit.

The router asks :class:`DeviceAccess` before every device call:

- the **requester** comes from the run's origin (``POST /v1/runs`` records it, see
  :class:`app.modules.tools.device_origin.RunOriginResolver`); runs without an origin
  fall back to their profile;
- the **target** is the user's phone that most recently declared its device tools;
- :func:`~app.modules.tools.device_policy.decide` gives ``allow | ask | deny``;
- ``ask`` becomes an M13 approval carrying ``device_consent``: only the target device may
  decide it (``/v1/approvals/{id}/decision``), the phone gets a push with actions, the run
  waits up to :data:`~app.modules.tools.device_policy.CONSENT_WAIT_SECONDS`
  (``waiting_for_device``);
- requests of other clients are written to the device access log (ADR-0016 §10).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.core.ports.approval_service import Approval
from app.core.ports.device_tool_channel import DeviceDelivery, DeviceToolCall, DeviceToolDeclaration
from app.core.ports.tool_router import ToolContext
from app.core.ports.types import JsonObject
from app.modules.tools.device_policy import (
    CONSENT_WAIT_SECONDS,
    PERSISTENT_GRANT_DAYS,
    Capability,
    GrantScope,
    PolicyDecision,
    RemoteSetting,
    Requester,
    allowed_scopes,
    decide,
)
from app.modules.tools.device_store import (
    AccessLogEntry,
    DeviceAccessStore,
    GrantRecord,
    RunOrigin,
)

logger = logging.getLogger(__name__)

ORIGIN_CACHE = 2048
LIVE_WINDOW_S = 300.0
"""A device that talked to the API this recently counts as reachable without a push token."""
RUN_GRANT_TTL = timedelta(hours=2)
"""A run grant is looked up by run id; this only bounds a row nobody cleans up."""
_SUMMARY_ARGUMENTS = ("query", "name", "label", "package", "contact_name", "username", "at")


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TargetDevice:
    device_id: str
    name: str | None
    tools: tuple[DeviceToolDeclaration, ...]

    @property
    def label(self) -> str:
        return self.name or "телефон"


class DeviceNotifier(Protocol):
    """Push to one device (``app.modules.notifications.device_push.DevicePush``)."""

    async def reachable(self, *, user_id: str, device_id: str) -> bool: ...

    async def consent_requested(
        self, *, user_id: str, device_id: str, approval: Approval
    ) -> bool: ...

    async def call_waiting(self, *, user_id: str, device_id: str, call: DeviceToolCall) -> bool: ...


def _clip(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def request_subject(arguments: Mapping[str, Any]) -> str | None:
    """The short user-visible subject of a device call («Яна», «Telegram»)."""
    for key in _SUMMARY_ARGUMENTS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value, 60)
    return None


def consent_summary(
    requester: Requester, capability: Capability, arguments: Mapping[str, Any]
) -> str:
    subject = request_subject(arguments)
    what = f"{capability.label} «{subject}»" if subject else capability.label
    return _clip(f"Подтвердите на телефоне: {requester.label} просит {what}.", 500)


class DeviceAccess:
    def __init__(
        self,
        store: DeviceAccessStore,
        *,
        notifier: DeviceNotifier | None = None,
        clock: Callable[[], datetime] = utc_now,
        consent_wait_s: int = CONSENT_WAIT_SECONDS,
        grant_days: int = PERSISTENT_GRANT_DAYS,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self._clock = clock
        self.consent_wait_s = consent_wait_s
        self._grant_ttl = timedelta(days=grant_days)
        self._origins: OrderedDict[tuple[str, str], RunOrigin | None] = OrderedDict()
        self._seen: dict[tuple[str, str], float] = {}

    def bind_notifier(self, notifier: DeviceNotifier) -> None:
        self.notifier = notifier

    # -- liveness ------------------------------------------------------------------------

    def seen(self, *, user_id: str, device_id: str) -> None:
        """The device talked to the API (registration, polling, a result)."""
        self._seen[(user_id, device_id)] = time.monotonic()

    def live(self, *, user_id: str, device_id: str) -> bool:
        at = self._seen.get((user_id, device_id))
        return at is not None and time.monotonic() - at <= LIVE_WINDOW_S

    async def reachable(self, *, user_id: str, device_id: str) -> bool:
        if self.live(user_id=user_id, device_id=device_id):
            return True
        notifier = self.notifier
        if notifier is None:
            return False
        try:
            return await notifier.reachable(user_id=user_id, device_id=device_id)
        except Exception:
            logger.warning("device_reachability_failed")
            return False

    # -- requester and target ------------------------------------------------------------

    async def record_origin(self, origin: RunOrigin) -> None:
        try:
            await self.store.record_origin(origin, now=self._clock())
        except Exception:
            logger.warning("run_origin_record_failed")
            return
        self._remember((origin.user_id, origin.run_id), origin)

    def remember(self, origin: RunOrigin) -> None:
        """Make the origin known in this process before it reaches the database."""
        self._remember((origin.user_id, origin.run_id), origin)

    def _remember(self, key: tuple[str, str], origin: RunOrigin | None) -> None:
        self._origins[key] = origin
        self._origins.move_to_end(key)
        while len(self._origins) > ORIGIN_CACHE:
            self._origins.popitem(last=False)

    async def origin(self, *, user_id: str, run_id: str) -> RunOrigin | None:
        key = (user_id, run_id)
        if key in self._origins:
            return self._origins[key]
        try:
            found = await self.store.origin(user_id=user_id, run_id=run_id)
        except Exception:
            logger.warning("run_origin_lookup_failed")
            return None
        if found is not None:
            self._remember(key, found)
        return found

    async def requester(self, context: ToolContext) -> Requester:
        origin = await self.origin(user_id=context.user_id, run_id=context.run_id)
        if origin is not None:
            return Requester(origin.kind, origin.label, origin.device_id, origin.web_session_id)
        if context.profile == "job":
            return Requester("job", "фоновая задача Mia")
        if context.profile == "app":
            return Requester("app", "стороннее приложение")
        if context.device_id is not None:
            # Runs that carry a device but no recorded origin (started inside the core).
            return Requester("device_app", "приложение Mia", device_id=context.device_id)
        return Requester("unknown", "другой клиент Mia")

    async def delivery(self, *, user_id: str, run_id: str, device_id: str) -> DeviceDelivery:
        """``stream`` only when the app on that device started the run (it follows it)."""
        origin = await self.origin(user_id=user_id, run_id=run_id)
        if origin is None:
            return "stream"
        if origin.kind == "device_app" and origin.device_id == device_id:
            return "stream"
        return "push"

    async def target(self, user_id: str) -> TargetDevice | None:
        """The phone that most recently declared device tools (no silent second choice)."""
        try:
            found = await self.store.declarations(user_id=user_id)
        except Exception:
            logger.warning("device_declarations_lookup_failed")
            return None
        for item in found:
            if item.tools:
                return TargetDevice(item.device_id, item.name, item.tools)
        return None

    async def remote_settings(self, *, user_id: str, device_id: str) -> dict[str, str]:
        try:
            return await self.store.remote_settings(user_id=user_id, device_id=device_id)
        except Exception:
            logger.warning("device_settings_lookup_failed")
            return {}

    # -- policy --------------------------------------------------------------------------

    async def evaluate(
        self,
        context: ToolContext,
        requester: Requester,
        target_device_id: str,
        capability: Capability,
    ) -> PolicyDecision:
        settings = await self.remote_settings(user_id=context.user_id, device_id=target_device_id)
        remote: RemoteSetting = "deny" if settings.get(capability.name) == "deny" else "ask"
        match = await self.store.find_grants(
            user_id=context.user_id,
            target_device_id=target_device_id,
            capability=capability.name,
            run_id=context.run_id,
            requester_key=requester.key,
            now=self._clock(),
        )
        return decide(
            capability,
            requester,
            target_device_id,
            remote=remote,
            run_grant_id=match.run_grant_id,
            persistent_grant_id=match.persistent_grant_id,
        )

    def consent_body(
        self,
        requester: Requester,
        target: TargetDevice,
        capability: Capability,
    ) -> JsonObject:
        """``DeviceConsent`` (``device-access.schema.json``)."""
        return {
            "waiting_for": "device",
            "target_device_id": target.device_id,
            "target_device_name": target.name,
            "requester": {"kind": requester.kind, "label": requester.label},
            "capability": capability.name,
            "capability_kind": capability.kind,
            "capability_label": capability.label,
            "model_sees": capability.model_sees,
            "scopes": list(allowed_scopes(capability, requester)),
        }

    # -- audit ---------------------------------------------------------------------------

    async def log(
        self,
        context: ToolContext,
        requester: Requester,
        *,
        device_id: str,
        capability: Capability,
        tool: str,
        decision: str,
        approval: Approval | None = None,
    ) -> None:
        try:
            await self.store.log(
                AccessLogEntry(
                    id=str(uuid.uuid4()),
                    user_id=context.user_id,
                    device_id=device_id,
                    capability=capability.name,
                    tool=tool,
                    requester_kind=requester.kind,
                    requester_label=requester.label,
                    decision=decision,
                    created_at=self._clock(),
                    run_id=context.run_id,
                    approval_id=approval.id if approval is not None else None,
                    expires_at=approval.expires_at if approval is not None else None,
                )
            )
        except Exception:
            logger.warning("device_access_log_failed")

    async def granted(self, decision: PolicyDecision) -> None:
        if decision.grant_id is None:
            return
        try:
            await self.store.touch_grant(decision.grant_id, now=self._clock())
        except Exception:
            logger.warning("device_grant_touch_failed")

    # -- consent -------------------------------------------------------------------------

    async def consent_requested(self, *, user_id: str, device_id: str, approval: Approval) -> None:
        notifier = self.notifier
        if notifier is None:
            return
        try:
            await notifier.consent_requested(
                user_id=user_id, device_id=device_id, approval=approval
            )
        except Exception:
            logger.warning("device_consent_push_failed")

    async def call_waiting(self, *, user_id: str, call: DeviceToolCall) -> None:
        notifier = self.notifier
        if notifier is None:
            return
        try:
            await notifier.call_waiting(user_id=user_id, device_id=call.device_id, call=call)
        except Exception:
            logger.warning("device_call_push_failed")

    async def consumed(
        self,
        context: ToolContext,
        requester: Requester,
        *,
        target_device_id: str,
        capability: Capability,
        approval_id: str | None,
    ) -> None:
        """An approved consent was used: it covers the rest of the run (not external acts)."""
        if not capability.run_scoped:
            return
        now = self._clock()
        try:
            await self.store.add_grant(
                GrantRecord(
                    id=str(uuid.uuid4()),
                    user_id=context.user_id,
                    target_device_id=target_device_id,
                    capability=capability.name,
                    scope="run",
                    run_id=context.run_id,
                    requester_kind=requester.kind,
                    requester_label=requester.label,
                    requester_key=requester.key,
                    requester_device_id=requester.device_id,
                    requester_web_session_id=requester.web_session_id,
                    approval_id=approval_id,
                    created_at=now,
                    expires_at=now + RUN_GRANT_TTL,
                )
            )
        except Exception:
            logger.warning("device_run_grant_failed")

    async def decided(
        self, *, user_id: str, approval: Approval, decision: str, scope: GrantScope
    ) -> None:
        """Called by the approvals API after the target device decided."""
        consent = approval.device_consent or {}
        status = "approved" if decision == "approve" else "denied"
        try:
            await self.store.resolve_log(user_id=user_id, approval_id=approval.id, decision=status)
        except Exception:
            logger.warning("device_access_log_failed")
        if decision != "approve" or scope != "persistent":
            return
        capability = str(consent.get("capability") or "")
        target = str(consent.get("target_device_id") or "")
        origin = await self.origin(user_id=user_id, run_id=approval.run_id)
        if not capability or not target or origin is None:
            return
        requester = Requester(origin.kind, origin.label, origin.device_id, origin.web_session_id)
        if requester.key is None:
            return
        now = self._clock()
        await self.store.add_grant(
            GrantRecord(
                id=str(uuid.uuid4()),
                user_id=user_id,
                target_device_id=target,
                capability=capability,
                scope="persistent",
                requester_kind=requester.kind,
                requester_label=requester.label,
                requester_key=requester.key,
                requester_device_id=requester.device_id,
                requester_web_session_id=requester.web_session_id,
                approval_id=approval.id,
                created_at=now,
                expires_at=now + self._grant_ttl,
            )
        )
