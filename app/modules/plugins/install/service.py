"""Install flows on top of M10's installations: consented install, updates with re-consent,
and «add MCP by URL» (probe, connect, consent).

Consent is the pair ``tools_digest`` + ``grants`` the user saw on the consent screen:

- a catalog install must echo the release digest and every grant it requires;
- an update is refused (``consent_required``) while the new release needs grants the
  installation does not hold; the digest echo covers changed tool descriptions;
- a custom MCP server is probed first, every tool is ``write_external`` (each call asks for
  approval) and the manifest overlay is generated here from the tools the user ticked (at
  most ``tool_limit``: :data:`MAX_PLUGIN_TOOLS` and what the user's other plugins leave of
  :data:`PLUGIN_TOOL_BUDGET`; M15-R1).

``health`` tells the store whether an installation's tools reach the model right now, so an
installation that cannot be offered is shown with its reason instead of vanishing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.connections.oauth.client import OAuthClient
from app.modules.connections.oauth.errors import (
    OAuthFailedError,
    OAuthProtocolError,
    TransientOAuthError,
)
from app.modules.connections.oauth.models import PluginConnection
from app.modules.connections.oauth.service import ConnectionService, remote_target
from app.modules.plugins.catalog.errors import ConsentRequiredError
from app.modules.plugins.catalog.schemas import (
    ConsentCustomRequest,
    ConsentGrant,
    DiscoveredTool,
    DiscoverResult,
    InstallationHealth,
    InstallRequest,
    UpdateInfo,
    UpgradeRequest,
)
from app.modules.plugins.catalog.service import CatalogService, consent_of
from app.modules.plugins.catalog.versions import version_key
from app.modules.plugins.install.grants import describe_grant, diff_grants, required_grants
from app.modules.plugins.registry import (
    MAX_PLUGIN_TOOLS,
    InstallationRecord,
    InstalledPlugin,
    PluginRegistry,
    parse_manifest,
)
from app.modules.plugins.registry.limits import default_selection, keep_within
from app.modules.plugins.registry.manifest import TOOL_NAME
from app.modules.tools.digest import ServerTool, compute_tools_digest
from app.modules.tools.mcp_client import ConnectionRequired, McpConnector, McpFailure
from app.modules.workspace.errors import (
    ConflictError,
    InvalidWorkspaceRequestError,
    ResourceNotFoundError,
    ToolsDigestMismatchError,
    VersionConflictError,
)
from app.modules.workspace.models import Installation
from app.modules.workspace.schemas import CatalogInstallRequest
from app.modules.workspace.service import (
    MutationResult,
    WorkspaceContext,
    WorkspaceService,
    installation_view,
)

logger = logging.getLogger(__name__)

CUSTOM_VERSION = "0.0.0"


def _dump(model: Any) -> dict[str, Any]:
    return dict(model.model_dump(mode="json"))


class InstallService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        workspace: WorkspaceService,
        catalog: CatalogService,
        connections: ConnectionService,
        connector: McpConnector,
        oauth: OAuthClient,
        *,
        registry: PluginRegistry,
        invalidate: Callable[[str], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._workspace = workspace
        self._catalog = catalog
        self._connections = connections
        self._connector = connector
        self._oauth = oauth
        self._invalidate = invalidate

    # ------------------------------------------------------------ catalog install

    async def install(
        self,
        ctx: WorkspaceContext,
        plugin_id: str,
        request: InstallRequest,
        *,
        idempotency_key: str,
    ) -> MutationResult:
        release = await self._catalog.release(plugin_id, request.version)
        if release.tools_digest != request.tools_digest:
            raise ToolsDigestMismatchError("Tool descriptions changed; review and consent again.")
        missing = sorted(set(release.grants) - set(request.grants))
        if missing:
            raise ConsentRequiredError(
                "The install needs consent to every permission of the plugin.",
                details={"missing_grants": missing},
            )
        return await self._workspace.create_installation(
            ctx,
            CatalogInstallRequest(
                plugin_id=plugin_id, version=request.version, tools_digest=request.tools_digest
            ),
            idempotency_key=idempotency_key,
        )

    # --------------------------------------------------------------- updates

    async def update_info(self, ctx: WorkspaceContext, installation_id: uuid.UUID) -> UpdateInfo:
        installation = await self._workspace.get_installation(ctx, installation_id)
        installed = installation.version
        base = {
            "installation_id": installation_id,
            "plugin_id": installation.plugin_id,
            "installed_version": installed,
        }
        try:
            latest = await self._catalog.release(installation.plugin_id)
        except ResourceNotFoundError:
            return UpdateInfo(
                **base,
                latest_version=installed,
                update_available=False,
                added_grants=[],
                removed_grants=[],
                digest_changed=False,
                consent=None,
            )
        newer = version_key(latest.version) > version_key(installed)
        diff = diff_grants(installation.grants, latest.grants)
        return UpdateInfo(
            **base,
            latest_version=latest.version,
            update_available=newer,
            added_grants=list(diff.added) if newer else [],
            removed_grants=list(diff.removed) if newer else [],
            digest_changed=newer and latest.tools_digest != installation.tools_digest,
            consent=consent_of(latest) if newer else None,
        )

    async def upgrade(
        self,
        ctx: WorkspaceContext,
        installation_id: uuid.UUID,
        request: UpgradeRequest,
        *,
        idempotency_key: str,
    ) -> MutationResult:
        installation = await self._workspace.get_installation(ctx, installation_id)
        if installation.source != "catalog":
            raise ConflictError("Only catalog installations can be updated.")
        target = await self._catalog.release(installation.plugin_id, request.version)
        if version_key(target.version) <= version_key(installation.version):
            raise ConflictError("The requested version is not newer than the installed one.")
        if target.tools_digest != request.tools_digest:
            raise ToolsDigestMismatchError("Tool descriptions changed; review and consent again.")
        diff = diff_grants(installation.grants, target.grants)
        uncovered = sorted(set(diff.added) - set(request.grants))
        if uncovered:
            raise ConsentRequiredError(
                "The new version asks for permissions you have not consented to.",
                details={
                    "added_grants": list(diff.added),
                    "removed_grants": list(diff.removed),
                    "grants": [
                        {"id": grant, "text": describe_grant(grant)} for grant in diff.added
                    ],
                    "version": target.version,
                    "tools_digest": target.tools_digest,
                },
            )
        server_moved = await self._server_moved(ctx, installation_id, target.manifest)

        async def work(session: AsyncSession) -> MutationResult:
            row = await session.scalar(
                update(Installation)
                .where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                    Installation.state_version == request.expected_version,
                )
                .values(
                    version=target.version,
                    tools_digest=target.tools_digest,
                    grants=sorted(target.grants),
                    manifest=dict(target.manifest),
                    status=_status_after_upgrade(installation.status, server_moved),
                    state_version=Installation.state_version + 1,
                    updated_at=func.now(),
                )
                .returning(Installation)
            )
            if row is None:
                current = await session.scalar(
                    select(Installation.state_version).where(
                        Installation.id == installation_id,
                        Installation.workspace_id == ctx.workspace_uuid,
                    )
                )
                if current is None:
                    raise ResourceNotFoundError("The installation was not found.")
                raise VersionConflictError(current)
            return MutationResult(200, _dump(installation_view(row)))

        result = await self._workspace.mutate(
            ctx,
            idempotency_key,
            f"upgrade:{installation_id}:{request.model_dump_json()}",
            work,
        )
        if server_moved:
            # The remote endpoint changed: the old tokens belong to another resource. Revoke
            # only after the update went through (a stale expected_version must not cost
            # the user their connection).
            await self._connections.revoke_installation(ctx, installation_id)
        if self._invalidate is not None:
            self._invalidate(installation.plugin_id)
        return result

    async def _server_moved(
        self, ctx: WorkspaceContext, installation_id: uuid.UUID, manifest: Any
    ) -> bool:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(Installation).where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                )
            )
            if row is None:
                return False
            old = remote_target(row)
        if old is None:
            return False
        server = manifest.get("server") if isinstance(manifest, dict) else None
        return not isinstance(server, dict) or server.get("url") != old.url

    # ------------------------------------------------------------ custom MCP URL

    async def discover_custom(
        self, ctx: WorkspaceContext, installation_id: uuid.UUID
    ) -> DiscoverResult:
        installation = await self._custom_installation(ctx, installation_id)
        url = installation.custom_url or ""
        host = _host(url)
        target = remote_target(installation)
        assert target is not None
        try:
            needs_auth = await self._oauth.requires_auth(url, target.network)
        except (OAuthProtocolError, TransientOAuthError) as error:
            raise OAuthFailedError("The MCP server is unreachable.") from error
        connected = needs_auth and await self._has_connection(installation_id)
        if needs_auth and not connected:
            return DiscoverResult(
                installation_id=installation_id,
                state="needs_connection",
                tools=[],
                tools_digest=None,
                grants=[],
                host=host,
            )
        tools = await self._list_custom_tools(ctx, installation, needs_auth)
        manifest = custom_manifest(installation.plugin_id, url, needs_auth, tools)
        limit = await self._tool_limit(ctx, installation_id)
        usable = _usable(tools)
        enabled = _enabled_tools(installation)
        current = [name for name in usable if name in enabled] if enabled else []
        selected = set(keep_within(current, limit) if current else default_selection(usable, limit))
        return DiscoverResult(
            installation_id=installation_id,
            state="ready",
            tools=[
                DiscoveredTool(name=t.name, description=t.description, selected=t.name in selected)
                for t in tools
            ],
            tools_digest=compute_tools_digest(tools),
            grants=[
                ConsentGrant(id=grant, text=describe_grant(grant))
                for grant in sorted(required_grants(manifest))
            ],
            host=host,
            skipped_tools=sorted(t.name for t in tools if not TOOL_NAME.match(t.name)),
            tool_limit=limit,
        )

    async def consent_custom(
        self,
        ctx: WorkspaceContext,
        installation_id: uuid.UUID,
        request: ConsentCustomRequest,
        *,
        idempotency_key: str,
    ) -> MutationResult:
        installation = await self._custom_installation(ctx, installation_id)
        url = installation.custom_url or ""
        target = remote_target(installation)
        assert target is not None
        try:
            needs_auth = await self._oauth.requires_auth(url, target.network)
        except (OAuthProtocolError, TransientOAuthError) as error:
            raise OAuthFailedError("The MCP server is unreachable.") from error
        connected = needs_auth and await self._has_connection(installation_id)
        if needs_auth and not connected:
            raise ConflictError("Connect the account before consenting to this server.")
        # Never trust the client's digest: list the tools again and compare.
        tools = await self._list_custom_tools(ctx, installation, needs_auth)
        digest = compute_tools_digest(tools)
        if digest != request.tools_digest:
            raise ToolsDigestMismatchError("The server's tools changed; review and consent again.")
        usable = _usable(tools)
        if not usable:
            raise ConflictError("The server offers no tool Mia can use.")
        chosen = _chosen_tools(usable, request.tools, await self._tool_limit(ctx, installation_id))
        manifest = custom_manifest(installation.plugin_id, url, needs_auth, tools, selected=chosen)
        required = required_grants(manifest)
        missing = sorted(required - set(request.grants))
        if missing:
            raise ConsentRequiredError(
                "Consent to every permission of the server.", details={"missing_grants": missing}
            )

        async def work(session: AsyncSession) -> MutationResult:
            row = await session.scalar(
                update(Installation)
                .where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                    Installation.state_version == request.expected_version,
                    Installation.source == "custom",
                )
                .values(
                    manifest=manifest,
                    tools_digest=digest,
                    grants=sorted(required),
                    status="active",
                    state_version=Installation.state_version + 1,
                    updated_at=func.now(),
                )
                .returning(Installation)
            )
            if row is None:
                current = await self._workspace.get_installation(ctx, installation_id)
                raise VersionConflictError(current.state_version)
            return MutationResult(200, _dump(installation_view(row)))

        result = await self._workspace.mutate(
            ctx,
            idempotency_key,
            f"consent-custom:{installation_id}:{request.model_dump_json()}",
            work,
        )
        if self._invalidate is not None:
            self._invalidate(installation.plugin_id)
        return result

    async def _tool_limit(self, ctx: WorkspaceContext, installation_id: uuid.UUID) -> int:
        return await self._registry.tool_limit(
            user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            installation_id=str(installation_id),
        )

    # ------------------------------------------------------------------ health

    async def health(self, ctx: WorkspaceContext, installation_id: uuid.UUID) -> InstallationHealth:
        """Whether the installation's tools reach the model now, and why not."""
        installation = await self._workspace.get_installation(ctx, installation_id)
        plugin = next(
            (
                item
                for item in await self._registry.installed(
                    user_id=ctx.user_id, workspace_id=ctx.workspace_id
                )
                if item.installation.id == str(installation_id)
            ),
            None,
        )
        limit = await self._tool_limit(ctx, installation_id)
        declared = 0
        if plugin is not None:
            declared = plugin.declared_tools or len(plugin.manifest.tools)

        def view(
            state: Any, tools: int = 0, problem: Any = None, reason: str | None = None
        ) -> InstallationHealth:
            return InstallationHealth(
                installation_id=installation_id,
                plugin_id=installation.plugin_id,
                state=state,
                problem=problem,
                reason=reason,
                tools=tools,
                declared_tools=declared,
                tool_limit=limit,
            )

        if plugin is not None and plugin.problem is not None and not plugin.active:
            return view("needs_attention", problem=plugin.problem, reason=plugin.reason)
        if installation.status != "active" or plugin is None:
            status = installation.status
            known = status in ("needs_connection", "needs_consent", "disabled")
            return view(status if known else "needs_attention")
        try:
            served = await self._connector.list_tools(plugin, user_id=ctx.user_id)
        except ConnectionRequired:
            return view("needs_connection")
        except McpFailure as failure:
            logger.warning(
                "plugin_health_unavailable",
                extra={"plugin_id": installation.plugin_id, "kind": failure.kind},
            )
            return view("needs_attention", problem="server_unavailable", reason=failure.kind)
        if compute_tools_digest(served) != installation.tools_digest:
            return view("needs_attention", problem="tools_changed")
        names = {tool.name for tool in served}
        offered = sum(1 for name in plugin.manifest.tools if name in names)
        if plugin.problem == "tool_budget":
            return view("limited", offered, problem="tool_budget")
        return view("ok", offered)

    async def _custom_installation(
        self, ctx: WorkspaceContext, installation_id: uuid.UUID
    ) -> Installation:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(Installation).where(
                    Installation.id == installation_id,
                    Installation.workspace_id == ctx.workspace_uuid,
                )
            )
            if row is None:
                raise ResourceNotFoundError("The installation was not found.")
            if row.source != "custom" or not row.custom_url:
                raise ConflictError("Only an MCP server added by URL can be consented this way.")
            session.expunge(row)
            return row

    async def _has_connection(self, installation_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            status = await session.scalar(
                select(PluginConnection.status).where(
                    PluginConnection.installation_id == installation_id
                )
            )
            return status == "connected"

    async def _list_custom_tools(
        self, ctx: WorkspaceContext, installation: Installation, needs_auth: bool
    ) -> list[ServerTool]:
        url = installation.custom_url or ""
        probe_manifest = custom_manifest(installation.plugin_id, url, needs_auth, [])
        plugin = InstalledPlugin(
            installation=InstallationRecord(
                id=str(installation.id),
                workspace_id=ctx.workspace_id,
                plugin_id=installation.plugin_id,
                version=installation.version,
                status="active",
                grants=(),
                tools_digest=None,
                manifest=probe_manifest,
            ),
            manifest=parse_manifest(probe_manifest),
            plugin_dir=None,
        )
        try:
            return await self._connector.list_tools(plugin, user_id=ctx.user_id)
        except ConnectionRequired as error:
            raise ConflictError("The server rejected the connection; connect again.") from error
        except McpFailure as failure:
            logger.warning("custom_mcp_list_failed", extra={"kind": failure.kind})
            raise OAuthFailedError(
                "The MCP server could not be reached or refused Mia."
            ) from failure


def custom_manifest(
    plugin_id: str,
    url: str,
    needs_auth: bool,
    tools: list[ServerTool],
    *,
    selected: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The generated ``mia.json`` of a custom server: every enabled tool is ``write_external``.

    Tool names the manifest grammar cannot hold (``list-issues``, ``createIssue``) are left
    out of the overlay, so the router does not offer them (reported as ``skipped_tools``).
    ``selected`` limits the overlay to the tools the user ticked (``None``: every usable one).
    """
    host = _host(url)
    chosen = None if selected is None else set(selected)
    overlay = {
        tool.name: {"risk": "write_external"}
        for tool in tools
        if TOOL_NAME.match(tool.name) and (chosen is None or tool.name in chosen)
    }
    return {
        "schemaVersion": "mia.plugin/1",
        "id": plugin_id,
        "version": CUSTOM_VERSION,
        "name": host[:60],
        "listing": {"description": f"MCP server {host} added by URL", "category": "utilities"},
        "server": {"kind": "remote", "url": url, "auth": "oauth" if needs_auth else "none"},
        "tools": overlay or {"placeholder": {"risk": "write_external"}},
        "permissions": {
            "network": [host],
            "llm": {"viaHost": False},
            "memory": [],
            "notify": False,
        },
    }


def _usable(tools: list[ServerTool]) -> list[str]:
    """Tool names the manifest can hold, in the server's order, without duplicates."""
    names: list[str] = []
    for tool in tools:
        if TOOL_NAME.match(tool.name) and tool.name not in names:
            names.append(tool.name)
    return names


def _enabled_tools(installation: Installation) -> set[str]:
    """Tools of a consented custom installation (its stored overlay), if any."""
    manifest = installation.manifest
    tools = manifest.get("tools") if isinstance(manifest, Mapping) else None
    if installation.tools_digest is None or not isinstance(tools, Mapping):
        return set()
    return {name for name in tools if isinstance(name, str)}


def _chosen_tools(usable: list[str], requested: list[str] | None, limit: int) -> list[str]:
    if requested is None:
        chosen = usable
    else:
        unknown = sorted(set(requested) - set(usable))
        if unknown:
            raise InvalidWorkspaceRequestError(
                "Some chosen tools are not offered by the server.",
                details={"unknown_tools": unknown[:20]},
            )
        wanted = set(requested)
        chosen = [name for name in usable if name in wanted]
    if not chosen:
        raise InvalidWorkspaceRequestError("Choose at least one tool.")
    if len(chosen) > limit:
        raise InvalidWorkspaceRequestError(
            f"Choose at most {limit} tools of this server.",
            details={"tool_limit": limit, "selected": len(chosen), "max_tools": MAX_PLUGIN_TOOLS},
        )
    return chosen


def _status_after_upgrade(current: str, server_moved: bool) -> str:
    if server_moved and current == "active":
        return "needs_connection"
    return current


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
