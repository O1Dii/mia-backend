"""Public error helpers that preserve the v1 error envelope."""

from typing import Any

from fastapi.responses import JSONResponse


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
