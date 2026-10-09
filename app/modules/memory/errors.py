"""Memory errors and the public error envelope (``contracts/errors/v1``)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

MEMORY_CODES: dict[str, tuple[int | None, bool, str]] = {
    # code: (http_status or None for tool/event-only, retryable, public message)
    "memory_incognito": (None, False, "Incognito runs do not write memory."),
    "memory_unavailable": (None, True, "Memory is temporarily unavailable."),
    "memory_import_invalid": (422, False, "The import could not be read."),
    "memory_import_too_large": (413, False, "The import is too large."),
}
"""Codes registered by M17 in ``contracts/mvp/v1/error-codes.json``."""


class MemoryLedgerError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class RecordNotFoundError(MemoryLedgerError):
    """Missing or owned by another user: both look identical to the caller."""

    status_code = 404
    code = "resource_not_found"

    def __init__(self) -> None:
        super().__init__("The record was not found.")


class IdempotencyConflictError(MemoryLedgerError):
    status_code = 409
    code = "idempotency_conflict"

    def __init__(self) -> None:
        super().__init__("The Idempotency-Key was used with a different request.")


class InvalidMemoryRequestError(MemoryLedgerError):
    status_code = 422
    code = "validation_error"


class ImportInvalidError(MemoryLedgerError):
    status_code = 422
    code = "memory_import_invalid"

    def __init__(self, message: str = "The import could not be read.") -> None:
        super().__init__(message)


class ImportTooLargeError(MemoryLedgerError):
    status_code = 413
    code = "memory_import_too_large"

    def __init__(self) -> None:
        super().__init__("The import is too large.")


class MemoryUnavailableError(MemoryLedgerError):
    status_code = 503
    code = "service_unavailable"

    def __init__(self) -> None:
        super().__init__("Memory is temporarily unavailable.")


async def memory_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, MemoryLedgerError):
        raise exc
    error: dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
        "request_id": str(getattr(request.state, "request_id", "request")),
        "retryable": exc.status_code == 503,
    }
    if exc.details:
        error["details"] = exc.details
    return JSONResponse(status_code=exc.status_code, content={"error": error})
