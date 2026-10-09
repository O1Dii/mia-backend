"""OAuth connection errors in the public envelope (they reuse the workspace handler)."""

from __future__ import annotations

from app.modules.workspace.errors import WorkspaceError


class OAuthUnsupportedError(WorkspaceError):
    """The server cannot be connected with what Mia implements (no PKCE, no registration)."""

    status_code = 422
    code = "connection_oauth_unsupported"


class OAuthFailedError(WorkspaceError):
    """Discovery, registration or the token exchange failed at the remote side."""

    status_code = 502
    code = "connection_oauth_failed"


class FlowInvalidError(WorkspaceError):
    """Unknown, expired or already used ``state`` on the OAuth callback."""

    status_code = 400
    code = "connection_flow_invalid"


class TransientOAuthError(Exception):
    """A refresh failed for a reason that may pass (network, 5xx); the connection stays."""


class OAuthProtocolError(Exception):
    """Internal: a remote response violated the protocol. Message is safe to log."""

    def __init__(self, message: str, *, unsupported: bool = False) -> None:
        super().__init__(message)
        self.unsupported = unsupported
