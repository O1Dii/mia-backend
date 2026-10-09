"""Errors of the authorization server and the app-facing resource endpoints."""

from __future__ import annotations

from typing import Final


class TokenEndpointError(Exception):
    """RFC 6749 §5.2 error of ``/oauth/token`` (``error`` is the registered code)."""

    def __init__(self, error: str, description: str, *, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


class AuthorizeError(Exception):
    """``/oauth/authorize`` failure.

    ``redirect_uri`` is set only once the client and its redirect URI were verified; the
    error then goes back to the client (RFC 6749 §4.1.2.1). Otherwise it is shown to the
    user and never redirected (an unverified redirect URI is an open redirector).
    """

    def __init__(
        self,
        error: str,
        description: str,
        *,
        redirect_uri: str | None = None,
        state: str | None = None,
    ) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.redirect_uri = redirect_uri
        self.state = state


class AppApiError(Exception):
    """Public v1 error envelope for consent and app-management endpoints."""

    def __init__(
        self, status: int, code: str, message: str, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


# code -> (http status, retryable, public message); mirrored in contracts/mvp/v1/error-codes.json
APP_ERROR_CODES: Final[dict[str, tuple[int, bool, str]]] = {
    "insufficient_scope": (403, False, "The access token lacks a required scope."),
    "invalid_token": (401, False, "The access token is invalid, expired or revoked."),
    "app_budget_exceeded": (429, False, "The monthly spending limit for this app is reached."),
    "oauth_request_not_found": (404, False, "The authorization request was not found."),
    "oauth_request_expired": (410, False, "The authorization request has expired."),
    "oauth_request_decided": (409, False, "The authorization request was already decided."),
    "app_grant_not_found": (404, False, "The connected app was not found."),
    "unsupported_model": (400, False, "Only the model mia:default is available."),
    "unsupported_content": (400, False, "Only text content is supported."),
    "authentication_required": (401, False, "Authentication is required."),
    "validation_error": (422, False, "The request data is invalid."),
}


def app_error(code: str, *, headers: dict[str, str] | None = None) -> AppApiError:
    status, _, message = APP_ERROR_CODES[code]
    return AppApiError(status, code, message, headers=headers)
