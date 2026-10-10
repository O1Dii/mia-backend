"""Screen-only actions of tool results (M41-R1, ``common.schema.json#/$defs/screenAction``).

A trusted (in-process) tool may attach one action to its result under
``CallToolResult._meta["mia/action"]``, for example the one-time Telegram bot link:
``{"kind": "open_url", "url": "https://t.me/…", "label": "Открыть Telegram",
"expires_at": "…Z"}``. The router validates it, the run engine passes it to the UI
in ``tool.call.completed`` and keeps it with the answer in the feed. It never reaches
the model: neither the tool's text nor the conversation history carries the URL.

Only ``https://`` URLs with a host and ``tg://`` links are accepted; anything else is
dropped. Expired actions are listed without their URL so clients render them disabled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

ACTION_META_KEY: Final = "mia/action"
ACTION_KINDS: Final = frozenset({"open_url"})
LABEL_MAX: Final = 40
URL_MAX: Final = 2048
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f<>\"'`\\]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_url(value: object) -> str | None:
    """The URL when it may be opened from a button (``https`` with a host, or ``tg``)."""
    if not isinstance(value, str) or not value or len(value) > URL_MAX or _UNSAFE.search(value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme == "https":
        if not parts.hostname or "@" in parts.netloc:
            return None
        return value
    if scheme == "tg" and (parts.netloc or parts.path):
        return value
    return None


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_action(raw: object) -> dict[str, Any] | None:
    """A validated ``screenAction`` (with ``url``) or None when anything is off."""
    if not isinstance(raw, Mapping):
        return None
    kind, label = raw.get("kind"), raw.get("label")
    if kind not in ACTION_KINDS or not isinstance(label, str):
        return None
    label = label.strip()
    if not label or len(label) > LABEL_MAX or _CONTROL.search(label):
        return None
    url = safe_url(raw.get("url"))
    if url is None:
        return None
    action: dict[str, Any] = {"kind": kind, "url": url, "label": label}
    expires = raw.get("expires_at")
    if expires is not None:
        stamp = _timestamp(expires)
        if stamp is None:
            return None
        action["expires_at"] = stamp
    return action


def action_from_meta(meta: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The action of a tool result's ``_meta`` (``mia/action``), validated."""
    if not meta:
        return None
    return parse_action(meta.get(ACTION_META_KEY))


def is_expired(action: Mapping[str, Any], now: datetime) -> bool:
    expires = action.get("expires_at")
    if not isinstance(expires, str):
        return False
    try:
        return datetime.fromisoformat(expires) <= now
    except ValueError:
        return True


def listed_action(raw: object, now: datetime) -> dict[str, Any] | None:
    """A stored action as the feed lists it: expired ones lose the URL (``expired: true``)."""
    action = parse_action(raw)
    if action is None:
        return None
    if is_expired(action, now):
        action.pop("url")
        action["expired"] = True
    return action
