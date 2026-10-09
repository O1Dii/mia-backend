"""Declarative MCP server: tools come from ``server.actions``, the view becomes an MCP App.

Effects (v2 semantics, ported from ``app/web_poc/runtime.py``):

* ``state.patch`` writes the validated arguments into the installation's state;
* ``model.prompt`` renders ``prompt`` (``{{input}}``, ``{{<input>}}``, ``{{state.<key>}}``),
  sends it to the user's model through the host (``HostServices.llm``) and stores the
  reply as ``state.answer``.

Besides the manifest's tools the server has an *app-only* tool ``get_state`` (hidden from
the model via ``_meta.ui.visibility``) so the tile can read the state without an LLM.
"""

from __future__ import annotations

import html
import inspect
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult
from pydantic import Field

from app.modules.plugins.builtin.bridge import BRIDGE_MARKER
from app.modules.plugins.builtin.kit import PluginKit, ok
from app.modules.plugins.builtin.services import HostLlmError, HostServices

GET_STATE_TOOL = "get_state"
CONTEXT_PARAMETER = "mia_ctx"
MAX_PROMPT_CHARS = 24_000
_PLACEHOLDER = re.compile(r"\{\{\s*(state\.)?([a-z][a-z0-9_]*)\s*\}\}")

_BASE_STYLE = (
    "<style>:root{color-scheme:dark}body{font:14px system-ui,sans-serif;background:#181d16;"
    "color:#d9e8c6;margin:12px}button{padding:8px;border-radius:8px;border:0;background:#d6f882;"
    "color:#223018}input,textarea{font:inherit;width:90%;padding:8px;background:#252e1e;"
    "color:#eaf4dc;border:1px solid #5a6d44;border-radius:8px}dt{color:#8fa07a;font-size:12px}"
    "dd{margin:0 0 8px;overflow-wrap:anywhere}</style>"
)

_FIELDS_SCRIPT = """(function () {
  var fields = JSON.parse(document.getElementById('cfg').textContent);
  var box = document.getElementById('fields');
  function render(state) {
    box.textContent = '';
    fields.forEach(function (key) {
      var dt = document.createElement('dt'); dt.textContent = key;
      var dd = document.createElement('dd');
      var value = state[key];
      dd.textContent = value === undefined || value === null || value === '' ? '—' : String(value);
      box.append(dt, dd);
    });
  }
  window.mia.onData(function (data) { if (data && data.state) render(data.state); });
  render({});
  window.mia.ready.then(function () { return window.mia.call('get_state'); }).catch(function () {});
})();"""

_COMPAT_SCRIPT = """(function () {
  var base = window.mia;
  function unwrap(data) { return data && data.state !== undefined ? data.state : data; }
  function ready(fn) { return base.ready.then(fn).then(unwrap); }
  base.onData(function (data) {
    if (!data || data.state === undefined) return;
    window.dispatchEvent(new MessageEvent('message', {
      data: { event: 'state.changed', state: data.state }, source: window.parent
    }));
  });
  window.mia = {
    ready: base.ready, onData: base.onData, call: base.call,
    state: function () { return ready(function () { return base.call('get_state'); }); },
    invoke: function (name, args) {
      return ready(function () { return base.call(name, args || {}); });
    },
    generate: function () {
      return Promise.reject(new Error('mia.generate: use a model.prompt action'));
    }
  };
})();"""


def view_html(manifest: Mapping[str, Any]) -> str:
    """The MCP App document of the manifest's view (bridge marker included)."""
    view = manifest["server"]["view"]
    title = html.escape(str(manifest["name"]))
    head = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>{BRIDGE_MARKER}{_BASE_STYLE}</head><body>"
    )
    if view["type"] == "html":
        return f"{head}<script>{_COMPAT_SCRIPT}</script>{view.get('html', '')}</body></html>"
    fields = json.dumps(view.get("fields", []), ensure_ascii=False).replace("<", "\\u003c")
    return (
        f'{head}<dl id="fields"></dl><script type="application/json" id="cfg">{fields}</script>'
        f"<script>{_FIELDS_SCRIPT}</script></body></html>"
    )


def _annotation(spec: Mapping[str, Any]) -> Any:
    description = spec.get("description") or None
    kind = spec["type"]
    if kind == "string":
        return Annotated[
            str, Field(strict=True, max_length=spec.get("maxLength", 1000), description=description)
        ]
    if kind == "integer":
        return Annotated[
            int,
            Field(
                strict=True,
                ge=spec.get("minimum", 0),
                le=spec.get("maximum", 1_000_000),
                description=description,
            ),
        ]
    return Annotated[bool, Field(strict=True, description=description)]


def _with_signature(
    handler: Callable[..., Any], name: str, inputs: Mapping[str, Mapping[str, Any]]
) -> Callable[..., Any]:
    """Give ``handler`` a signature from the declared inputs so the SDK derives the schema."""
    parameters = [
        inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, annotation=_annotation(inputs[key]))
        for key in inputs
    ]
    parameters.append(
        inspect.Parameter(CONTEXT_PARAMETER, inspect.Parameter.KEYWORD_ONLY, annotation=Context)
    )
    handler.__name__ = name
    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters, return_annotation=CallToolResult
    )
    handler.__annotations__ = {p.name: p.annotation for p in parameters} | {
        "return": CallToolResult
    }
    return handler


def render_prompt(template: str, arguments: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    def substitute(match: re.Match[str]) -> str:
        source = state if match.group(1) else arguments
        value = source.get(match.group(2), "")
        return "" if value is None else str(value)

    prompt = _PLACEHOLDER.sub(substitute, template)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ToolError("Запрос к модели слишком длинный.")
    return prompt


def build_declarative_server(
    manifest: Mapping[str, Any], services: HostServices, plugin_dir: Path | None = None
) -> MCPServer:
    """Build the MCP server of a ``server.kind: declarative`` manifest."""
    server_spec = manifest["server"]
    if server_spec.get("kind") != "declarative":
        raise ValueError("not a declarative manifest")
    skill = None
    if plugin_dir is not None:
        parts = [
            (plugin_dir / path).read_text(encoding="utf-8")
            for path in manifest.get("skills", [])
            if (plugin_dir / path).is_file()
        ]
        skill = "\n\n".join(parts) or None
    kit = PluginKit(manifest, services, ui={"tile": view_html(manifest)}, instructions=skill)
    plugin_id = kit.plugin_id

    for action in server_spec["actions"]:
        if action["name"] == GET_STATE_TOOL or CONTEXT_PARAMETER in action["inputs"]:
            raise ValueError(f"reserved name in action {action['name']!r}")
        kit.tool(action["name"], description=action["description"], ui="tile")(
            _with_signature(
                _action_handler(kit, services, action), action["name"], action["inputs"]
            )
        )

    async def get_state(*, mia_ctx: Context) -> CallToolResult:
        data = await kit.read(kit.identity(mia_ctx))
        return ok(
            "Состояние приложения: " + json.dumps(data, ensure_ascii=False)[:2000], {"state": data}
        )

    get_state.__annotations__ = {"mia_ctx": Context, "return": CallToolResult}
    kit.tool(
        GET_STATE_TOOL, description="Read the app state for its tile.", ui="tile", app_only=True
    )(get_state)
    kit.verify_tools()
    assert plugin_id == manifest["id"]
    return kit.server


def _action_handler(
    kit: PluginKit, services: HostServices, action: Mapping[str, Any]
) -> Callable[..., Any]:
    effect = action["effect"]
    template = action.get("prompt") or ""
    declared = set(action["inputs"])

    async def handler(**kwargs: Any) -> CallToolResult:
        context = kwargs.pop(CONTEXT_PARAMETER)
        identity = kit.identity(context)
        arguments = {key: kwargs[key] for key in declared}
        if effect == "state.patch":

            def patch(data: dict[str, Any]) -> dict[str, Any]:
                data.update(arguments)
                return dict(data)

            state = await kit.update(identity, patch)
            shown = ", ".join(f"{key}={str(value)[:80]!r}" for key, value in arguments.items())
            return ok(f"Сохранено: {shown}.", {"state": state, "changed": arguments})

        if services.llm is None:
            raise ToolError("Подключите модель: действие использует модель пользователя.")
        current = await kit.read(identity)
        prompt = render_prompt(template, arguments, current)
        try:
            reply = await services.llm.generate(prompt, identity=identity, plugin_id=kit.plugin_id)
        except HostLlmError as error:
            raise ToolError(str(error)) from None

        def store(data: dict[str, Any]) -> dict[str, Any]:
            data["answer"] = reply.text
            return dict(data)

        state = await kit.update(identity, store)
        return ok(reply.text, {"state": state, "answer": reply.text})

    return handler
