"""The phone's status report sent with its heartbeat (``DeviceStatusReport``, M84).

Only names and states: the OS and model strings the phone shows in its own settings, the app
version, whether notifications are allowed and the OS permission per capability. Never phone
numbers, contacts or other content. Strings are cleaned of control characters and of anything
that looks like a phone number before they are stored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.tools.device_policy import CAPABILITY_PATTERN, scrub_phone_numbers

OsPermission = Literal["granted", "denied", "not_required", "unknown"]
NotificationState = Literal["granted", "denied", "unknown"]
MAX_REPORTED_CAPABILITIES = 32
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(value: str | None, limit: int) -> str | None:
    """Single-line, no control characters, no phone numbers, bounded; ``None`` when empty."""
    if value is None:
        return None
    text = " ".join(_CONTROL.sub(" ", value).split())
    text = scrub_phone_numbers(text, strict=True)
    return text[:limit] or None


class CapabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=3, max_length=64, pattern=CAPABILITY_PATTERN.pattern)
    os_permission: OsPermission


class DeviceStatusReport(BaseModel):
    """``DeviceStatusReport`` (``devices.schema.json``)."""

    model_config = ConfigDict(extra="forbid")

    os: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    app_version: str | None = Field(default=None, max_length=64)
    notifications: NotificationState = "unknown"
    capabilities: list[CapabilityReport] = Field(
        default_factory=list, max_length=MAX_REPORTED_CAPABILITIES
    )

    def stored(self) -> dict[str, Any]:
        """The JSON kept in ``devices.status_report`` (cleaned, one entry per capability)."""
        permissions: dict[str, str] = {}
        for item in self.capabilities:
            permissions[item.capability] = item.os_permission
        return {
            "os": clean_text(self.os, 64),
            "model": clean_text(self.model, 128),
            "app_version": clean_text(self.app_version, 64),
            "notifications": self.notifications,
            "capabilities": permissions,
        }


def os_permissions(stored: Mapping[str, Any] | None) -> dict[str, str]:
    raw = (stored or {}).get("capabilities")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): str(state)
        for name, state in raw.items()
        if state in ("granted", "denied", "not_required", "unknown")
    }


def report_text(stored: Mapping[str, Any] | None, key: str) -> str | None:
    value = (stored or {}).get(key)
    return value if isinstance(value, str) and value else None
