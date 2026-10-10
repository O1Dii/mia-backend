"""Alarm: absolute-time reminders; the deadline is stored, firing is M16's job.

The alarm lives in Mia (cloud, ADR-0016): it is pushed and shown on the dashboard; the
phone presents it as a Mia alarm. The system Clock (``device.alarm.set``) is never set
implicitly: after the user's first alarm the result asks the model to offer it once
(``clock_offer`` in the plugin state remembers that the offer was made).
"""

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult
from pydantic import Field

from app.modules.plugins.builtin import deadlines
from app.modules.plugins.builtin.kit import PluginKit, load_ui, ok
from app.modules.plugins.builtin.services import HostServices

PLUGIN_ID = "builtin.alarm"
MAX_AHEAD_MS = 31 * 86400 * 1000
MAX_ACTIVE = 10
CLOCK_OFFER_KEY = "clock_offer"
CLOCK_OFFER = (
    "Это первый будильник пользователя: один раз предложи поставить его ещё в «Часах» "
    "на телефоне (device.alarm.set), сам не ставь."
)


def build_server(
    services: HostServices, manifest: Mapping[str, Any], plugin_dir: Path
) -> MCPServer:
    kit = PluginKit(manifest, services, ui=load_ui(plugin_dir, ("tile",)))

    def payload(
        items: list[deadlines.Item], now: int, changed: deadlines.Item | None
    ) -> dict[str, Any]:
        return {
            "items": [deadlines.view(item, now) for item in items],
            "changed": None if changed is None else deadlines.view(changed, now),
            "server_time_ms": now,
        }

    def stored(data: dict[str, Any]) -> list[deadlines.Item]:
        items: list[deadlines.Item] = data.setdefault("items", [])
        return items

    @kit.tool(
        "set",
        description=(
            "Set an alarm at an absolute time. `at` is ISO 8601 WITH a UTC offset, "
            "e.g. 2026-10-05T14:00:00+03:00; up to 31 days ahead."
        ),
        ui="tile",
    )
    async def set_alarm(
        at: Annotated[str, Field(max_length=50, description="ISO 8601 with UTC offset")],
        label: Annotated[str, Field(min_length=1, max_length=80)] = "Будильник",
        *,
        ctx: Context,
    ) -> CallToolResult:
        identity = kit.identity(ctx)
        now = services.now_ms()
        try:
            moment = datetime.fromisoformat(at)
        except ValueError:
            raise ToolError(
                "Время должно быть в формате ISO 8601, например 2026-10-05T14:00:00+03:00."
            ) from None
        if moment.tzinfo is None:
            raise ToolError("Укажите часовой пояс (смещение UTC) во времени будильника.")
        deadline = int(moment.timestamp() * 1000)
        if not now < deadline <= now + MAX_AHEAD_MS:
            raise ToolError("Время будильника должно быть в будущем, не дальше чем через 31 день.")

        def mutate(
            data: dict[str, Any],
        ) -> tuple[list[deadlines.Item], deadlines.Item, bool]:
            items = stored(data)
            first = not data.get(CLOCK_OFFER_KEY)
            data[CLOCK_OFFER_KEY] = True
            item = deadlines.add(
                items,
                now_ms=now,
                new_id=services.new_id,
                label=label,
                deadline_ms=deadline,
                status="scheduled",
                max_active=MAX_ACTIVE,
            )
            item["at"] = moment.isoformat()
            return items, item, first

        items, item, first = await kit.update(identity, mutate)
        text = f"Будильник «{label}» установлен в Mia на {item['at']}. id {item['id']}."
        if first:
            text += " " + CLOCK_OFFER
        return ok(text, payload(items, now, item))

    @kit.tool("get", description="List alarms (read only).", ui="tile")
    async def get(*, ctx: Context) -> CallToolResult:
        identity = kit.identity(ctx)
        now = services.now_ms()
        items = stored(await kit.read(identity))
        text = deadlines.describe(items, now, empty="Будильников нет.")
        return ok(text, payload(items, now, None))

    @kit.tool("cancel", description="Cancel one alarm by id, or all with cancel_all.", ui="tile")
    async def cancel(
        id: Annotated[str | None, Field(description="Alarm id from get")] = None,
        cancel_all: bool = False,
        *,
        ctx: Context,
    ) -> CallToolResult:
        identity = kit.identity(ctx)
        now = services.now_ms()

        def mutate(data: dict[str, Any]) -> tuple[list[deadlines.Item], list[deadlines.Item]]:
            items = stored(data)
            if cancel_all:
                removed = list(items)
                items.clear()
            else:
                item = deadlines.select(items, id, now_ms=now)
                items.remove(item)
                removed = [item]
            return items, removed

        items, removed = await kit.update(identity, mutate)
        names = ", ".join(f"«{item['label']}»" for item in removed) or "нечего отменять"
        return ok(f"Отменено: {names}.", payload(items, now, removed[0] if removed else None))

    kit.verify_tools()
    return kit.server
