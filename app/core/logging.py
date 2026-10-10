"""Safe structured logging configuration.

The formatter is an allow-list: it emits server-authored event names and typed,
content-free fields only. Log messages, arguments, exception text, tracebacks and
arbitrary ``extra`` values may contain credentials, transcripts or provider
payloads, so they are never serialized.

Event names stay readable: application code (``app.*`` loggers) logs a snake_case
literal such as ``plugin_manifest_invalid`` with no ``%`` arguments, and that name is
emitted as is. A message with arguments, any other shape, or a record of a library
logger is a ``redacted_event`` unless listed in :data:`KNOWN_EVENTS`. Only values are
redacted, never the name of what happened.
"""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.correlation import current_request_id

KNOWN_EVENTS = frozenset(
    {
        "application_started",
        "application_stopped",
        "request_completed",
        "websocket_completed",
        "readiness_failed",
        "admission_rejected",
        "provider_circuit_opened",
        "provider_circuit_recovered",
        "realtime_session_expired",
        "model_failover",
        "app_run_finished",
        "app_usage_record_failed",
        "oauth_purge_failed",
        "telegram_send_failed",
        "telegram_update_failed",
    }
)
# Server-generated IDs and UUID-shaped client IDs are content-free and logged as-is.
_OPAQUE_ID = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_ROUTE = re.compile(r"^[A-Za-z0-9_{}/:.\-]{1,128}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}(\.[a-z][a-z0-9-]{1,47}){1,2}$")
_TOOL = re.compile(r"^[a-z][a-z0-9_]{0,63}(\.[a-z][a-z0-9_-]{0,63}){0,3}$")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"})
_UUID_FIELDS = (
    "user_id",
    "conversation_id",
    "turn_id",
    "response_id",
    "session_id",
    "installation_id",
    "run_id",
)
_INT_FIELDS = (
    "status_code",
    "duration_ms",
    "close_code",
    "retry_after_seconds",
    "tool_count",
    "limit",
)
_TOKEN_FIELDS = ("environment", "ai_provider", "provider", "reason", "dependency", "kind")


def _safe_uuid(value: object) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            return None
    return None


def _event_name(record: logging.LogRecord) -> str | None:
    message = record.msg
    if not isinstance(message, str):
        return None
    if message in KNOWN_EVENTS:
        return message
    # Application events are snake_case literals without arguments; anything else (a
    # formatted sentence, a library's message) may carry content and stays redacted.
    from_app = record.name == "app" or record.name.startswith("app.")
    if from_app and not record.args and _TOKEN.fullmatch(message):
        return message
    return None


class JsonFormatter(logging.Formatter):
    """Serialize one JSON object per record using only allow-listed fields."""

    def format(self, record: logging.LogRecord) -> str:
        event = _event_name(record)
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name if record.name.startswith("app.") else "external",
            "event": event or "redacted_event",
        }
        request_id = getattr(record, "request_id", None) or current_request_id()
        if isinstance(request_id, str):
            if _OPAQUE_ID.fullmatch(request_id):
                payload["request_id"] = request_id
            else:
                # Client-chosen free-form IDs could carry anything; log a stable digest.
                digest = hashlib.sha256(request_id.encode()).hexdigest()[:16]
                payload["request_id_hash"] = digest
        method = getattr(record, "method", None)
        if isinstance(method, str) and method in _METHODS:
            payload["method"] = method
        route = getattr(record, "route", None)
        if isinstance(route, str) and _ROUTE.fullmatch(route):
            payload["route"] = route
        for key in _INT_FIELDS:
            value = getattr(record, key, None)
            if type(value) is int:
                payload[key] = value
        for key in _TOKEN_FIELDS:
            value = getattr(record, key, None)
            if isinstance(value, str) and _TOKEN.fullmatch(value):
                payload[key] = value
        for key in _UUID_FIELDS:
            safe = _safe_uuid(getattr(record, key, None))
            if safe is not None:
                payload[key] = safe
        plugin_id = getattr(record, "plugin_id", None)
        if isinstance(plugin_id, str) and _PLUGIN_ID.fullmatch(plugin_id):
            payload["plugin_id"] = plugin_id
        tool = getattr(record, "tool", None)
        if isinstance(tool, str) and _TOOL.fullmatch(tool):
            payload["tool"] = tool
        configured = getattr(record, "gemini_api_key_configured", None)
        if type(configured) is bool:
            payload["gemini_api_key_configured"] = configured
        if record.exc_info and record.exc_info[0] is not None:
            # The exception class is server code; its message and traceback are not logged.
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload)


def configure_logging(level: str) -> None:
    """Route application and server loggers through the redacting JSON formatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi", "alembic"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    # A library configuration (for example Alembic's fileConfig) can silence existing
    # loggers; application and server logs must always reach the JSON handler.
    for name in (*logging.root.manager.loggerDict, "uvicorn.error"):
        if name.startswith(("app", "uvicorn")):
            logging.getLogger(name).disabled = False
    # Access lines contain raw paths and query strings; request_completed replaces them.
    logging.getLogger("uvicorn.access").disabled = True
