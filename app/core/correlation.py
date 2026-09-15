"""Request correlation IDs, request metrics and completion logs for HTTP and WebSocket."""

from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.metrics import METRICS, MetricsRegistry

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
request_id_var: ContextVar[str | None] = ContextVar("mia_request_id", default=None)


def current_request_id() -> str | None:
    """Return the correlation ID of the request being served, if any."""
    return request_id_var.get()


def resolve_request_id(supplied: str | None) -> str:
    """Accept only a short opaque client ID; otherwise generate a server ID."""
    if supplied is not None and _REQUEST_ID.fullmatch(supplied):
        return supplied
    return uuid4().hex


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class CorrelationMiddleware:
    """Pure ASGI middleware, so streaming responses and WebSockets keep the context."""

    def __init__(self, app: ASGIApp, *, metrics: MetricsRegistry = METRICS) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_supplied = headers.get(REQUEST_ID_HEADER.lower().encode())
        supplied = raw_supplied.decode("latin-1") if raw_supplied is not None else None
        request_id = resolve_request_id(supplied)
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        started = time.monotonic()
        try:
            if scope["type"] == "http":
                await self._http(scope, receive, send, request_id, started)
            else:
                await self._websocket(scope, receive, send, request_id, started)
        finally:
            request_id_var.reset(token)

    async def _http(
        self, scope: Scope, receive: Receive, send: Send, request_id: str, started: float
    ) -> None:
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration = time.monotonic() - started
            method = str(scope.get("method", "")).upper()
            method = method if method in _METHODS else "OTHER"
            route = _route_template(scope)
            self.metrics.inc(
                "mia_http_requests_total",
                {"method": method, "route": route, "status": f"{status_code // 100}xx"},
            )
            self.metrics.observe(
                "mia_http_request_duration_seconds", duration, {"method": method, "route": route}
            )
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "method": method,
                    "route": route,
                    "status_code": status_code,
                    "duration_ms": int(duration * 1000),
                },
            )

    async def _websocket(
        self, scope: Scope, receive: Receive, send: Send, request_id: str, started: float
    ) -> None:
        close_code: int | None = None

        async def observe_close(message: Message) -> None:
            nonlocal close_code
            if message["type"] == "websocket.close":
                close_code = int(message.get("code", 1000))
            await send(message)

        self.metrics.inc("mia_websocket_connections_total")
        self.metrics.add("mia_websocket_connections_active", 1)
        try:
            await self.app(scope, receive, observe_close)
        finally:
            self.metrics.add("mia_websocket_connections_active", -1)
            logger.info(
                "websocket_completed",
                extra={
                    "request_id": request_id,
                    "route": _route_template(scope),
                    "close_code": close_code,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
