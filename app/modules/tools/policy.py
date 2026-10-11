"""Pure policy helpers: argument checks, untrusted-result framing, approval text."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]

from app.core.ports.types import JsonObject

MAX_ARGUMENT_BYTES = 16 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
MAX_DESCRIPTION_CHARS = 1024
SUMMARY_MAX_CHARS = 500
REF_MAX_CHARS = 64
NAME_MAX_CHARS = 120

_RESULT_OPEN = "<tool_result"
_RESULT_CLOSE = "</tool_result>"
_TAG = re.compile(r"<\s*/?\s*tool_result", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REF_HINT = re.compile(r"\b(person|contact):")


def encoded_arguments(arguments: object) -> bytes | None:
    """Canonical UTF-8 JSON of an argument object, or None if not a JSON object."""
    if not isinstance(arguments, Mapping):
        return None
    try:
        return json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return None


def compile_schema(schema: object) -> Draft202012Validator | None:
    """A validator for a tool ``inputSchema`` (object root, bounded size) or None."""
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        return None
    try:
        size = len(json.dumps(schema, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        return None
    if size > MAX_SCHEMA_BYTES:
        return None
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return None
    return Draft202012Validator(dict(schema))


def arguments_valid(validator: Draft202012Validator, arguments: JsonObject) -> bool:
    """JSON-Schema check without echoing values; references are never fetched."""
    encoded = encoded_arguments(arguments)
    if encoded is None or len(encoded) > MAX_ARGUMENT_BYTES:
        return False
    try:
        return next(iter(validator.iter_errors(json.loads(encoded))), None) is None
    except Exception:
        return False


def untrusted(tool: str, text: str, *, limit: int) -> str:
    """Frame tool output as data for the model (Review §5.3).

    The model receives ``<tool_result tool="…" trust="untrusted">…</tool_result>``;
    any tag-like sequence inside the output is defused so the output cannot close
    the frame early or open a new one. Nothing in a result is ever interpreted
    by the router: results cannot create approvals or trigger tool calls.
    """
    body = _CONTROL.sub("", text)
    if len(body) > limit:
        body = body[: max(0, limit - 1)] + "…"
    body = _TAG.sub(lambda match: match.group(0).replace("<", "‹"), body)
    return f'{_RESULT_OPEN} tool="{tool}" trust="untrusted">\n{body}\n{_RESULT_CLOSE}'


def structured_object(value: object, *, limit_bytes: int) -> JsonObject | None:
    """``structuredContent`` as a JSON object for ``display`` (non-objects are wrapped)."""
    if value is None:
        return None
    body: Mapping[str, Any] = value if isinstance(value, Mapping) else {"value": value}
    try:
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    if len(encoded.encode("utf-8")) > limit_bytes:
        return None
    parsed = json.loads(encoded)
    return parsed if isinstance(parsed, dict) else None


def ref_arguments(schema: object, arguments: JsonObject) -> dict[str, str]:
    """Top-level string arguments a tool documents as people refs, stripped (M41-R2).

    An argument counts when its property schema mentions ``person:`` or ``contact:`` in the
    ``description`` or ``pattern`` (``builtin.telegram.send_message.to``: "the ref
    (person:<id>, contact:<n>) or name"). Other arguments (a message ``text`` that happens to
    look like a ref) are never replaced on the approval card.
    """
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping):
        return {}
    found: dict[str, str] = {}
    for key, value in arguments.items():
        prop = properties.get(key)
        if not isinstance(value, str) or not isinstance(prop, Mapping):
            continue
        hint = " ".join(str(prop.get(field) or "") for field in ("description", "pattern"))
        ref = value.strip()
        if _REF_HINT.search(hint) and 0 < len(ref) <= REF_MAX_CHARS:
            found[key] = ref
    return found


def _name_label(label: str) -> str:
    cleaned = " ".join(_CONTROL.sub(" ", label).replace("«", '"').replace("»", '"').split())
    return cleaned if len(cleaned) <= NAME_MAX_CHARS else cleaned[: NAME_MAX_CHARS - 1] + "…"


def approval_summary(
    plugin_name: str,
    local_tool: str,
    arguments: JsonObject,
    names: Mapping[str, str] | None = None,
) -> str:
    """Core-rendered approval text: «Linear: delete_issue — id="ABC-1"» (≤ 500 chars).

    ``names`` (argument → display name, from the owner's memory, M41-R2) replaces a people ref
    with the name the user knows: «Telegram: send_message — to=«Яна Петрова (жена)»,
    text="…"». Named arguments come first so a clipped voice prompt still says who; the
    approval itself stays bound to the raw arguments (``args_hash``).
    """
    labels = {key: _name_label((names or {}).get(key) or "") for key in sorted(arguments)}
    parts = [f"{key}=«{label}»" for key, label in labels.items() if label]
    for key in sorted(arguments):
        if labels[key]:
            continue
        try:
            value = json.dumps(arguments[key], ensure_ascii=False)
        except (TypeError, ValueError):
            value = "…"
        if len(value) > 160:
            value = value[:159] + "…"
        parts.append(f"{key}={value}")
    text = f"{plugin_name}: {local_tool}"
    if parts:
        text += " — " + ", ".join(parts)
    text = _CONTROL.sub(" ", text)
    return text if len(text) <= SUMMARY_MAX_CHARS else text[: SUMMARY_MAX_CHARS - 1] + "…"


def clip_description(text: str) -> str:
    cleaned = _CONTROL.sub(" ", text).strip()
    return cleaned[:MAX_DESCRIPTION_CHARS]
