"""Per-call identity of a plugin tool call (``tools/call`` request ``_meta``).

The tool router (M13) stamps four keys on every call; the in-process servers read them
instead of trusting arguments. A missing or malformed key is a clear tool error, never a
crash: the model (or the router log) sees which key is absent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from mcp.server.mcpserver.exceptions import ToolError

META_USER_ID: Final = "mia/user_id"
META_WORKSPACE_ID: Final = "mia/workspace_id"
META_INSTALLATION_ID: Final = "mia/installation_id"
META_RUN_ID: Final = "mia/run_id"
REQUIRED_META_KEYS: Final = (META_USER_ID, META_WORKSPACE_ID, META_INSTALLATION_ID)
MAX_ID_LENGTH: Final = 128


@dataclass(frozen=True, slots=True)
class CallIdentity:
    user_id: str
    workspace_id: str
    installation_id: str
    run_id: str | None = None


def identity_from_meta(meta: Mapping[str, Any] | None) -> CallIdentity:
    """Build the identity of a call or raise ``ToolError`` naming the missing keys."""
    values = meta or {}
    missing = [key for key in REQUIRED_META_KEYS if not _valid_id(values.get(key))]
    if missing:
        raise ToolError(
            "Вызов без идентификации: в _meta запроса нет или неверны ключи "
            + ", ".join(missing)
            + ". Инструмент вызывается только через Mia."
        )
    run_id = values.get(META_RUN_ID)
    if run_id is not None and not _valid_id(run_id):
        raise ToolError(f"Вызов без идентификации: неверен ключ {META_RUN_ID} в _meta запроса.")
    return CallIdentity(
        user_id=str(values[META_USER_ID]),
        workspace_id=str(values[META_WORKSPACE_ID]),
        installation_id=str(values[META_INSTALLATION_ID]),
        run_id=None if run_id is None else str(run_id),
    )


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH
