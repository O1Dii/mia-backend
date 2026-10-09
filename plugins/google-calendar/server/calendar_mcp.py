"""Read-only Google Calendar MCP server (the remote server behind ``team.google_calendar``).

It is deployed separately from Mia (the release stores only the package data, never code).
Mia connects the user's Google account over OAuth and sends the Google access token as the
bearer of every MCP request; this server forwards it to the Calendar REST API untouched and
never stores it. Required scope: ``https://www.googleapis.com/auth/calendar.events.readonly``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

API_BASE = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
MAX_RANGE = timedelta(days=31)
MAX_RESULTS_CAP = 100
MAX_TITLE = 200

TokenSource = Callable[[Context[Any, Any]], str]


def bearer_from_request(ctx: Context[Any, Any]) -> str:
    """The Google access token Mia sent as the MCP bearer."""
    request = getattr(ctx.request_context, "request", None)
    header = request.headers.get("authorization", "") if request is not None else ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ToolError("Google Календарь не подключён: нужно войти в аккаунт Google.")
    return token.strip()


def day_bounds(day: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    local = day.astimezone(zone).date()
    start = datetime.combine(local, time.min, tzinfo=zone)
    return start, datetime.combine(local + timedelta(days=1), time.min, tzinfo=zone)


def normalize_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """The fields the model and the tile need; cancelled events are dropped."""
    if raw.get("status") == "cancelled":
        return None
    start, end = raw.get("start") or {}, raw.get("end") or {}
    all_day = "date" in start and "dateTime" not in start
    attendees = raw.get("attendees") or []
    return {
        "id": str(raw.get("id", "")),
        "title": str(raw.get("summary") or "(без названия)")[:MAX_TITLE],
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": all_day,
        "location": str(raw["location"])[:MAX_TITLE] if raw.get("location") else None,
        "attendees": len(attendees),
        "link": raw.get("htmlLink"),
    }


def _sort_key(event: dict[str, Any]) -> tuple[str, str]:
    # all-day events first, then by start; date and dateTime strings both sort by calendar order
    return (event["start"] or "")[:10], "" if event["all_day"] else (event["start"] or "")


def _summary(events: list[dict[str, Any]], label: str) -> str:
    if not events:
        return f"{label}: событий нет."
    lines = [
        f"- {'весь день' if e['all_day'] else (e['start'] or '')[11:16]} {e['title']}"
        for e in events
    ]
    return f"{label}: {len(events)} соб.\n" + "\n".join(lines)


def build_server(
    *,
    http: httpx.AsyncClient | None = None,
    token_source: TokenSource = bearer_from_request,
    now: Callable[[], datetime] = lambda: datetime.now(ZoneInfo("UTC")),
) -> MCPServer:
    server = MCPServer("google-calendar", version="0.1.0")

    async def fetch(
        token: str, calendar_id: str, start: datetime, end: datetime, limit: int
    ) -> list[dict[str, Any]]:
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(limit),
        }
        client = http or httpx.AsyncClient(timeout=10.0)
        try:
            response = await client.get(
                f"{API_BASE}/calendars/{quote(calendar_id, safe='')}/events",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as error:
            raise ToolError("Google Календарь недоступен, попробуй позже.") from error
        finally:
            if http is None:
                await client.aclose()
        if response.status_code == 401:
            raise ToolError("Доступ к Google Календарю истёк: переподключи аккаунт.")
        if response.status_code == 403:
            raise ToolError("Google не разрешил чтение календаря: нужно выдать доступ заново.")
        if response.status_code == 404:
            raise ToolError("Календарь не найден.")
        if response.status_code >= 400:
            raise ToolError(f"Google Календарь ответил ошибкой {response.status_code}.")
        items = response.json().get("items") or []
        events = [e for e in map(normalize_event, items) if e is not None]
        return sorted(events, key=_sort_key)

    def result(body: dict[str, Any], text: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(text=text)], structured_content=body)

    def zone_of(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ToolError(f"Неизвестный часовой пояс: {name}") from error

    async def today_events(
        ctx: Context[Any, Any], timezone: str = "UTC", calendar_id: str = "primary"
    ) -> CallToolResult:
        zone = zone_of(timezone)
        start, end = day_bounds(now(), zone)
        events = await fetch(token_source(ctx), calendar_id, start, end, MAX_RESULTS_CAP)
        body = {"date": start.date().isoformat(), "timezone": timezone, "events": events}
        return result(body, _summary(events, f"Сегодня, {start.date().isoformat()}"))

    async def list_events(
        ctx: Context[Any, Any],
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
        max_results: int = 25,
    ) -> CallToolResult:
        try:
            start, end = datetime.fromisoformat(time_min), datetime.fromisoformat(time_max)
        except ValueError as error:
            raise ToolError("time_min и time_max — даты RFC 3339 со смещением.") from error
        if start.tzinfo is None or end.tzinfo is None:
            raise ToolError("time_min и time_max должны содержать смещение часового пояса.")
        if not start < end <= start + MAX_RANGE:
            raise ToolError("Интервал должен быть положительным и не длиннее 31 дня.")
        limit = max(1, min(max_results, MAX_RESULTS_CAP))
        events = await fetch(token_source(ctx), calendar_id, start, end, limit)
        body = {"time_min": time_min, "time_max": time_max, "events": events}
        return result(body, _summary(events, "События"))

    server.add_tool(
        today_events,
        name="today_events",
        description="Events of the user's calendar today in the given IANA timezone (read-only).",
    )
    server.add_tool(
        list_events,
        name="list_events",
        description="Events between time_min and time_max (RFC 3339 with offset, at most 31 days).",
    )
    return server
