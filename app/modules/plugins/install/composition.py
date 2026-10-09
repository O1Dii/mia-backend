"""Wiring of the store (M15): catalog, installs and account connections.

Order in ``app/composition.py`` (each step needs the previous one)::

    catalog = build_catalog_stack(settings, runtime, builtin_plugins)
    workspace = install_workspace(app, database=..., catalog=catalog.catalog)
    connections = build_connections(
        settings, runtime, vault=models_stack.vault, workspace=workspace
    )
    tools = install_tools(
        ..., manifest_source=catalog.manifest_source, credentials=connections
    )
    install_store(
        app, settings, runtime, catalog=catalog, connections=connections, tools=tools,
        workspace=workspace,
    )
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from app.api.v1.plugins import build_plugins_router
from app.core.config import Settings
from app.core.runtime import Runtime
from app.modules.auth.composition import request_authenticator
from app.modules.connections.oauth.client import OAuthClient
from app.modules.connections.oauth.service import ConnectionService
from app.modules.models.vault import KeyVault
from app.modules.plugins.catalog.discovery import token_connector
from app.modules.plugins.catalog.service import CatalogService
from app.modules.plugins.catalog.sources import (
    CompositeCatalog,
    CompositeManifestSource,
    ReleaseManifestSource,
)
from app.modules.plugins.composition import BuiltinPlugins
from app.modules.plugins.install.service import InstallService
from app.modules.plugins.registry import DirectoryManifestSource
from app.modules.tools.composition import BUILTIN_PLUGINS_DIR, ToolStack, host_allowlist
from app.modules.workspace.service import WorkspaceService


def admin_user_ids(settings: Settings) -> frozenset[str]:
    """``STORE_ADMIN_USER_IDS``: canonical UUIDs; malformed entries are ignored."""
    ids: set[str] = set()
    for raw in (settings.store_admin_user_ids or "").split(","):
        try:
            ids.add(str(uuid.UUID(raw.strip())))
        except ValueError:
            continue
    return frozenset(ids)


@dataclass(frozen=True, slots=True)
class CatalogStack:
    service: CatalogService
    manifest_source: CompositeManifestSource
    catalog: CompositeCatalog


def build_catalog_stack(
    settings: Settings, runtime: Runtime, builtin: BuiltinPlugins
) -> CatalogStack:
    """Releases first, builtin packages of the image as the fallback."""
    allowlist = host_allowlist(settings)
    service = CatalogService(
        runtime.database.session_factory,
        server_factory=builtin.server_factory,
        admin_user_ids=admin_user_ids(settings),
        connector_factory=lambda token: token_connector(
            token,
            server_factory=builtin.server_factory,
            global_allowlist=allowlist,
            list_timeout_s=settings.tools_list_timeout_seconds,
        ),
    )
    releases = ReleaseManifestSource(
        service, Path(settings.store_packages_dir) if settings.store_packages_dir else None
    )
    return CatalogStack(
        service=service,
        manifest_source=CompositeManifestSource(
            DirectoryManifestSource(BUILTIN_PLUGINS_DIR), releases
        ),
        catalog=CompositeCatalog(service, builtin.catalog),
    )


def build_oauth_client(settings: Settings) -> OAuthClient:
    return OAuthClient(
        timeout_s=settings.connections_oauth_timeout_seconds,
        global_allowlist=host_allowlist(settings),
    )


def build_connections(
    settings: Settings,
    runtime: Runtime,
    *,
    vault: KeyVault,
    workspace: WorkspaceService,
    oauth: OAuthClient | None = None,
) -> ConnectionService:
    """The ``RemoteCredentials`` of the router and the connection API; revokes on uninstall."""
    service = ConnectionService(
        runtime.database.session_factory,
        vault,
        oauth or build_oauth_client(settings),
        workspace,
        public_base_url=settings.public_base_url,
        flow_ttl_s=settings.connections_oauth_flow_ttl_seconds,
    )

    workspace.uninstall_hooks.append(service.revoke_installation)
    return service


@dataclass(frozen=True, slots=True)
class StoreStack:
    catalog: CatalogService
    installs: InstallService
    connections: ConnectionService


def install_store(
    app: FastAPI,
    settings: Settings,
    runtime: Runtime,
    *,
    catalog: CatalogStack,
    connections: ConnectionService,
    tools: ToolStack,
    workspace: WorkspaceService,
) -> StoreStack:
    """Mount the store routes; ``app.state.store`` exposes the stack to other modules."""
    connections.on_change = tools.router.invalidate
    installs = InstallService(
        runtime.database.session_factory,
        workspace,
        catalog.service,
        connections,
        tools.connector,
        connections.oauth,
        invalidate=tools.router.invalidate,
    )
    app.include_router(
        build_plugins_router(
            catalog=catalog.service,
            installs=installs,
            connections=connections,
            workspace=workspace,
            authenticator=request_authenticator(app),
        )
    )
    stack = StoreStack(catalog.service, installs, connections)
    app.state.store = stack
    return stack
