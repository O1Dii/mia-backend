"""Record/replay transport for adapter tests.

A *cassette* is a JSON file: ``{"name", "interactions": [{"request", "response"}]}``.

* ``request``: ``{"method", "url", "forbid_body_keys": [...]}``; the body itself is
  asserted by the tests through ``ReplayTransport.requests``.
* ``response``: ``{"status", "headers", "events": [{"event", "data"}]}`` for a streamed
  body (``event`` may be ``null``; ``data`` may be a JSON value or a raw string such as
  ``"[DONE]"``) or ``{"json": ...}`` / ``{"text": ...}`` for a plain body.

Cassettes are produced by ``python -m app.modules.models.smoke --record DIR`` against the
live provider (secrets and ids are scrubbed) or written by hand for failure modes that a
live run cannot reproduce on demand (usage limit, 503, 401).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

CASSETTES = Path(__file__).parent / "cassettes"


def load(name: str | Path) -> dict[str, Any]:
    path = name if isinstance(name, Path) else CASSETTES / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def render_events(events: list[dict[str, Any]]) -> bytes:
    chunks: list[str] = []
    for item in events:
        data = item["data"]
        payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        head = f"event: {item['event']}\n" if item.get("event") else ""
        chunks.append(f"{head}data: {payload}\n\n")
    return "".join(chunks).encode()


class TrackedStream(httpx.AsyncByteStream):
    """Streams a body in small chunks and records whether it was closed early."""

    def __init__(self, body: bytes, chunk: int = 64) -> None:
        self._body = body
        self._chunk = chunk
        self.closed = False
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._body), self._chunk):
            self.sent += 1
            yield self._body[start : start + self._chunk]

    async def aclose(self) -> None:
        self.closed = True


class ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, *cassettes: str | Path) -> None:
        self._queue: list[dict[str, Any]] = []
        for name in cassettes:
            self._queue.extend(load(name)["interactions"])
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.streams: list[TrackedStream] = []

    @property
    def remaining(self) -> int:
        return len(self._queue)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert self._queue, f"unexpected request {request.method} {request.url.path}"
        step = self._queue.pop(0)
        expected = step["request"]
        assert request.method == expected["method"]
        assert str(request.url) == expected["url"], str(request.url)
        body = json.loads(request.content) if request.content else {}
        for key in expected.get("forbid_body_keys", []):
            assert key not in body, f"request must not send {key!r}"
        self.requests.append(request)
        self.bodies.append(body)
        spec = step["response"]
        headers = dict(spec.get("headers", {}))
        if "events" in spec:
            headers.setdefault("content-type", "text/event-stream")
            stream = TrackedStream(render_events(spec["events"]))
            self.streams.append(stream)
            return httpx.Response(spec["status"], headers=headers, stream=stream)
        if "json" in spec:
            return httpx.Response(spec["status"], headers=headers, json=spec["json"])
        return httpx.Response(spec["status"], headers=headers, text=spec.get("text", ""))
