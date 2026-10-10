"""Device access policy (ADR-0016, M70): ``allow | ask | deny`` over
(capability, requester, target, scope), the capability vocabulary and data minimization.

- **Capability** — what a device tool does on the phone (``contacts.read``, ``alarm.clock``…).
  Reads of device data and user-only actions may be granted for the run or persistently;
  actions with an external effect (Telegram draft to a person) are asked per action.
- **Requester** — the client a run came from: the app on a device, the app's WebView on that
  device (same device, M24 hand-off), a browser session, or an unknown client.
- **Target** — the device that executes the tool. The requester on the target device itself
  needs nothing here (the OS permission and Mia's on-device switch are the phone's job); any
  other client needs consent given on the target device.

Pure functions only; persistence is :mod:`app.modules.tools.device_store`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

CapabilityKind = Literal["read", "action", "external"]
RequesterKind = Literal["device_app", "device_webview", "web", "unknown", "job", "app"]
PolicyValue = Literal["allow", "ask", "deny"]
RemoteSetting = Literal["ask", "deny"]
GrantScope = Literal["run", "persistent"]

CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
PERSISTENT_GRANT_DAYS = 30
CONSENT_WAIT_SECONDS = 120
"""``waiting_for_device``: how long a cross-client request waits for the phone (ADR-0016 §8)."""


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    kind: CapabilityKind
    label: str
    """Russian, lower case, fits «<клиент> просит: <label>»."""
    model_sees: str
    """What the model receives from the device (shown in the consent text)."""

    @property
    def persistent_allowed(self) -> bool:
        """External effects are approved per action, never pre-allowed (ADR-0016 §5)."""
        return self.kind != "external"

    @property
    def run_scoped(self) -> bool:
        """One consent covers repeated calls of the capability in the same run."""
        return self.kind != "external"


CAPABILITIES: dict[str, Capability] = {
    "contacts.read": Capability(
        "contacts.read",
        "read",
        "поиск в контактах телефона",
        "только имена найденных контактов и их число, без номеров",
    ),
    "alarm.clock": Capability(
        "alarm.clock",
        "action",
        "будильник в «Часах» телефона",
        "время будильника и поставлен ли он",
    ),
    "apps.open": Capability(
        "apps.open",
        "action",
        "открыть приложение на телефоне",
        "открылось ли приложение",
    ),
    "telegram.draft": Capability(
        "telegram.draft",
        "external",
        "черновик сообщения в Telegram",
        "открылся ли Telegram; отправляете вы сами",
    ),
}
TOOL_CAPABILITIES: dict[str, str] = {
    "device.contacts.search": "contacts.read",
    "device.alarm.set": "alarm.clock",
    "device.app.open": "apps.open",
    "telegram.handoff": "telegram.draft",
}


def capability_for(tool: str) -> Capability:
    """Capability of a device tool; an unknown tool is its own, strictest capability."""
    known = TOOL_CAPABILITIES.get(tool)
    if known is not None:
        return CAPABILITIES[known]
    return Capability(tool, "external", f"действие на телефоне ({tool})", "результат действия")


@dataclass(frozen=True, slots=True)
class Requester:
    kind: RequesterKind
    label: str
    device_id: str | None = None
    """Verified device of the requester (its app or its WebView)."""
    web_session_id: str | None = None

    @property
    def key(self) -> str | None:
        """Stable identity a persistent grant is bound to ("this browser / this device")."""
        if self.device_id is not None:
            return f"device:{self.device_id}"
        if self.web_session_id is not None:
            return f"web:{self.web_session_id}"
        return None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    value: PolicyValue
    reason: str
    """``same_device``, ``run_grant``, ``persistent_grant``, ``consent_required``,
    ``capability_disabled``, ``requester_not_allowed``."""
    grant_id: str | None = None


def same_device(requester: Requester, target_device_id: str) -> bool:
    return requester.device_id is not None and requester.device_id == target_device_id


def decide(
    capability: Capability,
    requester: Requester,
    target_device_id: str,
    *,
    remote: RemoteSetting = "ask",
    run_grant_id: str | None = None,
    persistent_grant_id: str | None = None,
) -> PolicyDecision:
    """The single policy of ADR-0016 §11 for one device call."""
    if same_device(requester, target_device_id):
        return PolicyDecision("allow", "same_device")
    if requester.kind in ("job", "app"):
        # Jobs run in the worker (no device channel there) and third-party apps never
        # reach the phone; both are refused, not asked (no silent fallback either).
        return PolicyDecision("deny", "requester_not_allowed")
    if remote == "deny":
        return PolicyDecision("deny", "capability_disabled")
    if capability.run_scoped and run_grant_id is not None:
        return PolicyDecision("allow", "run_grant", run_grant_id)
    if capability.persistent_allowed and persistent_grant_id is not None and requester.key:
        return PolicyDecision("allow", "persistent_grant", persistent_grant_id)
    return PolicyDecision("ask", "consent_required")


def allowed_scopes(capability: Capability, requester: Requester) -> list[GrantScope]:
    scopes: list[GrantScope] = ["run"]
    if capability.persistent_allowed and requester.key is not None:
        scopes.append("persistent")
    return scopes


# -- data minimization -----------------------------------------------------------------

_INTERNATIONAL = re.compile(r"(?<![\w+])\+\d[\d\s().-]{6,}\d")
_TEL_URI = re.compile(r"tel:[^\s\"'<>]+", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"(?<![\w])\(?\+?\d[\d\s().-]{5,}\d(?![\w])")
PHONE_PLACEHOLDER = "[номер скрыт]"
CONTACT_FIELDS = ("name", "phone_count")


def _digits(value: str) -> int:
    return sum(char.isdigit() for char in value)


def scrub_phone_numbers(text: str, *, strict: bool = False) -> str:
    """Replace phone numbers by a placeholder.

    Always: ``tel:`` URIs and ``+``-prefixed international numbers (≥ 8 digits). ``strict``
    (contact data): any run of ≥ 7 digits with phone separators as well.
    """
    text = _TEL_URI.sub(PHONE_PLACEHOLDER, text)
    text = _INTERNATIONAL.sub(
        lambda m: PHONE_PLACEHOLDER if _digits(m.group(0)) >= 8 else m.group(0), text
    )
    if strict:
        text = _DIGIT_RUN.sub(
            lambda m: PHONE_PLACEHOLDER if _digits(m.group(0)) >= 7 else m.group(0), text
        )
    return text


def _scrub_value(value: Any, *, strict: bool) -> Any:
    if isinstance(value, str):
        return scrub_phone_numbers(value, strict=strict)
    if isinstance(value, list):
        return [_scrub_value(item, strict=strict) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item, strict=strict) for key, item in value.items()}
    return value


def _contact_matches(structured: Mapping[str, Any]) -> dict[str, Any]:
    """``{"matches": [{"name", "phone_count"?}]}`` from any contact list shape (M17 reader)."""
    matches = structured.get("matches", structured.get("contacts", structured.get("results")))
    kept: list[dict[str, Any]] = []
    if isinstance(matches, list):
        for match in matches[:20]:
            if not isinstance(match, dict):
                continue
            name = match.get("name") or match.get("display_name")
            if not isinstance(name, str) or not name.strip():
                continue
            entry: dict[str, Any] = {"name": scrub_phone_numbers(name[:120], strict=True)}
            count = match.get("phone_count")
            phones = match.get("phones", match.get("phone"))
            if count is None and phones is not None:
                count = len(phones) if isinstance(phones, list) else 1
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 99:
                entry["phone_count"] = count
            kept.append(entry)
    return {"matches": kept}


def minimize_device_result(
    tool: str, content: str, structured: Mapping[str, Any] | None
) -> tuple[str, dict[str, Any] | None]:
    """Keep only what the cloud may see from a device result (ADR-0016 §6).

    Contact reads keep names and counts only (whatever the device sent); every device
    result loses phone numbers in text and structured data.
    """
    contacts = capability_for(tool).name == "contacts.read"
    text = scrub_phone_numbers(content, strict=contacts)
    if structured is None:
        return text, None
    if contacts:
        return text, _contact_matches(structured)
    return text, _scrub_value(dict(structured), strict=False)
