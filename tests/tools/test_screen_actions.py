"""Screen-only actions of tool results (M41-R1): validation, router hand-off, trust boundary."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from app.core.ports.screen_action import (
    ACTION_META_KEY,
    action_from_meta,
    listed_action,
    parse_action,
    safe_url,
)
from app.core.ports.tool_router import ToolCallRequest
from app.modules.plugins.builtin.kit import ok, screen_action
from app.modules.plugins.registry import (
    InstallationRecord,
    InstalledPlugin,
    LoadedManifest,
    PluginRegistry,
    StaticInstallationReader,
    StaticManifestSource,
)
from app.modules.runs.engine import _completed_payload
from app.modules.tools.audit import InMemoryToolAudit
from app.modules.tools.digest import ServerTool, compute_tools_digest
from app.modules.tools.mcp_client import McpConnector, McpResult
from app.modules.tools.router import McpToolRouter, RouterConfig
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent

from tests.fakes import assert_mvp_valid
from tests.tools.helpers import PLUGIN_ID, TOOL_POLICIES, digest_of, manifest, write_package

LINK = "https://t.me/mia_test_bot?start=ONE-TIME-SECRET"
ACTION = {
    "kind": "open_url",
    "url": LINK,
    "label": "Открыть Telegram",
    "expires_at": "2030-01-01T00:00:00Z",
}


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/mia_bot?start=abc",
        "https://example.com",
        "https://example.com?x=1",
        "tg://resolve?domain=mia_bot&start=abc",
    ],
)
def test_safe_urls(url: str) -> None:
    assert safe_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://t.me/mia_bot",
        "javascript:alert(1)",
        "data:text/html,hi",
        "intent://scan#Intent;end",
        "https://user:pass@evil.example/",
        "https://",
        "https://t.me/a b",
        "https://t.me/\nx",
        'https://t.me/"onmouseover',
        "tg://",
        "",
        None,
        42,
        "https://t.me/" + "x" * 2048,
    ],
)
def test_unsafe_urls_are_refused(url: object) -> None:
    assert safe_url(url) is None


def test_parse_action_normalizes_and_refuses_bad_fields() -> None:
    parsed = parse_action({**ACTION, "expires_at": "2030-01-01T03:00:00+03:00", "extra": 1})
    assert parsed == ACTION  # UTC "Z", unknown fields dropped
    assert parse_action({**ACTION, "label": "  Открыть  "}) == {**ACTION, "label": "Открыть"}
    for bad in (
        {**ACTION, "kind": "run_tool"},
        {**ACTION, "label": ""},
        {**ACTION, "label": "x" * 41},
        {**ACTION, "label": "a\u0000b"},
        {**ACTION, "url": "http://t.me/x"},
        {**ACTION, "expires_at": "2030-01-01T00:00:00"},  # no offset
        {**ACTION, "expires_at": "soon"},
        "open_url",
        None,
    ):
        assert parse_action(bad) is None, bad
    without_expiry = {k: v for k, v in ACTION.items() if k != "expires_at"}
    assert parse_action(without_expiry) == without_expiry
    assert action_from_meta({ACTION_META_KEY: ACTION, "other": 1}) == ACTION
    assert action_from_meta({"other": 1}) is None and action_from_meta(None) is None


def test_expired_actions_are_listed_without_the_url() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    assert listed_action(ACTION, now - timedelta(seconds=1)) == ACTION
    expired = listed_action(ACTION, now)
    assert expired == {
        "kind": "open_url",
        "label": "Открыть Telegram",
        "expires_at": "2030-01-01T00:00:00Z",
        "expired": True,
    }
    assert_mvp_valid(expired, "common", "screenAction")
    assert listed_action({"kind": "open_url"}, now) is None


def test_kit_helper_builds_the_meta_and_refuses_unsafe_links() -> None:
    action = screen_action(LINK, "Открыть Telegram", expires_at="2030-01-01T00:00:00Z")
    assert action == ACTION
    result = ok("Кнопка на экране.", {"linked": False}, action=action)
    assert result.meta == {ACTION_META_KEY: ACTION}
    assert LINK not in str(result.content) and LINK not in str(result.structured_content)
    assert ok("x", {}).meta is None
    with pytest.raises(ValueError):
        screen_action("javascript:alert(1)", "Открыть")
    with pytest.raises(ValueError):
        screen_action(LINK, "")


# -- router -------------------------------------------------------------------------------


def _server(action: Mapping[str, Any] | None) -> MCPServer:
    """Tools named like the test manifest; ``get`` attaches ``action`` to its result."""
    server = MCPServer("test-actions", version="1.0.0")

    async def get(ctx: Context[Any, Any]) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(text="Кнопка показана на экране.")],
            structured_content={"linked": False},
            meta={ACTION_META_KEY: dict(action)} if action is not None else None,
        )

    async def broken(ctx: Context[Any, Any]) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(text="failed")],
            is_error=True,
            meta={ACTION_META_KEY: dict(ACTION)},
        )

    server.add_tool(get, description="List timers.")
    server.add_tool(broken, description="Always fails.")
    return server


async def _router(
    tmp_path: Path, server: MCPServer, *, body: dict[str, Any] | None = None
) -> tuple[McpToolRouter, str]:
    body = body or manifest(tools={"get": TOOL_POLICIES["get"], "broken": TOOL_POLICIES["broken"]})
    package = write_package(tmp_path / "pkg", body)
    user_id = str(uuid.uuid4())
    installation = InstallationRecord(
        id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        plugin_id=PLUGIN_ID,
        version="1.0.0",
        status="active",
        grants=(),
        tools_digest=await digest_of(server),
        manifest=body,
    )
    router = McpToolRouter(
        registry=PluginRegistry(
            StaticInstallationReader({user_id: [installation]}),
            StaticManifestSource([LoadedManifest(body, package)]),
        ),
        connector=McpConnector(server_factory=lambda raw, plugin_dir: server),
        audit=InMemoryToolAudit(),
        config=RouterConfig(list_cache_s=0),
    )
    return router, user_id


def _context(user_id: str) -> Any:
    from app.core.ports.tool_router import ToolContext

    return ToolContext(user_id=user_id, run_id=str(uuid.uuid4()), profile="chat")


def _event(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "sequence": 1,
        "cursor": "c1",
        "type": "tool.call.completed",
        "at": "2026-10-10T12:00:00Z",
        "payload": payload,
    }


async def test_in_process_action_reaches_the_ui_payload_but_not_the_model(tmp_path: Path) -> None:
    router, user = await _router(tmp_path, _server(ACTION))

    outcome = await router.call(_context(user), ToolCallRequest("c1", f"{PLUGIN_ID}.get", {}))

    assert outcome.status == "succeeded" and outcome.action == ACTION
    assert LINK not in outcome.content and LINK not in str(outcome.structured_content)
    payload = _completed_payload("c1", outcome)
    assert payload["action"] == ACTION
    assert_mvp_valid(_event(payload), "run-events")


async def test_unsafe_or_failed_actions_are_dropped(tmp_path: Path) -> None:
    router, user = await _router(tmp_path, _server({**ACTION, "url": "javascript:alert(1)"}))

    unsafe = await router.call(_context(user), ToolCallRequest("c1", f"{PLUGIN_ID}.get", {}))
    failed = await router.call(_context(user), ToolCallRequest("c2", f"{PLUGIN_ID}.broken", {}))

    assert unsafe.status == "succeeded" and unsafe.action is None
    assert "action" not in _completed_payload("c1", unsafe)
    assert failed.status == "failed" and failed.action is None
    assert "action" not in _completed_payload("c2", failed)


class _RemoteConnector(McpConnector):
    """A remote server that tries to put a button on the user's screen."""

    def __init__(self) -> None:
        super().__init__()

    async def list_tools(self, plugin: InstalledPlugin, *, user_id: str) -> list[ServerTool]:
        return [ServerTool("get", "List timers.", {"type": "object", "properties": {}})]

    async def call_tool(
        self,
        plugin: InstalledPlugin,
        *,
        user_id: str,
        name: str,
        arguments: Mapping[str, Any],
        meta: Mapping[str, Any],
        timeout_s: float,
    ) -> McpResult:
        return McpResult("ok", {"x": 1}, False, meta={ACTION_META_KEY: dict(ACTION)})


async def test_remote_servers_cannot_attach_screen_actions(tmp_path: Path) -> None:
    body = manifest(
        server={"kind": "remote", "url": "https://mcp.example.com/mcp", "auth": "none"},
        network=["mcp.example.com"],
        tools={"get": TOOL_POLICIES["get"]},
    )
    connector = _RemoteConnector()
    digest = compute_tools_digest(await connector.list_tools(None, user_id=""))  # type: ignore[arg-type]
    user_id = str(uuid.uuid4())
    installation = InstallationRecord(
        id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        plugin_id=PLUGIN_ID,
        version="1.0.0",
        status="active",
        grants=(),
        tools_digest=digest,
        manifest=body,
    )
    router = McpToolRouter(
        registry=PluginRegistry(
            StaticInstallationReader({user_id: [installation]}),
            StaticManifestSource([LoadedManifest(body, write_package(tmp_path / "r", body))]),
        ),
        connector=connector,
        audit=InMemoryToolAudit(),
        config=RouterConfig(list_cache_s=0),
    )

    outcome = await router.call(_context(user_id), ToolCallRequest("c1", f"{PLUGIN_ID}.get", {}))

    assert outcome.status == "succeeded" and outcome.action is None
