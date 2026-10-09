"""MCP App HTML preparation: CSP + a tiny JSON-RPC bridge over ``postMessage``.

Plugin ``ui/*.html`` files carry the marker ``<!--mia:bridge-->`` inside ``<head>``; it is
replaced here by a restrictive Content-Security-Policy and the bridge script. The bridge
speaks the MCP Apps protocol (``ui/initialize``, ``tools/call``, ``ui/notifications/
tool-result``) and exposes ``window.mia``:

* ``mia.call(name, args)`` -> Promise of the tool's ``structuredContent`` (rejects on error);
* ``mia.onData(cb)`` -> ``cb(structuredContent, toolName)`` for every tool result the host
  pushes (and for results of ``mia.call``);
* ``mia.ready`` -> Promise resolved after the handshake (immediately outside a host).
"""

from __future__ import annotations

from typing import Final

BRIDGE_MARKER: Final = "<!--mia:bridge-->"
MCP_APP_PROTOCOL_VERSION: Final = "2026-01-26"

CSP: Final = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'"
)

BRIDGE_JS: Final = """(function () {
  var pending = {}, nextId = 1, listeners = [], hosted = window.parent !== window;
  function post(msg) { window.parent.postMessage(Object.assign({ jsonrpc: '2.0' }, msg), '*'); }
  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      var timer = setTimeout(function () {
        delete pending[id]; reject(new Error('Mia: host timeout'));
      }, 60000);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      post({ id: id, method: method, params: params || {} });
    });
  }
  function emit(data, tool) {
    listeners.forEach(function (cb) { try { cb(data, tool); } catch (e) { console.error(e); } });
  }
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) return;
    var msg = event.data;
    if (!msg || msg.jsonrpc !== '2.0') return;
    if (msg.id !== undefined && !msg.method && pending[msg.id]) {
      var p = pending[msg.id]; delete pending[msg.id]; clearTimeout(p.timer);
      if (msg.error) p.reject(new Error(msg.error.message || 'Mia: host error'));
      else p.resolve(msg.result);
    } else if (msg.method === 'ui/notifications/tool-result') {
      var r = msg.params || {};
      if (r.structuredContent) emit(r.structuredContent, null);
    }
  });
  var mia = {
    ready: Promise.resolve(),
    onData: function (cb) { listeners.push(cb); },
    call: function (name, args) {
      if (!hosted) return Promise.reject(new Error('Mia: no host'));
      return request('tools/call', { name: name, arguments: args || {} }).then(function (res) {
        if (res && res.isError) {
          var t = (res.content || []).map(function (c) { return c.text || ''; }).join(' ');
          throw new Error(t || 'Mia: tool error');
        }
        var data = (res && res.structuredContent) || {};
        emit(data, name);
        return data;
      });
    }
  };
  if (hosted) {
    mia.ready = request('ui/initialize', {
      protocolVersion: '__PROTOCOL__',
      appInfo: { name: document.title || 'mia-app', version: '1.0.0' },
      appCapabilities: {}
    }).then(function () { post({ method: 'ui/notifications/initialized', params: {} }); },
            function () {});
  }
  window.mia = mia;
})();"""


def render_app_html(html: str) -> str:
    """Insert CSP and the bridge at the marker (or at the start of ``<head>``)."""
    block = (
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        f"<script>{BRIDGE_JS.replace('__PROTOCOL__', MCP_APP_PROTOCOL_VERSION)}</script>"
    )
    if BRIDGE_MARKER in html:
        return html.replace(BRIDGE_MARKER, block, 1)
    lower = html.lower()
    index = lower.find("<head>")
    if index >= 0:
        cut = index + len("<head>")
        return html[:cut] + block + html[cut:]
    return f"<!doctype html><html><head>{block}</head><body>{html}</body></html>"
