"""Notes and tasks (port of ``mia.notes`` from the web POC)."""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult
from pydantic import Field

from app.modules.plugins.builtin.kit import PluginKit, load_ui, ok
from app.modules.plugins.builtin.services import HostServices

PLUGIN_ID = "builtin.notes"
MAX_NOTES = 200

NoteId = Annotated[str, Field(min_length=1, max_length=50, description="Note id from list")]


def build_server(
    services: HostServices, manifest: Mapping[str, Any], plugin_dir: Path
) -> MCPServer:
    kit = PluginKit(manifest, services, ui=load_ui(plugin_dir, ("tile",)))

    def notes_of(data: dict[str, Any]) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = data.setdefault("notes", [])
        return notes

    def payload(notes: list[dict[str, Any]], changed: dict[str, Any] | None) -> dict[str, Any]:
        return {"notes": notes, "changed": changed, "server_time_ms": services.now_ms()}

    def find(notes: list[dict[str, Any]], note_id: str) -> dict[str, Any]:
        for note in notes:
            if note["id"] == note_id:
                return note
        raise ToolError("Заметка не найдена. Сначала вызовите list и возьмите id оттуда.")

    @kit.tool("add", description="Save a note or task.", ui="tile")
    async def add(
        text: Annotated[str, Field(min_length=1, max_length=5000)], *, ctx: Context
    ) -> CallToolResult:
        identity = kit.identity(ctx)

        def mutate(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            notes = notes_of(data)
            if len(notes) >= MAX_NOTES:
                raise ToolError(f"Достигнут лимит заметок ({MAX_NOTES}). Удалите ненужные.")
            used = {note["id"] for note in notes}
            note_id = services.new_id()
            while note_id in used:
                note_id = services.new_id()
            note = {"id": note_id, "text": text, "done": False, "created_ms": services.now_ms()}
            notes.insert(0, note)
            return notes, note

        notes, note = await kit.update(identity, mutate)
        return ok(f"Заметка сохранена (id {note['id']}).", payload(notes, note))

    @kit.tool("list", description="List notes with their ids (read only).", ui="tile")
    async def list_notes(*, ctx: Context) -> CallToolResult:
        identity = kit.identity(ctx)
        notes = notes_of(await kit.read(identity))
        if not notes:
            return ok("Заметок нет.", payload(notes, None))
        lines = [f"{n['id']} [{'x' if n['done'] else ' '}] {n['text'][:200]}" for n in notes]
        return ok("Заметки:\n" + "\n".join(lines), payload(notes, None))

    @kit.tool("toggle", description="Toggle completion of a task by id.", ui="tile")
    async def toggle(id: NoteId, *, ctx: Context) -> CallToolResult:
        identity = kit.identity(ctx)

        def mutate(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            notes = notes_of(data)
            note = find(notes, id)
            note["done"] = not note["done"]
            return notes, note

        notes, note = await kit.update(identity, mutate)
        state = "выполнена" if note["done"] else "снова открыта"
        return ok(f"Задача {state}.", payload(notes, note))

    @kit.tool("delete", description="Delete a note by id.", ui="tile")
    async def delete(id: NoteId, *, ctx: Context) -> CallToolResult:
        identity = kit.identity(ctx)

        def mutate(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            notes = notes_of(data)
            note = find(notes, id)
            notes.remove(note)
            return notes, note

        notes, note = await kit.update(identity, mutate)
        return ok("Заметка удалена.", payload(notes, note))

    kit.verify_tools()
    return kit.server
