"""How many plugin tools Mia offers the model (M15-R1).

One model request carries every tool of the run: core tools (``background.*``,
``schedule.*``, ``memory.*``), ``skills.load``, the device's tools and the tools of
every active installation. OpenAI-compatible providers reject a request with more
than :data:`MODEL_TOOL_LIMIT` function tools, so the plugins of one user share a
budget and the rest is reserved for the host:

- :data:`MAX_PLUGIN_TOOLS` — tools one installation may declare (``mia.json``
  ``tools``, the add-by-URL selection); the same number everywhere: manifest schema,
  ``parse_manifest``, the add-by-URL probe and consent, the registry;
- :data:`PLUGIN_TOOL_BUDGET` — tools all active installations of a user may offer
  together; the registry trims installations past it (later installations first,
  destructive tools first) and reports them, it never drops one silently.

Descriptions are clipped to 1024 characters by the router (``clip_description``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

MODEL_TOOL_LIMIT = 128
"""Function tools one model request may carry."""
HOST_TOOL_RESERVE = 32
"""Core tools, ``skills.load`` and device tools of one run (about 20 today)."""
PLUGIN_TOOL_BUDGET = MODEL_TOOL_LIMIT - HOST_TOOL_RESERVE
"""Tools all active installations of one user offer together."""
MAX_PLUGIN_TOOLS = PLUGIN_TOOL_BUDGET
"""Tools one installation may declare (``mia.json`` ``tools``, add-by-URL selection)."""
SELECT_ALL_UP_TO = 24
"""Add-by-URL preselects every tool of a server this small; a larger one starts without
destructive tools (delete, archive, …), which the user can still tick."""

_READ_VERBS = frozenset(
    {
        "get",
        "list",
        "search",
        "find",
        "read",
        "fetch",
        "query",
        "retrieve",
        "view",
        "show",
        "count",
        "lookup",
        "describe",
        "check",
    }
)
_MAIN_WRITE_VERBS = frozenset(
    {
        "create",
        "save",
        "update",
        "add",
        "comment",
        "post",
        "send",
        "set",
        "edit",
        "assign",
        "upload",
        "attach",
        "reply",
        "mark",
        "move",
        "start",
        "complete",
    }
)
_DESTRUCTIVE_VERBS = frozenset(
    {
        "delete",
        "remove",
        "archive",
        "destroy",
        "purge",
        "drop",
        "erase",
        "wipe",
        "revoke",
        "reset",
        "clear",
        "unassign",
        "unlink",
    }
)


def tool_tier(name: str) -> int:
    """0 read, 1 main write, 2 other, 3 destructive (by the leading verb of the name)."""
    verb = name.split("_", 1)[0].lower()
    if verb in _READ_VERBS:
        return 0
    if verb in _MAIN_WRITE_VERBS:
        return 1
    if verb in _DESTRUCTIVE_VERBS:
        return 3
    return 2


def destructive(name: str) -> bool:
    return tool_tier(name) == 3


def rank_tools(names: Iterable[str]) -> list[str]:
    """Names by tier (reads first, destructive last), stable within a tier."""
    ordered = list(names)
    return sorted(ordered, key=tool_tier)


def keep_within(names: Sequence[str], limit: int) -> list[str]:
    """The first ``limit`` names by rank, in their original order."""
    if len(names) <= limit:
        return list(names)
    kept = set(rank_tools(names)[: max(limit, 0)])
    return [name for name in names if name in kept]


def default_selection(names: Sequence[str], limit: int) -> list[str]:
    """Tools ticked on the add-by-URL consent screen before the user changes anything."""
    chosen = (
        list(names)
        if len(names) <= SELECT_ALL_UP_TO
        else [name for name in names if not destructive(name)]
    )
    return keep_within(chosen, limit)
