"""Common FastAPI dependencies."""

from fastapi import Request


def get_request_id(request: Request) -> str:
    """Return the correlation identifier assigned by request middleware."""
    return str(request.state.request_id)
