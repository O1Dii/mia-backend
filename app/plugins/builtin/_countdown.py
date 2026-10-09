"""Server factory shared by the timer and focus plugins (a countdown with pause/resume)."""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult
from pydantic import Field

from app.modules.plugins.builtin import deadlines
from app.modules.plugins.builtin.kit import PluginKit, load_ui, ok
from app.modules.plugins.builtin.services import HostServices

Id = Annotated[str | None, Field(description="Id from get; may be omitted when only one fits")]


def build_countdown_server(
    services: HostServices,
    manifest: Mapping[str, Any],
    plugin_dir: Path,
    *,
    multiple: bool,
    default_label: str,
    default_seconds: int,
    max_seconds: int,
    noun: str,
) -> MCPServer:
    """``multiple`` timers (with ids) or a single slot that ``start`` replaces."""
    kit = PluginKit(manifest, services, ui=load_ui(plugin_dir, ("tile",)))
    max_active = 10 if multiple else 1

    def payload(
        items: list[deadlines.Item], now: int, changed: deadlines.Item | None
    ) -> dict[str, Any]:
        shown = [deadlines.view(item, now) for item in items]
        return {
            "items": shown,
            "changed": None if changed is None else deadlines.view(changed, now),
            "server_time_ms": now,
        }

    def snapshot(data: dict[str, Any]) -> list[deadlines.Item]:
        items: list[deadlines.Item] = data.setdefault("items", [])
        return items

    start_doc = f"Start a {noun} for `seconds` with a `label`." + (
        " Several can run at once." if multiple else " Replaces the current one."
    )

    @kit.tool("start", description=start_doc, ui="tile")
    async def start(
        seconds: Annotated[
            int, Field(ge=1, le=max_seconds, description="Duration in seconds (5 min = 300)")
        ] = default_seconds,
        label: Annotated[str, Field(min_length=1, max_length=80)] = default_label,
        *,
        ctx: Context,
    ) -> CallToolResult:
        identity = kit.identity(ctx)
        now = services.now_ms()

        def mutate(data: dict[str, Any]) -> tuple[list[deadlines.Item], deadlines.Item]:
            items = snapshot(data)
            if not multiple:
                items.clear()
            item = deadlines.add(
                items,
                now_ms=now,
                new_id=services.new_id,
                label=label,
                deadline_ms=now + seconds * 1000,
                status="running",
                max_active=max_active,
            )
            return items, item

        items, item = await kit.update(identity, mutate)
        text = f"{noun.capitalize()} «{label}» запущен на {deadlines.format_ms(seconds * 1000)}."
        return ok(text + f" id {item['id']}.", payload(items, now, item))

    @kit.tool("get", description=f"Read the current {noun} state (read only).", ui="tile")
    async def get(*, ctx: Context) -> CallToolResult:
        identity = kit.identity(ctx)
        now = services.now_ms()
        items = snapshot(await kit.read(identity))
        text = deadlines.describe(items, now, empty=f"Активных записей ({noun}) нет.")
        return ok(text, payload(items, now, None))

    def control(name: str, only: str | None, doc: str) -> None:
        @kit.tool(name, description=doc, ui="tile")
        async def action(id: Id = None, *, ctx: Context) -> CallToolResult:
            identity = kit.identity(ctx)
            now = services.now_ms()

            def mutate(data: dict[str, Any]) -> tuple[list[deadlines.Item], deadlines.Item]:
                items = snapshot(data)
                item = deadlines.select(items, id, now_ms=now, only=only)
                if name == "pause":
                    deadlines.pause(item, now)
                elif name == "resume":
                    deadlines.resume(item, now)
                else:
                    items.remove(item)
                return items, item

            items, item = await kit.update(identity, mutate)
            done = {"pause": "на паузе", "resume": "продолжен", "cancel": "отменён"}[name]
            return ok(f"{noun.capitalize()} «{item['label']}» {done}.", payload(items, now, item))

    control("pause", "running", f"Pause a running {noun}.")
    control("resume", "paused", f"Resume a paused {noun}.")
    control("cancel", None, f"Cancel (delete) a {noun}.")
    kit.verify_tools()
    return kit.server
