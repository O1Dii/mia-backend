"""Declarative host: actions become MCP tools, the view becomes an MCP App."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.modules.plugins.builtin import InMemoryPluginStateStore
from app.modules.plugins.builtin.bridge import BRIDGE_MARKER, CSP
from app.modules.plugins.declarative import build_declarative_server, convert_manifest
from mcp_types import TextResourceContents

from tests.plugins.support import (
    ROOT,
    Caller,
    FakeLlm,
    connect,
    load_json,
    make_services,
    meta,
)

EXAMPLES = ROOT / "contracts/plugins"


def converted(name: str) -> dict[str, Any]:
    return convert_manifest(load_json(EXAMPLES / name)).manifest


async def page(client: Any, uri: str) -> str:
    content = (await client.read_resource(uri)).contents[0]
    assert isinstance(content, TextResourceContents)
    assert content.mime_type == "text/html;profile=mcp-app"
    return content.text


async def test_actions_become_tools_and_state_patch_persists_per_installation() -> None:
    manifest = converted("v2/examples/focus-board.json")
    state = InMemoryPluginStateStore()
    server = build_declarative_server(manifest, make_services(state=state))
    async with connect(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {"set_focus", "get_state"}
        schema = tools["set_focus"].input_schema
        assert schema["properties"]["task"]["maxLength"] == 240
        assert schema["required"] == ["task"]
        assert tools["set_focus"].meta == {
            "ui": {"resourceUri": "ui://community.focus-board/tile"},
            "ui/resourceUri": "ui://community.focus-board/tile",
        }
        # get_state is for the tile only: hidden from the model by MCP Apps visibility
        assert tools["get_state"].meta is not None
        assert tools["get_state"].meta["ui"]["visibility"] == ["app"]

        a = Caller(client, meta(installation="a"))
        b = Caller(client, meta(installation="b"))
        result = await a("set_focus", {"task": "Дописать тесты"})
        assert result["state"] == {"task": "Дописать тесты"}
        assert result["changed"] == {"task": "Дописать тесты"}
        assert (await a("get_state"))["state"] == {"task": "Дописать тесты"}
        assert (await b("get_state"))["state"] == {}
        assert "Дописать тесты" in await a.text("set_focus", {"task": "Дописать тесты"})
        # a second server over the same store sees the state (persistence)
    async with connect(build_declarative_server(manifest, make_services(state=state))) as client:
        assert (await Caller(client, meta(installation="a"))("get_state"))["state"]["task"]


async def test_inputs_are_strictly_validated() -> None:
    manifest = converted("v2/examples/focus-board.json")
    async with connect(build_declarative_server(manifest, make_services())) as client:
        call = Caller(client, meta())
        assert await call.error("set_focus", {"task": "x" * 241})
        assert await call.error("set_focus", {"task": 5})
        assert await call.error("set_focus", {})
        assert (await call("get_state"))["state"] == {}


async def test_typed_inputs_integer_and_boolean() -> None:
    manifest = converted("v2/examples/focus-board.json")
    manifest["server"]["actions"] = [
        {
            "name": "configure",
            "description": "Configure the app",
            "inputs": {
                "count": {"type": "integer", "minimum": 1, "maximum": 5},
                "loud": {"type": "boolean"},
            },
            "effect": "state.patch",
        }
    ]
    manifest["tools"] = {"configure": {"risk": "write_reversible"}}
    async with connect(build_declarative_server(manifest, make_services())) as client:
        call = Caller(client, meta())
        assert (await call("configure", {"count": 3, "loud": True}))["state"] == {
            "count": 3,
            "loud": True,
        }
        assert await call.error("configure", {"count": 0, "loud": True})
        assert await call.error("configure", {"count": 6, "loud": True})
        assert await call.error("configure", {"count": "3", "loud": True})
        assert await call.error("configure", {"count": 3, "loud": 1})


async def test_fields_view_is_an_mcp_app_with_escaped_field_names() -> None:
    manifest = converted("v2/examples/focus-board.json")
    manifest["server"]["view"]["fields"] = ["task", "</script><b>x"]
    async with connect(build_declarative_server(manifest, make_services())) as client:
        resources = (await client.list_resources()).resources
        assert [str(r.uri) for r in resources] == ["ui://community.focus-board/tile"]
        html = await page(client, "ui://community.focus-board/tile")
    assert BRIDGE_MARKER not in html and CSP in html and "ui/initialize" in html
    assert "\\u003c/script>" in html and "</script><b>x" not in html
    assert '"task"' in html and "textContent" in html
    assert "<title>Мой фокус</title>" in html


async def test_html_view_embeds_the_author_html_with_the_mia_compat_shim() -> None:
    manifest = converted("v2/examples/focus-html.json")
    async with connect(build_declarative_server(manifest, make_services())) as client:
        html = await page(client, "ui://community.focus-html/tile")
    assert "mia.invoke('set_focus'" in html  # the author's code is untouched
    assert html.index("window.mia = {") < html.index("mia.invoke('set_focus'")
    assert "state.changed" in html and CSP in html


async def test_model_prompt_uses_the_users_model_and_stores_the_answer() -> None:
    manifest = converted("v1/examples/day-planner.json")
    llm = FakeLlm("План: 1. Писать")
    async with connect(build_declarative_server(manifest, make_services(llm=llm))) as client:
        call = Caller(client, meta())
        result = await call("run", {"input": "тесты, письма"})
        assert result["answer"] == "План: 1. Писать"
        assert result["state"] == {"answer": "План: 1. Писать"}
        assert "тесты, письма" in llm.prompts[0] and "{{input}}" not in llm.prompts[0]
        assert llm.identities[0].installation_id == "inst-a"
        assert (await call("get_state"))["state"]["answer"] == "План: 1. Писать"
        llm.fail = "Выберите модель."
        assert "Выберите модель" in await call.error("run", {"input": "x"})
        assert await call.error("run", {"input": "x" * 8001})
    async with connect(build_declarative_server(manifest, make_services())) as client:
        assert "Подключите модель" in await Caller(client, meta()).error("run", {"input": "x"})


async def test_prompt_template_reads_state_and_inputs() -> None:
    manifest = load_json(ROOT / "contracts/mvp/v1/examples/plugin-manifest/focus.mia.json")
    llm = FakeLlm("ok")
    async with connect(build_declarative_server(manifest, make_services(llm=llm))) as client:
        call = Caller(client, meta())
        await call("set_focus", {"task": "A"})
        await call("plan_day")
        # the contract example reads {{state.focus}}; our state has `task`, so it renders empty
        assert llm.prompts[-1] == "Составь короткий план дня по задачам: "
    manifest["server"]["actions"][1]["prompt"] = (
        "Задача: {{state.task}} / {{ missing }} / {{state.nope}}"
    )
    async with connect(build_declarative_server(manifest, make_services(llm=llm))) as client:
        call = Caller(client, meta())
        await call("set_focus", {"task": "A"})
        await call("plan_day")
        assert llm.prompts[-1] == "Задача: A /  / "


async def test_prompt_that_is_too_long_is_refused() -> None:
    manifest = converted("v1/examples/day-planner.json")
    action = manifest["server"]["actions"][0]
    action["inputs"]["input"]["maxLength"] = 16000
    action["prompt"] = "{{input}}{{input}}"
    llm = FakeLlm()
    async with connect(build_declarative_server(manifest, make_services(llm=llm))) as client:
        call = Caller(client, meta())
        assert "слишком длинный" in await call.error("run", {"input": "x" * 13000})
        assert llm.prompts == []
        assert (await call("run", {"input": "x" * 3000}))["answer"]


async def test_missing_identity_is_a_tool_error() -> None:
    manifest = converted("v2/examples/focus-board.json")
    async with connect(build_declarative_server(manifest, make_services())) as client:
        result = await client.call_tool("set_focus", {"task": "x"})
        assert result.is_error
        text = "".join(getattr(part, "text", "") for part in result.content)
        assert "mia/user_id" in text


async def test_skill_md_becomes_server_instructions(tmp_path: Path) -> None:
    converted_plugin = convert_manifest(load_json(EXAMPLES / "v2/examples/focus-board.json"))
    (tmp_path / "SKILL.md").write_text(converted_plugin.skill_md, encoding="utf-8")
    server = build_declarative_server(converted_plugin.manifest, make_services(), tmp_path)
    async with connect(server) as client:
        assert client.instructions is not None and "set_focus" in client.instructions


def test_reserved_names_and_wrong_kind_are_rejected() -> None:
    manifest = converted("v2/examples/focus-board.json")
    manifest["server"]["actions"][0]["name"] = "get_state"
    manifest["tools"] = {"get_state": {"risk": "read"}}
    with pytest.raises(ValueError, match="reserved"):
        build_declarative_server(manifest, make_services())
    manifest = converted("v2/examples/focus-board.json")
    manifest["server"]["actions"][0]["inputs"]["mia_ctx"] = {"type": "string"}
    with pytest.raises(ValueError, match="reserved"):
        build_declarative_server(manifest, make_services())
    with pytest.raises(ValueError, match="not a declarative"):
        build_declarative_server({"server": {"kind": "builtin"}}, make_services())
