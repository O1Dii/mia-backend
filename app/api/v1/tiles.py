"""MCP Apps host for dashboard tiles (M31): the ``ui://`` document and a policed tool proxy.

``GET /v1/installations/{id}/tiles/{tile}`` returns the tile document (CSP and the
``window.mia`` bridge already inside) plus what the browser host needs: sizes, refresh rule and the
tools the tile may call. ``POST .../tool-calls`` runs one of the plugin's own tools through the
M13 router (audit, argument validation, consent/connection checks) on behalf of the signed-in user.

Host policy (the browser only forwards what the iframe asks for):

* a tile reaches the tools of its own installation only (``team.x.list`` or just ``list``);
* ``write_external`` tools are never run from a tile: they need an approval bound to a run
  (chat), so the call is refused with ``tool_permission_denied`` and nothing is executed;
* the installation must be the caller's and ``active``; a tile of another user is a 404.

Android (bearer) and web (cookie, same-origin mutations only) share the authenticator.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.ports.tool_router import ToolCallRequest, ToolContext, ToolRouter
from app.modules.auth.principal import RequestAuthenticator
from app.modules.plugins.builtin.bridge import CSP, render_app_html
from app.modules.plugins.registry import InstalledPlugin, PluginRegistry
from app.modules.tools.errors import router_message
from app.modules.tools.mcp_client import ConnectionRequired, McpConnector, McpFailure

logger = logging.getLogger(__name__)

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=16, max_length=128)]
_FRAME_OPEN = "<tool_result"
_FRAME_CLOSE = "</tool_result>"
MAX_BUNDLED_BYTES = 262_144
_BLOCKED_STATUS = {
    "needs_connection": "connection_required",
    "needs_consent": "tool_consent_required",
}
_HTTP_STATUS = {
    "resource_not_found": 404,
    "tool_permission_denied": 403,
    "tool_not_found": 404,
    "connection_required": 409,
    "tool_consent_required": 409,
    "tool_not_active": 409,
    "tool_unavailable": 502,
    "tile_unavailable": 502,
}
_MESSAGES = {
    "resource_not_found": "The tile was not found.",
    "tile_unavailable": "The tile document could not be loaded.",
}


class TileError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ToolCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TileDocument:
    installation_id: str
    plugin_id: str
    tile_id: str
    title: str | None
    resource_uri: str
    html: str
    sizes: list[str]
    mobile: str
    refresh: dict[str, Any] | None
    tools: list[str]

    def to_wire(self) -> dict[str, Any]:
        return {
            "installation_id": self.installation_id,
            "plugin_id": self.plugin_id,
            "tile_id": self.tile_id,
            "title": self.title,
            "resource_uri": self.resource_uri,
            "html": self.html,
            "csp": CSP,
            "sizes": self.sizes,
            "mobile": self.mobile,
            "refresh": self.refresh,
            "tools": self.tools,
        }


def _unframe(text: str) -> str:
    """Drop the ``<tool_result …>`` frame the router puts around output for the model."""
    if text.startswith(_FRAME_OPEN) and text.endswith(_FRAME_CLOSE):
        head, _, rest = text.partition("\n")
        _ = head
        return rest[: -len(_FRAME_CLOSE)].rstrip("\n")
    return text


def _bundled_text(plugin: InstalledPlugin, relative: str) -> str | None:
    root = plugin.plugin_dir
    if root is None:
        return None
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    candidate = (root / pure).resolve()
    if root.resolve() not in candidate.parents or not candidate.is_file():
        return None
    if candidate.stat().st_size > MAX_BUNDLED_BYTES:
        return None
    try:
        return candidate.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


class TileService:
    def __init__(
        self, *, registry: PluginRegistry, connector: McpConnector, router: ToolRouter
    ) -> None:
        self._registry = registry
        self._connector = connector
        self._router = router

    async def _plugin(self, user_id: str, installation_id: str) -> InstalledPlugin:
        for plugin in await self._registry.installed(user_id=user_id, workspace_id=None):
            if plugin.installation.id == installation_id:
                if plugin.installation.status != "active":
                    code = _BLOCKED_STATUS.get(plugin.installation.status, "tool_not_active")
                    raise TileError(code)
                return plugin
        raise TileError("resource_not_found")

    @staticmethod
    def _tile(plugin: InstalledPlugin, tile_id: str) -> dict[str, Any]:
        surfaces: Any = plugin.manifest.raw.get("surfaces") or {}
        for tile in surfaces.get("tiles", []):
            if isinstance(tile, dict) and tile.get("id") == tile_id:
                return tile
        raise TileError("resource_not_found")

    @staticmethod
    def allowed_tools(plugin: InstalledPlugin) -> list[str]:
        tools = plugin.manifest.tools
        return [name for name, policy in tools.items() if policy.risk != "write_external"]

    async def document(self, user_id: str, installation_id: str, tile_id: str) -> TileDocument:
        plugin = await self._plugin(user_id, installation_id)
        tile = self._tile(plugin, tile_id)
        html: str | None = None
        bundled = tile.get("bundled")
        if isinstance(bundled, str):
            html = _bundled_text(plugin, bundled)
        if html is None:
            try:
                html = await self._connector.read_resource(
                    plugin, user_id=user_id, uri=str(tile["resourceUri"])
                )
            except ConnectionRequired as error:
                raise TileError("connection_required") from error
            except McpFailure as failure:
                logger.warning(
                    "tile_resource_unavailable",
                    extra={"plugin_id": plugin.plugin_id, "kind": failure.kind},
                )
                raise TileError("tile_unavailable") from failure
        if not html.strip():
            raise TileError("tile_unavailable")
        if CSP not in html:
            html = render_app_html(html)
        refresh = tile.get("refresh")
        return TileDocument(
            installation_id=installation_id,
            plugin_id=plugin.plugin_id,
            tile_id=tile_id,
            title=tile.get("title"),
            resource_uri=str(tile["resourceUri"]),
            html=html,
            sizes=list(tile.get("sizes", [])),
            mobile=str(tile.get("mobile", "card")),
            refresh=(
                {"tool": refresh["tool"], "every_seconds": refresh["everySeconds"]}
                if isinstance(refresh, dict)
                else None
            ),
            tools=self.allowed_tools(plugin),
        )

    async def call(
        self, user_id: str, installation_id: str, tile_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        plugin = await self._plugin(user_id, installation_id)
        self._tile(plugin, tile_id)
        prefix = f"{plugin.plugin_id}."
        local = name[len(prefix) :] if name.startswith(prefix) else name
        policy = plugin.manifest.tools.get(local)
        if policy is None:
            raise TileError("tool_not_found")
        if policy.risk == "write_external":
            raise TileError("tool_permission_denied")
        context = ToolContext(
            user_id=user_id,
            run_id=f"tile-{uuid.uuid4()}",
            profile="chat",
            workspace_id=plugin.installation.workspace_id,
        )
        outcome = await self._router.call(
            context,
            ToolCallRequest(
                call_id=str(uuid.uuid4()), tool=f"{plugin.plugin_id}.{local}", arguments=arguments
            ),
        )
        failed = outcome.status != "succeeded"
        code = outcome.error_code
        if failed and code in _HTTP_STATUS and code != "tool_unavailable":
            raise TileError(code)
        text = _unframe(outcome.content)
        if failed and not text:
            text = router_message(outcome.error_code or "tool_execution_failed")
        return {
            "status": outcome.status,
            "is_error": failed,
            "content": text,
            "structured_content": outcome.structured_content,
            "error_code": outcome.error_code,
        }


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", request.headers.get("X-Request-ID", "request")))


def json_response(request: Request, content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=content, headers={"X-Request-ID": request_id(request)}
    )


def _error(request: Request, code: str) -> JSONResponse:
    message = _MESSAGES.get(code) or router_message(code)
    return json_response(
        request,
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id(request),
                "retryable": code in {"tool_unavailable", "tile_unavailable"},
            }
        },
        _HTTP_STATUS.get(code, 409),
    )


def build_tiles_router(*, service: TileService, authenticator: RequestAuthenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/installations", tags=["Plugins"])

    @router.get("/{installation_id}/tiles/{tile_id}")
    async def get_tile(installation_id: str, tile_id: str, request: Request) -> JSONResponse:
        principal = await authenticator.authenticate(request)
        try:
            uuid.UUID(installation_id)
            document = await service.document(principal.user_id, installation_id, tile_id)
        except ValueError:
            return _error(request, "resource_not_found")
        except TileError as error:
            return _error(request, error.code)
        response = json_response(request, document.to_wire())
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @router.post("/{installation_id}/tiles/{tile_id}/tool-calls")
    async def tile_tool_call(
        installation_id: str,
        tile_id: str,
        request: Request,
        body: Annotated[ToolCallBody, Body()],
        idempotency_key: IdempotencyKey,
    ) -> JSONResponse:
        principal = await authenticator.authenticate(request)
        try:
            uuid.UUID(installation_id)
            result = await service.call(
                principal.user_id, installation_id, tile_id, body.name, body.arguments
            )
        except ValueError:
            return _error(request, "resource_not_found")
        except TileError as error:
            return _error(request, error.code)
        response = json_response(request, result)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    return router


def install_tiles(
    app: Any,
    *,
    registry: PluginRegistry,
    connector: McpConnector,
    router: ToolRouter,
    authenticator: RequestAuthenticator,
) -> TileService:
    service = TileService(registry=registry, connector=connector, router=router)
    app.include_router(build_tiles_router(service=service, authenticator=authenticator))
    return service
