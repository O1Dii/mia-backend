const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1];
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
function el() {
  return { children: [], textContent: '', className: '', disabled: false, listeners: {},
    appendChild(c) { this.children.push(c); },
    addEventListener(n, f) { this.listeners[n] = f; },
    get firstChild() { return this.children[0] || null; },
    set textContent(v) { this._t = v; if (v === '') this.children = []; },
    get textContent() { return this._t || ''; } };
}
const nodes = {};
global.document = { getElementById: (id) => (nodes[id] = nodes[id] || el()), createElement: () => el() };
let calls = [], cb;
global.window = { mia: { ready: Promise.resolve(), onData: (f) => { cb = f; },
  call: (name, args) => { calls.push([name, args]);
    return payload.fail ? Promise.reject(new Error('boom')) : Promise.resolve(payload.structuredContent); } } };
eval(script);
setTimeout(() => {
  const rows = nodes.list.children.map((li) => li.children.map((c) => c.textContent));
  console.log(JSON.stringify({ calls, rows, empty: nodes.empty.textContent, meta: nodes.meta.textContent,
    error: nodes.error.textContent }));
}, 20);
