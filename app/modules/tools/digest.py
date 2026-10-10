"""Digest of a server's tool descriptions, pinned at consent (Review §5.4.3).

``tools_digest = "sha256:" + hex(sha256(canonical JSON))`` of the list of
``{"name", "description", "input_schema"}`` for every tool the server lists,
sorted by name. Canonical JSON is the ``canonical_args_hash`` form: keys sorted,
separators ``(",", ":")``, non-ASCII kept, UTF-8. ``description`` is ``""`` when
absent. Publication (M15) and builtin packaging (M14) must compute the catalog
digest with :func:`compute_tools_digest` over the same ``tools/list`` result;
any later change of a description or schema blocks the plugin's tools until the
user consents again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.core.ports.types import JsonObject


@dataclass(frozen=True, slots=True)
class ServerTool:
    """One entry of an MCP ``tools/list`` result, as the router uses it."""

    name: str
    description: str
    input_schema: JsonObject
    meta: JsonObject | None = None
    annotations: JsonObject | None = None
    """MCP ``ToolAnnotations`` (``readOnlyHint``, ``destructiveHint``); not part of the digest."""

    def digest_entry(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


def compute_tools_digest(tools: Iterable[ServerTool | Mapping[str, object]]) -> str:
    """Return ``sha256:<hex>`` over the canonical tool descriptions."""
    entries: list[dict[str, object]] = []
    for tool in tools:
        if isinstance(tool, ServerTool):
            entries.append(tool.digest_entry())
        else:
            schema = tool.get("input_schema", tool.get("inputSchema", {}))
            entries.append(
                {
                    "name": str(tool.get("name", "")),
                    "description": str(tool.get("description") or ""),
                    "input_schema": dict(schema) if isinstance(schema, Mapping) else {},
                }
            )
    entries.sort(key=lambda entry: str(entry["name"]))
    canonical = json.dumps(
        entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
