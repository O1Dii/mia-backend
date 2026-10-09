"""Core tools owned by Mia itself: ``background.*`` (M16), ``memory.*`` (M17), ``skills.*``.

They run in-process under the same router policy as plugin tools (schema,
risk/approval, profile filter, audit). M13 ships the namespaces as empty
extension points; M16/M17 register their tools with :meth:`CoreToolRegistry.register`.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from app.core.ports.tool_router import ToolContext
from app.core.ports.types import JsonObject, Risk, RunProfile

RESERVED_NAMESPACES: frozenset[str] = frozenset({"background", "memory", "skills", "schedule"})
_NAME = re.compile(r"^((background|memory|skills|schedule)\.[a-z][a-z0-9_]{0,63}|notify)$")
"""``schedule.*`` and the bare ``notify`` were added by M16 (Review §5.2(d) tool names)."""


@dataclass(frozen=True, slots=True)
class CoreToolResult:
    content: str
    structured_content: JsonObject | None = None
    ui_resource_uri: str | None = None
    status: Literal["succeeded", "failed"] = "succeeded"
    error_code: str | None = None


CoreToolHandler = Callable[[ToolContext, JsonObject], Awaitable[CoreToolResult]]
"""``handler(context, validated_arguments) -> CoreToolResult``."""


@dataclass(frozen=True, slots=True)
class CoreTool:
    name: str
    """Qualified name in a reserved namespace, e.g. ``background.start``."""
    description: str
    input_schema: JsonObject
    risk: Risk
    handler: CoreToolHandler
    voice: bool = False
    timeout_s: float = 10.0
    profiles: frozenset[RunProfile] = field(
        default_factory=lambda: frozenset({"voice", "chat", "job"})
    )
    """Profiles that see the tool (``app`` runs get core tools only when listed here)."""


class CoreToolRegistry:
    def __init__(self, tools: Iterable[CoreTool] = ()) -> None:
        self._tools: dict[str, CoreTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: CoreTool) -> None:
        if not _NAME.match(tool.name):
            raise ValueError("core tools live in background.*, memory.*, skills.*, schedule.*")
        if tool.name in self._tools:
            raise ValueError(f"duplicate core tool {tool.name}")
        if tool.timeout_s <= 0:
            raise ValueError("timeout must be positive")
        self._tools[tool.name] = tool

    def get(self, name: str) -> CoreTool | None:
        return self._tools.get(name)

    def tools(self) -> list[CoreTool]:
        return list(self._tools.values())

    def namespace(self, prefix: str) -> list[CoreTool]:
        if prefix not in RESERVED_NAMESPACES:
            raise ValueError("unknown core namespace")
        return [tool for tool in self._tools.values() if tool.name.startswith(prefix + ".")]
