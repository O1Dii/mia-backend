"""Public error helpers that preserve the v1 error envelope."""

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Return validation failures without echoing credential-bearing request input."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "The request data is invalid.",
                "request_id": str(request.state.request_id),
                "retryable": False,
            }
        },
    )


def service_unavailable(request_id: str, details: dict[str, Any] | None = None) -> JSONResponse:
    """Return the stable retryable error used when a required dependency is down."""
    error: dict[str, Any] = {
        "code": "service_unavailable",
        "message": "Required dependencies are unavailable.",
        "request_id": request_id,
        "retryable": True,
    }
    if details:
        error["details"] = details
    return JSONResponse(status_code=503, content={"error": error})


async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    """Return the stable envelope for unexpected failures without exception details."""
    request_id = str(getattr(request.state, "request_id", "request"))
    return JSONResponse(
        status_code=500,
        headers={"X-Request-ID": request_id},
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred.",
                "request_id": request_id,
                "retryable": False,
            }
        },
    )
