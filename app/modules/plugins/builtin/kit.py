"""Shared scaffolding of in-process MCP servers (builtin and declarative plugins)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from app.core.ports.screen_action import ACTION_META_KEY, parse_action
from app.modules.plugins.builtin.bridge import render_app_html
from app.modules.plugins.builtin.identity import CallIdentity, identity_from_meta
from app.modules.plugins.builtin.services import HostServices
from app.modules.plugins.builtin.state import read_state, update_state

T = TypeVar("T")
_F = TypeVar("_F", bound=Callable[..., Any])


def ok(
    summary: str, data: Mapping[str, Any], *, action: Mapping[str, Any] | None = None
) -> CallToolResult:
    """Text for the model (``content``) plus data for the UI (``structuredContent``).

    ``action`` (from :func:`screen_action`) goes to ``_meta["mia/action"]``: a button on
    the user's screen that the model never sees (M41-R1).
    """
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=dict(data),
        meta={ACTION_META_KEY: dict(action)} if action is not None else None,
    )


def screen_action(url: str, label: str, *, expires_at: str | None = None) -> dict[str, Any]:
    """A screen-only ``open_url`` action (``common.schema.json#/$defs/screenAction``).

    Use it for links the user must open but the model must not repeat, such as one-time
    bearer links. Raises ``ValueError`` for a URL that is not ``https``/``tg`` or a bad label.
    """
    raw: dict[str, Any] = {"kind": "open_url", "url": url, "label": label}
    if expires_at is not None:
        raw["expires_at"] = expires_at
    action = parse_action(raw)
    if action is None:
        raise ValueError("invalid screen action")
    return action


def ui_meta(resource_uri: str, *, app_only: bool = False) -> dict[str, Any]:
    """Tool ``_meta`` binding a tool to a ``ui://`` resource (MCP Apps + legacy flat key)."""
    ui: dict[str, Any] = {"resourceUri": resource_uri}
    if app_only:
        ui["visibility"] = ["app"]
    return {"ui": ui, "ui/resourceUri": resource_uri}


def load_ui(plugin_dir: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Read ``ui/<name>.html`` for each name."""
    return {
        name: (plugin_dir / "ui" / f"{name}.html").read_text(encoding="utf-8") for name in names
    }


class PluginKit:
    """An MCP server for one manifest with MCP Apps resources and policy-aware tools."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        services: HostServices,
        *,
        ui: Mapping[str, str] | None = None,
        instructions: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.services = services
        self.plugin_id: str = manifest["id"]
        self._policies: Mapping[str, Mapping[str, Any]] = manifest.get("tools", {})
        self._registered: list[str] = []
        apps = Apps()
        self._uris: dict[str, str] = {}
        for name, html in (ui or {}).items():
            uri = f"ui://{self.plugin_id}/{name}"
            self._uris[name] = uri
            apps.add_html_resource(uri, render_app_html(html), name=uri, title=name)
        self.server = MCPServer(
            self.plugin_id,
            version=str(manifest["version"]),
            instructions=instructions,
            extensions=[apps],
        )

    @property
    def registered_tools(self) -> list[str]:
        return list(self._registered)

    def tool(
        self,
        name: str,
        *,
        description: str,
        ui: str | None = None,
        app_only: bool = False,
        open_world: bool = False,
    ) -> Callable[[_F], _F]:
        """Register ``fn`` as tool ``name``; the manifest must declare it (unless app-only)."""
        policy = self._policies.get(name)
        if policy is None and not app_only:
            raise ValueError(f"tool {name!r} is not declared in the manifest of {self.plugin_id}")
        read_only = (policy or {}).get("risk", "read") == "read"
        meta = ui_meta(self._uris[ui], app_only=app_only) if ui is not None else None
        if ui is None and app_only:
            raise ValueError("an app-only tool needs a ui resource")

        def decorator(fn: _F) -> _F:
            self.server.add_tool(
                fn,
                name=name,
                description=description,
                annotations=ToolAnnotations(
                    read_only_hint=read_only, destructive_hint=False, open_world_hint=open_world
                ),
                meta=meta,
            )
            if not app_only:
                self._registered.append(name)
            return fn

        return decorator

    def verify_tools(self) -> None:
        """The server's model-visible tools must equal the manifest's ``tools``."""
        declared, actual = set(self._policies), set(self._registered)
        if declared != actual:
            raise ValueError(
                f"{self.plugin_id}: manifest tools {sorted(declared)} != server {sorted(actual)}"
            )

    @staticmethod
    def identity(ctx: Context) -> CallIdentity:
        return identity_from_meta(ctx.request_context.meta)

    async def update(self, identity: CallIdentity, mutate: Callable[[dict[str, Any]], T]) -> T:
        return await update_state(self.services.state, identity, self.plugin_id, mutate)

    async def read(self, identity: CallIdentity) -> dict[str, Any]:
        return await read_state(self.services.state, identity, self.plugin_id)
