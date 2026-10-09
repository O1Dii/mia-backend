"""Deadline bookkeeping for timer, alarm and focus.

These plugins only *store* deadlines in ``PluginState``; firing (push, sound) is the job
scheduler's work (M16). The stored item shape is a public contract for M16/M31::

    {"id", "label", "status": "running" | "paused" | "scheduled",
     "deadline_ms": epoch ms | None, "remaining_ms": int, "duration_ms": int,
     "created_ms": epoch ms}

``running``/``scheduled`` items have a deadline; ``paused`` ones keep ``remaining_ms``.
An item whose deadline passed is reported as ``expired`` but stays stored (until pruned)
so M16 can still deliver it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

Item = dict[str, Any]
EXPIRED_RETENTION_MS = 24 * 3600 * 1000


def prune(items: list[Item], now_ms: int) -> None:
    items[:] = [
        item
        for item in items
        if item["status"] == "paused" or (item["deadline_ms"] or 0) > now_ms - EXPIRED_RETENTION_MS
    ]


def active_count(items: list[Item], now_ms: int) -> int:
    return sum(1 for item in items if state_of(item, now_ms) != "expired")


def state_of(item: Item, now_ms: int) -> str:
    if item["status"] == "paused":
        return "paused"
    return "expired" if item["deadline_ms"] <= now_ms else item["status"]


def remaining_ms(item: Item, now_ms: int) -> int:
    if item["status"] == "paused":
        return int(item["remaining_ms"])
    return max(0, int(item["deadline_ms"]) - now_ms)


def view(item: Item, now_ms: int) -> Item:
    return {**item, "state": state_of(item, now_ms), "remaining_ms": remaining_ms(item, now_ms)}


def add(
    items: list[Item],
    *,
    now_ms: int,
    new_id: Callable[[], str],
    label: str,
    deadline_ms: int,
    status: str,
    max_active: int,
) -> Item:
    prune(items, now_ms)
    if active_count(items, now_ms) >= max_active:
        raise ToolError(f"Слишком много активных записей (максимум {max_active}). Отмените лишние.")
    used = {item["id"] for item in items}
    item_id = new_id()
    while item_id in used:
        item_id = new_id()
    item: Item = {
        "id": item_id,
        "label": label,
        "status": status,
        "deadline_ms": deadline_ms,
        "remaining_ms": deadline_ms - now_ms,
        "duration_ms": deadline_ms - now_ms,
        "created_ms": now_ms,
    }
    items.append(item)
    return item


def select(items: list[Item], item_id: str | None, *, now_ms: int, only: str | None = None) -> Item:
    """Find the target of pause/resume/cancel; ambiguity is an error the model can resolve."""
    if item_id is not None:
        for item in items:
            if item["id"] == item_id:
                return item
        raise ToolError(f"Запись {item_id} не найдена. Вызовите get, чтобы увидеть список.")
    pool = [item for item in items if only is None or state_of(item, now_ms) == only]
    if not pool:
        raise ToolError("Подходящих записей нет. Вызовите get, чтобы увидеть список.")
    if len(pool) > 1:
        listing = ", ".join(f"{item['id']} «{item['label']}»" for item in pool)
        raise ToolError(f"Записей несколько ({listing}). Уточните id.")
    return pool[0]


def pause(item: Item, now_ms: int) -> None:
    if state_of(item, now_ms) != "running":
        raise ToolError("Пауза возможна только для идущего таймера.")
    item.update(status="paused", remaining_ms=remaining_ms(item, now_ms), deadline_ms=None)


def resume(item: Item, now_ms: int) -> None:
    if item["status"] != "paused" or item["remaining_ms"] <= 0:
        raise ToolError("Продолжить можно только таймер на паузе.")
    item.update(status="running", deadline_ms=now_ms + item["remaining_ms"])


def format_ms(value: int) -> str:
    seconds = (max(value, 0) + 999) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def describe(items: list[Item], now_ms: int, *, empty: str) -> str:
    if not items:
        return empty
    lines = []
    for item in items:
        shown = view(item, now_ms)
        status = {
            "running": f"осталось {format_ms(shown['remaining_ms'])}",
            "paused": f"на паузе, осталось {format_ms(shown['remaining_ms'])}",
            "scheduled": f"сработает через {format_ms(shown['remaining_ms'])}",
            "expired": "время вышло",
        }[shown["state"]]
        lines.append(f"{item['id']} «{item['label']}»: {status}")
    return "; ".join(lines)


def pending(items: list[Item]) -> list[tuple[str, str, int]]:
    """``(id, label, deadline_ms)`` of items that still wait to fire (for M16)."""
    return [
        (item["id"], item["label"], int(item["deadline_ms"]))
        for item in items
        if item["status"] in {"running", "scheduled"} and item["deadline_ms"] is not None
    ]
