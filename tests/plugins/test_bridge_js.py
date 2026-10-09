"""The MCP App bridge and tile scripts: syntax and the host protocol, run under node if present."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from app.modules.plugins.builtin.bridge import BRIDGE_JS, MCP_APP_PROTOCOL_VERSION, render_app_html
from app.modules.plugins.declarative import host

from tests.plugins.support import ROOT

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
BRIDGE = BRIDGE_JS.replace("__PROTOCOL__", MCP_APP_PROTOCOL_VERSION)


def run_node(source: str, tmp_path: Path) -> str:
    script = tmp_path / "run.js"
    script.write_text(source, encoding="utf-8")
    assert NODE is not None
    done = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


@needs_node
def test_all_inline_scripts_are_syntactically_valid(tmp_path: Path) -> None:
    sources = {"bridge": BRIDGE, "fields": host._FIELDS_SCRIPT, "compat": host._COMPAT_SCRIPT}
    for path in sorted((ROOT / "app/plugins/builtin").glob("*/ui/*.html")):
        for index, script in enumerate(
            re.findall(r"<script>(.*?)</script>", path.read_text(), re.S)
        ):
            sources[f"{path.parent.parent.name}/{path.name}#{index}"] = script
    assert len(sources) >= 9
    for name, script in sources.items():
        target = tmp_path / "check.js"
        target.write_text(script, encoding="utf-8")
        assert NODE is not None
        done = subprocess.run([NODE, "--check", str(target)], capture_output=True, text=True)
        assert done.returncode == 0, f"{name}: {done.stderr}"


HARNESS = """
const listeners = [], sent = [];
const parent = { postMessage: (message) => sent.push(message) };
global.window = { parent, addEventListener: (type, fn) => listeners.push(fn) };
global.document = { title: 'Tile' };
global.setTimeout = (fn, ms) => 0; global.clearTimeout = () => {};
%s
const deliver = (data) => listeners.forEach((fn) => fn({ source: parent, data }));
(async () => {
  const out = {};
  out.init = sent[0];
  deliver({ jsonrpc: '2.0', id: sent[0].id, result: { protocolVersion: 'x' } });
  await window.mia.ready;
  out.initialized = sent[1];
  const seen = [];
  window.mia.onData((data, tool) => seen.push([data, tool]));
  const pending = window.mia.call('get', { a: 1 });
  out.call = sent[2];
  deliver({ jsonrpc: '2.0', id: sent[2].id, result: { structuredContent: { items: [1] } } });
  out.result = await pending;
  deliver({ jsonrpc: '2.0', method: 'ui/notifications/tool-result',
            params: { structuredContent: { items: [2] } } });
  const failing = window.mia.call('boom');
  deliver({ jsonrpc: '2.0', id: sent[3].id,
            result: { isError: true, content: [{ type: 'text', text: 'нет' }] } });
  out.error = await failing.then(() => null, (e) => e.message);
  const rpc = window.mia.call('x');
  deliver({ jsonrpc: '2.0', id: sent[4].id, error: { code: -32000, message: 'denied' } });
  out.rpcError = await rpc.then(() => null, (e) => e.message);
  window.parent.postMessage = sent.push.bind(sent);
  deliver({ jsonrpc: '2.0', id: 999, result: {} });  // unknown id is ignored
  out.seen = seen;
  console.log(JSON.stringify(out));
})();
"""


@needs_node
def test_bridge_speaks_the_mcp_apps_protocol(tmp_path: Path) -> None:
    out = json.loads(run_node(HARNESS % BRIDGE, tmp_path))
    assert out["init"]["method"] == "ui/initialize"
    assert out["init"]["params"]["protocolVersion"] == MCP_APP_PROTOCOL_VERSION
    assert out["initialized"] == {
        "jsonrpc": "2.0",
        "method": "ui/notifications/initialized",
        "params": {},
    }
    assert out["call"]["method"] == "tools/call"
    assert out["call"]["params"] == {"name": "get", "arguments": {"a": 1}}
    assert out["result"] == {"items": [1]}
    assert out["error"] == "нет" and out["rpcError"] == "denied"
    assert out["seen"] == [[{"items": [1]}, "get"], [{"items": [2]}, None]]


def test_render_inserts_csp_and_bridge_once() -> None:
    html = render_app_html("<html><head><!--mia:bridge--></head><body></body></html>")
    assert html.count("Content-Security-Policy") == 1 and html.count("window.mia") >= 1
    assert "<!--mia:bridge-->" not in html
    assert render_app_html("<head></head>").startswith("<head><meta http-equiv")
    assert render_app_html("<p>x</p>").startswith("<!doctype html>")
