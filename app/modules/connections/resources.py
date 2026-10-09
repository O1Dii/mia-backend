"""Plugin servers as RFC 8707 resources of Mia's authorization server (M51-R1).

Mia's AS issues tokens for its own API and ``/mcp``. A plugin server that trusts Mia as its
authorization server (``server.auth: mia_token``, or an MCP server whose protected resource
metadata names Mia) needs tokens bound to *its* URL instead. Which URLs are accepted is
derived from data Mia already vetted, so no per-plugin configuration is needed:

- the remote ``server.url`` of a release in the store (published by an administrator). The
  token may go to the plugin's own client (``server.clientId``, its CIMD URL) or to Mia's
  connection client (Mia connecting to that server on the user's behalf); a release without
  ``clientId`` only to Mia's connection client;
- the URL of an MCP server a user added by URL (``installations.custom_url``), only for Mia's
  connection client: an arbitrary third-party client can never get a token for it.

Anything else stays ``invalid_target``. ``OAUTH_EXTRA_RESOURCES`` (any client) is checked by
the AS itself before this policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.connections.oauth.client import canonical_resource
from app.modules.connections.oauth.errors import OAuthProtocolError
from app.modules.plugins.catalog.models import PluginRelease
from app.modules.workspace.models import Installation


def canonical_or_none(url: str) -> str | None:
    try:
        return canonical_resource(url)
    except (OAuthProtocolError, ValueError):
        return None


def _remote_server(manifest: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    server = manifest.get("server") if isinstance(manifest, Mapping) else None
    if isinstance(server, Mapping) and server.get("kind") == "remote":
        return server
    return None


class PluginResources:
    """:class:`app.modules.oauth.service.ResourcePolicy` over releases and installations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        connection_client_id: str | None,
    ) -> None:
        self._session_factory = session_factory
        self._connection_client_id = connection_client_id

    async def allows(self, resource: str, *, client_id: str) -> bool:
        canon = canonical_or_none(resource)
        if canon is None:
            return False
        keys = [canon.lower(), canon.lower() + "/"]
        mia = self._connection_client_id is not None and client_id == self._connection_client_id
        async with self._session_factory() as session:
            manifests = (
                await session.scalars(
                    select(PluginRelease.manifest).where(
                        PluginRelease.server_kind == "remote",
                        func.lower(PluginRelease.manifest["server"]["url"].astext).in_(keys),
                    )
                )
            ).all()
            for manifest in manifests:
                server = _remote_server(manifest)
                if server is None or canonical_or_none(str(server.get("url", ""))) != canon:
                    continue
                if mia or (server.get("clientId") is not None and server["clientId"] == client_id):
                    return True
            if not mia:
                return False
            urls = (
                await session.scalars(
                    select(Installation.custom_url)
                    .where(
                        Installation.source == "custom",
                        func.lower(Installation.custom_url).in_(keys),
                    )
                    .limit(5)
                )
            ).all()
        return any(url is not None and canonical_or_none(url) == canon for url in urls)
