"""Tool-call log material for ``tool.call.*`` events (M73): arguments and the error reason.

The events are the user's tool-call journal, kept with the run, so what goes in is bounded
and stripped of personal data:

- values under secret-looking keys, e-mail addresses, phone-like numbers and long opaque
  tokens are replaced by short markers;
- arguments of **device** tools (contacts, messages, calendar of the phone) keep only their
  shape: strings become ``[скрыто]`` (ADR-0016: data minimisation);
- the error reason is the first line(s) of the tool's own message without the
  ``<tool_result>`` frame the router puts around tool output for the model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

MASK: Final = "[скрыто]"
EMAIL_MASK: Final = "[email]"
PHONE_MASK: Final = "[телефон]"
MAX_STRING: Final = 300
MAX_ITEMS: Final = 20
MAX_DEPTH: Final = 4
MAX_ARGUMENTS_BYTES: Final = 2000
MAX_REASON: Final = 300

_SECRET_KEY = re.compile(
    r"pass(word|wd)?|secret|token|authorization|api[-_]?key|credential|cookie|bearer|signature",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{6,}\d(?![\w])")
_OPAQUE = re.compile(r"(?<![\w-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}")
_FRAME = re.compile(r"^\s*<tool_result\b[^>]*>\n?(.*?)\n?</tool_result>\s*$", re.DOTALL)


def scrub_text(text: str) -> str:
    """E-mail addresses, phone-like numbers and opaque tokens out of free text."""
    cleaned = _EMAIL.sub(EMAIL_MASK, text)
    cleaned = _PHONE.sub(PHONE_MASK, cleaned)
    return _OPAQUE.sub(MASK, cleaned)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _redact(value: Any, *, device: bool, depth: int) -> Any:
    if isinstance(value, str):
        if device:
            return MASK
        return _clip(scrub_text(value), MAX_STRING)
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= MAX_DEPTH:
        return MASK
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                result["…"] = len(value) - MAX_ITEMS
                break
            name = _clip(str(key), 64)
            if _SECRET_KEY.search(name):
                result[name] = MASK
            else:
                result[name] = _redact(item, device=device, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        items = [_redact(item, device=device, depth=depth + 1) for item in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"… +{len(value) - MAX_ITEMS}")
        return items
    return MASK


def redact_arguments(arguments: Mapping[str, Any], *, device: bool = False) -> dict[str, Any]:
    """Bounded, personal-data-free copy of tool arguments for ``tool.call.started``."""
    redacted = _redact(arguments, device=device, depth=0)
    if not isinstance(redacted, dict):
        return {}
    encoded = json.dumps(redacted, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= MAX_ARGUMENTS_BYTES:
        return redacted
    # Too large for the journal: keep the keys and short scalar values only.
    shrunk: dict[str, Any] = {}
    for key, item in redacted.items():
        shrunk[key] = item if isinstance(item, bool | int | float | None) else _brief(item)
    shrunk["_truncated"] = True
    return shrunk


def _brief(value: Any) -> Any:
    if isinstance(value, str):
        return _clip(value, 80)
    if isinstance(value, list):
        return f"[{len(value)} элементов]"
    if isinstance(value, dict):
        return f"{{{len(value)} полей}}"
    return MASK


def failure_reason(content: str) -> str | None:
    """Why a tool call did not succeed, from the tool's own message (never the model frame)."""
    text = content.strip()
    if not text:
        return None
    framed = _FRAME.match(text)
    if framed is not None:
        text = framed.group(1).strip()
    text = " ".join(text.split())
    if not text:
        return None
    return _clip(scrub_text(text), MAX_REASON)
