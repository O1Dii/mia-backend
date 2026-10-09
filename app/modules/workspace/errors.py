"""Workspace domain errors and their public error envelope."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class WorkspaceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ResourceNotFoundError(WorkspaceError):
    """Missing or owned by another workspace: both look identical to the caller."""

    status_code = 404
    code = "resource_not_found"


class VersionConflictError(WorkspaceError):
    """``expected_version`` is stale; ``details.current_version`` has the fresh one."""

    status_code = 409
    code = "version_conflict"

    def __init__(self, current_version: int) -> None:
        super().__init__(
            "The resource was changed by another request.",
            details={"current_version": current_version},
        )
        self.current_version = current_version


class ConflictError(WorkspaceError):
    status_code = 409
    code = "conflict"


class ToolsDigestMismatchError(WorkspaceError):
    """The consented tool descriptions differ from the catalog version."""

    status_code = 409
    code = "tools_digest_mismatch"


class IdempotencyConflictError(WorkspaceError):
    status_code = 409
    code = "idempotency_conflict"


class InvalidWorkspaceRequestError(WorkspaceError):
    status_code = 422
    code = "validation_error"


class WorkspaceUnavailableError(WorkspaceError):
    status_code = 503
    code = "service_unavailable"


async def workspace_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, WorkspaceError):
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
