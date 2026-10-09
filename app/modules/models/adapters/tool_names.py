"""Reversible per-request aliases for tool names (OI-M13-001).

Router tool names look like ``builtin.timer.start``; OpenAI-compatible APIs accept only
``^[a-zA-Z0-9_-]{1,64}$``. Adapters send the alias and map calls back to the router name.
The mapping is a pure function of the names in the request (tools and replayed tool
calls), so every step of a run encodes the same history the same way.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

from app.core.ports.model_gateway import ModelRequest

MAX_NAME: Final = 64
_VALID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(name: str) -> str:
    alias = _INVALID_CHARS.sub("_", name.replace(".", "__")) or "tool"
    if len(alias) > MAX_NAME:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        alias = f"{alias[: MAX_NAME - 9]}_{digest}"
    return alias


class ToolNames:
    def __init__(self, names: set[str]) -> None:
        self._wire: dict[str, str] = {}
        self._canonical: dict[str, str] = {}
        # Valid names keep their spelling and win collisions; the rest get a suffix.
        for name in sorted(names, key=lambda n: (_VALID.fullmatch(n) is None, n)):
            alias = name if _VALID.fullmatch(name) else _sanitize(name)
            base, index = alias, 2
            while alias in self._canonical:
                suffix = f"_{index}"
                alias = base[: MAX_NAME - len(suffix)] + suffix
                index += 1
            self._wire[name] = alias
            self._canonical[alias] = name

    @classmethod
    def for_request(cls, request: ModelRequest) -> ToolNames:
        names = {tool.name for tool in request.tools}
        for message in request.messages:
            names.update(call.name for call in message.tool_calls)
        return cls(names)

    def wire(self, name: str) -> str:
        return self._wire.get(name, name)

    def canonical(self, alias: str) -> str:
        """Router name of a model-returned alias; unknown aliases pass through unchanged."""
        return self._canonical.get(alias, alias)
