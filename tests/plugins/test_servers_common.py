"""Contract every builtin server honours, checked through the SDK client (in-memory)."""

from __future__ import annotations

import pytest
from app.modules.plugins.builtin import CallIdentity, InMemoryPluginStateStore
from app.modules.plugins.builtin.bridge import BRIDGE_MARKER, CSP
from mcp_types import TextResourceContents

from tests.plugins.support import (
    FakeWeb,
    as_meta,
    builtin,
    connect,
    make_services,
    meta,
    server_for,
)

PLUGIN_IDS = [
    "builtin.alarm",
    "builtin.focus",
    "builtin.internet",
    "builtin.news",
    "builtin.notes",
    "builtin.timer",
]
# One harmless call per plugin, used to prove the identity gate.
PROBE = {
    "builtin.alarm": ("get", {}),
    "builtin.focus": ("get", {}),
    "builtin.internet": ("search", {"query": "mia"}),
    "builtin.news": ("get", {}),
    "builtin.notes": ("list", {}),
    "builtin.timer": ("get", {}),
}


@pytest.mark.parametrize("plugin_id", PLUGIN_IDS)
async def test_tools_list_equals_manifest_and_binds_ui(plugin_id: str) -> None:
    plugin = builtin(plugin_id)
    async with connect(server_for(plugin_id, make_services())) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == set(plugin.manifest["tools"])
        resources = {str(r.uri): r for r in (await client.list_resources()).resources}
        for tool in tools:
            assert tool.description
            assert tool.annotations is not None
            expected_read_only = plugin.manifest["tools"][tool.name]["risk"] == "read"
            assert tool.annotations.read_only_hint is expected_read_only
            assert tool.meta is not None
            uri = tool.meta["ui"]["resourceUri"]
            assert uri.startswith(f"ui://{plugin_id}/")
            assert tool.meta["ui/resourceUri"] == uri
            assert uri in resources
            assert resources[uri].mime_type == "text/html;profile=mcp-app"
        for tile in plugin.manifest.get("surfaces", {}).get("tiles", []):
            assert tile["resourceUri"] in resources


@pytest.mark.parametrize("plugin_id", PLUGIN_IDS)
async def test_ui_resource_is_html_with_bridge_and_csp(plugin_id: str) -> None:
    async with connect(server_for(plugin_id, make_services())) as client:
        for resource in (await client.list_resources()).resources:
            contents = (await client.read_resource(str(resource.uri))).contents
            assert len(contents) == 1
            content = contents[0]
            assert isinstance(content, TextResourceContents)
            assert content.mime_type == "text/html;profile=mcp-app"
            assert BRIDGE_MARKER not in content.text
            assert CSP in content.text
            assert "window.mia" in content.text
            assert "ui/initialize" in content.text
            assert content.text.lstrip().lower().startswith("<!doctype html>")


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("plugin_id", PLUGIN_IDS)
async def test_call_without_identity_is_a_clear_tool_error(plugin_id: str, mode: str) -> None:
    name, arguments = PROBE[plugin_id]
    async with connect(server_for(plugin_id, make_services(web=FakeWeb())), mode) as client:
        result = await client.call_tool(name, arguments)
        assert result.is_error
        text = "".join(getattr(part, "text", "") for part in result.content)
        assert "mia/user_id" in text and "mia/installation_id" in text
        partial = await client.call_tool(name, arguments, meta=as_meta({"mia/user_id": "u"}))
        assert partial.is_error
        blank = await client.call_tool(
            name, arguments, meta=as_meta({**meta(), "mia/workspace_id": ""})
        )
        assert blank.is_error
        # The server survives the failures and still answers a valid call.
        good = await client.call_tool(name, arguments, meta=meta())
        assert not good.is_error


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_identity_reaches_the_server_in_both_protocol_modes(mode: str) -> None:
    state = InMemoryPluginStateStore()
    async with connect(server_for("builtin.notes", make_services(state=state)), mode) as client:
        await client.call_tool("add", {"text": "x"}, meta=meta(installation="inst-m"))
    snapshot = await state.load(CallIdentity("user-1", "ws-1", "inst-m"), "builtin.notes")
    assert snapshot.data["notes"][0]["text"] == "x"
    assert snapshot.version == 1


@pytest.mark.parametrize("plugin_id", PLUGIN_IDS)
async def test_tool_input_schemas_are_objects(plugin_id: str) -> None:
    async with connect(server_for(plugin_id, make_services())) as client:
        for tool in (await client.list_tools()).tools:
            assert tool.input_schema["type"] == "object"
            assert "ctx" not in tool.input_schema.get("properties", {})
