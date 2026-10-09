"""Resource-server side: verify an app access token for one endpoint (RFC 6750, RFC 8707).

App tokens are opaque ``mia_at_…`` strings; first-party authenticators reject them, so an
app can never reach first-party APIs (approvals, memory management, installations).
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import Request

from app.modules.oauth.errors import AppApiError, app_error
from app.modules.oauth.service import AppAccess, AuthorizationService


class AppAuthenticator:
    def __init__(self, service: AuthorizationService) -> None:
        self._service = service
        base = service.config.issuer.rstrip("/")
        self.api_resource = base
        self.mcp_resource = service.config.mcp_resource
        self._api_metadata = base + "/.well-known/oauth-protected-resource"
        self._mcp_metadata = base + "/.well-known/oauth-protected-resource/mcp"

    def challenge(self, *, mcp: bool, error: str | None = None, scope: str | None = None) -> str:
        metadata = self._mcp_metadata if mcp else self._api_metadata
        parts = [f'resource_metadata="{metadata}"']
        if error:
            parts.append(f'error="{error}"')
        if scope:
            parts.append(f'scope="{scope}"')
        return "Bearer " + ", ".join(parts)

    async def authenticate(
        self, request: Request, *, required: Iterable[str] = (), mcp: bool = False
    ) -> AppAccess:
        """Return the verified access; else 401 ``invalid_token`` / 403 ``insufficient_scope``."""
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AppApiError(
                401,
                "authentication_required",
                "Authentication is required.",
                headers={"WWW-Authenticate": self.challenge(mcp=mcp)},
            )
        access = await self._service.authenticate(token.strip())
        expected = {self.mcp_resource} if mcp else {self.api_resource, self.api_resource + "/"}
        if access is None or (access.resource is not None and access.resource not in expected):
            raise app_error(
                "invalid_token",
                headers={"WWW-Authenticate": self.challenge(mcp=mcp, error="invalid_token")},
            )
        missing = [scope for scope in required if scope not in access.scopes]
        if missing:
            raise app_error(
                "insufficient_scope",
                headers={
                    "WWW-Authenticate": self.challenge(
                        mcp=mcp, error="insufficient_scope", scope=" ".join(missing)
                    )
                },
            )
        return access
