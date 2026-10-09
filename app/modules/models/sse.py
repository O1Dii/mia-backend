"""Minimal async Server-Sent Events parser shared by the streaming adapters."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator


async def iter_sse(lines: AsyncIterator[str]) -> AsyncGenerator[tuple[str, str], None]:
    """Yield ``(event, data)`` pairs from already split lines; comments are ignored."""
    event = "message"
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:][1:] if line[5:6] == " " else line[5:])
    if data:
        yield event, "\n".join(data)
