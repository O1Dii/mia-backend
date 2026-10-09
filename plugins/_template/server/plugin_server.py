"""Reference MCP server of the plugin template (``server.kind: remote``, Streamable HTTP).

Deploy it on your own host; Mia never runs this code. Mia connects to ``/mcp``, lists the
tools and checks them against ``mia.json``: the names must match exactly, otherwise the
publication is refused. Keep the tool names and the ``risk`` classes in both places in sync.

Run locally (listens on 127.0.0.1:8000, the path is ``/mcp``)::

    uv run python plugins/_template/server/plugin_server.py

The notes list below is in-memory demo state. A real server maps the bearer token to a user
and keeps the state in its own storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

SERVER_NAME = "plugin-template"
MAX_NOTE_LENGTH = 500


def _result(summary: str, data: dict[str, Any]) -> CallToolResult:
    """``content`` is for the model, ``structuredContent`` is for the tile and the apps."""
    return CallToolResult(content=[TextContent(type="text", text=summary)], structured_content=data)


def build_server() -> MCPServer:
    server = MCPServer(SERVER_NAME, version="0.1.0")
    notes: list[str] = []

    def get_status() -> CallToolResult:
        updated = datetime.now(UTC).isoformat(timespec="seconds")
        return _result(
            f"Заметок: {len(notes)}",
            {"notes_count": len(notes), "updated_at": updated},
        )

    def add_note(text: str) -> CallToolResult:
        cleaned = text.strip()
        if not cleaned or len(cleaned) > MAX_NOTE_LENGTH:
            raise ToolError(f"Текст заметки: от 1 до {MAX_NOTE_LENGTH} символов.")
        notes.append(cleaned)
        return _result(
            f"Заметка сохранена. Всего заметок: {len(notes)}",
            {"notes_count": len(notes), "index": len(notes)},
        )

    server.add_tool(
        get_status,
        name="get_status",
        description="Number of notes and when the status was read (read-only).",
    )
    server.add_tool(
        add_note,
        name="add_note",
        description="Save a short note the user dictated. Reversible; no approval needed.",
    )
    return server


def main() -> None:
    build_server().run("streamable-http", stateless_http=True, json_response=True)


if __name__ == "__main__":
    main()
