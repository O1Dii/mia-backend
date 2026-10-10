"""Wire the M13 tool stack: router, approvals (+ API), device channel, core tools.

Usage in ``app/composition.py`` (after ``install_auth``)::

    tools = install_tools(app, settings, runtime)
    runs = install_runs(..., tool_router=tools.router, approvals=tools.approvals)
    tools.device_channel.bind_publisher(runs.publish)

M14 supplies ``server_factory`` (in-process MCP servers) and, if needed, a
``manifest_source``; M15 supplies ``credentials`` for OAuth/Mia-token remotes.

M70 adds device access (ADR-0016): ``DeviceAccess`` (policy, grants, log) shared by the
router and the device channel, ``/v1/devices/{id}/…`` for the phone, the push notifier and
``app.state.run_origins`` (``POST /v1/runs`` records who asks).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from app.api.v1.approvals import build_approvals_router
from app.api.v1.device_access import build_device_access_router
from app.core.config import Settings
from app.core.runtime import Runtime
from app.modules.auth.composition import request_authenticator
from app.modules.notifications.device_push import build_device_push
from app.modules.plugins.registry import (
    DirectoryManifestSource,
    InstallationReader,
    ManifestSource,
    PluginRegistry,
    SqlInstallationReader,
    SqlRiskStore,
)
from app.modules.tools.approvals import SqlApprovalService
from app.modules.tools.audit import SqlToolAudit
from app.modules.tools.core_tools import CoreToolRegistry
from app.modules.tools.device import InMemoryDeviceToolChannel
from app.modules.tools.device_access import DeviceAccess
from app.modules.tools.device_origin import RunOriginResolver
from app.modules.tools.device_store import SqlDeviceAccessStore
from app.modules.tools.mcp_client import InProcessServerFactory, McpConnector, RemoteCredentials
from app.modules.tools.router import McpToolRouter, RouterConfig

BUILTIN_PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins" / "builtin"
"""``app/plugins/builtin`` (M14 packages); read as data, never imported here."""


@dataclass(frozen=True, slots=True)
class ToolStack:
    router: McpToolRouter
    approvals: SqlApprovalService
    device_channel: InMemoryDeviceToolChannel
    core_tools: CoreToolRegistry
    registry: PluginRegistry
    connector: McpConnector
    device_access: DeviceAccess | None = None


def host_allowlist(settings: Settings) -> frozenset[str] | None:
    raw = settings.tools_mcp_host_allowlist
    if raw is None or not raw.strip():
        return None
    return frozenset(host.strip().lower() for host in raw.split(",") if host.strip())


def build_tool_stack(
    settings: Settings,
    runtime: Runtime,
    *,
    server_factory: InProcessServerFactory | None = None,
    manifest_source: ManifestSource | None = None,
    credentials: RemoteCredentials | None = None,
    wrap_installations: Callable[[InstallationReader], InstallationReader] | None = None,
) -> ToolStack:
    session_factory = runtime.database.session_factory
    reader: InstallationReader = SqlInstallationReader(session_factory)
    if wrap_installations is not None:
        reader = wrap_installations(reader)
    registry = PluginRegistry(
        reader,
        manifest_source or DirectoryManifestSource(BUILTIN_PLUGINS_DIR),
    )
    connector = McpConnector(
        server_factory=server_factory,
        credentials=credentials,
        global_allowlist=host_allowlist(settings),
        list_timeout_s=settings.tools_list_timeout_seconds,
    )
    approvals = SqlApprovalService(session_factory)
    device_access = DeviceAccess(SqlDeviceAccessStore(session_factory))
    channel = InMemoryDeviceToolChannel(access=device_access)
    core_tools = CoreToolRegistry()
    router = McpToolRouter(
        registry=registry,
        connector=connector,
        audit=SqlToolAudit(session_factory),
        approvals=approvals,
        device_channel=channel,
        core_tools=core_tools,
        config=RouterConfig(
            call_timeout_s=settings.tools_call_timeout_seconds,
            list_cache_s=settings.tools_list_cache_seconds,
            approval_ttl_s=settings.tools_approval_ttl_seconds,
            max_result_chars=settings.tools_max_result_chars,
        ),
        device_access=device_access,
        risk_store=SqlRiskStore(session_factory),
    )
    return ToolStack(router, approvals, channel, core_tools, registry, connector, device_access)


def install_tools(
    app: FastAPI,
    settings: Settings,
    runtime: Runtime,
    *,
    server_factory: InProcessServerFactory | None = None,
    manifest_source: ManifestSource | None = None,
    credentials: RemoteCredentials | None = None,
    wrap_installations: Callable[[InstallationReader], InstallationReader] | None = None,
) -> ToolStack:
    """Mount ``/v1/approvals*``; the stack is also exposed as ``app.state.tools``."""
    stack = build_tool_stack(
        settings,
        runtime,
        server_factory=server_factory,
        manifest_source=manifest_source,
        credentials=credentials,
        wrap_installations=wrap_installations,
    )
    app.state.tools = stack
    authenticator = request_authenticator(app)
    access = stack.device_access
    app.include_router(
        build_approvals_router(
            service=stack.approvals, authenticator=authenticator, device_access=access
        )
    )
    if access is not None:
        access.bind_notifier(build_device_push(runtime.database.session_factory))
        app.include_router(
            build_device_access_router(
                access=access, calls=stack.device_channel, authenticator=authenticator
            )
        )
        app.state.run_origins = RunOriginResolver(access, authenticator)
    return stack
