"""Translate neutral tool JSON Schemas into the subset Gemini accepts.

`ToolDefinition.parameters` is a provider-neutral JSON Schema, and the registry
builds it with `BaseModel.model_json_schema()`. That document legitimately
carries keywords Gemini's `Schema` type does not define — `additionalProperties`
and `$defs`/`$ref` above all — and both Gemini surfaces reject them:

* the Gen AI SDK validates `FunctionDeclaration.parameters` and raises on any
  extra key, so every text turn fails before a request is sent;
* the Live wire API refuses the setup frame, so the session never connects.

Translating at the provider boundary keeps the registry provider-neutral: the
adapter owns the mapping to its own wire format, exactly as it already does for
messages and events.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# Keys of the OpenAPI 3.0 subset Gemini documents for function-declaration
# schemas. Anything outside this set is dropped rather than forwarded.
_SUPPORTED_KEYS = frozenset(
    {
        "anyOf",
        "default",
        "description",
        "enum",
        "example",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "nullable",
        "pattern",
        "properties",
        "propertyOrdering",
        "required",
        "title",
        "type",
    }
)

_MAX_REF_DEPTH = 8


def to_gemini_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of `schema` restricted to the keywords Gemini accepts.

    `$ref` targets are inlined from `$defs`, so nested argument models survive
    the translation instead of collapsing into an empty object.
    """
    defs = schema.get("$defs")
    definitions: Mapping[str, Any] = defs if isinstance(defs, Mapping) else {}
    return _convert(schema, definitions, depth=0)


def _convert(
    node: Mapping[str, Any], definitions: Mapping[str, Any], *, depth: int
) -> dict[str, Any]:
    resolved = _resolve_ref(node, definitions, depth=depth)
    converted: dict[str, Any] = {}
    for key, value in resolved.items():
        if key not in _SUPPORTED_KEYS:
            continue
        if key == "properties" and isinstance(value, Mapping):
            converted[key] = {
                name: _convert(child, definitions, depth=depth)
                for name, child in value.items()
                if isinstance(child, Mapping)
            }
        elif key in {"items", "additionalItems"} and isinstance(value, Mapping):
            converted[key] = _convert(value, definitions, depth=depth)
        elif key == "anyOf" and isinstance(value, Sequence) and not isinstance(value, str | bytes):
            converted[key] = [
                _convert(child, definitions, depth=depth)
                for child in value
                if isinstance(child, Mapping)
            ]
        else:
            converted[key] = value

    # `const` has no Gemini counterpart but a single-valued enum expresses it.
    const = resolved.get("const")
    if const is not None and "enum" not in converted:
        converted["enum"] = [const]
    return converted


def _resolve_ref(
    node: Mapping[str, Any], definitions: Mapping[str, Any], *, depth: int
) -> Mapping[str, Any]:
    ref = node.get("$ref")
    if not isinstance(ref, str) or depth >= _MAX_REF_DEPTH:
        return node
    prefix = "#/$defs/"
    if not ref.startswith(prefix):
        return node
    target = definitions.get(ref[len(prefix) :])
    if not isinstance(target, Mapping):
        return node
    # Sibling keys of `$ref` (for example `description`) override the target.
    merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return _resolve_ref(merged, definitions, depth=depth + 1)
