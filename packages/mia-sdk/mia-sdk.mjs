/** Model-neutral client. Provider keys never leave MIA. Tokens stay in memory. */
export class MiaClient {
  #token = '';
  constructor({ baseUrl = import.meta.url.includes('/api/web/sdk/') ? import.meta.url.split('/api/web/sdk/')[0] : 'http://127.0.0.1:8001', pluginId }) {
    const url = new URL(baseUrl);
    if (url.username || url.password || url.search || url.hash || !(url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname)))) {
      throw new Error('Use an HTTPS MIA URL or localhost.');
    }
    this.baseUrl = url.origin + url.pathname.replace(/\/$/, '');
    this.pluginId = pluginId;
  }
  async #request(path, body, authorized = true) {
    if (authorized && !this.#token) throw new Error('Сначала подключитесь к Мии.');
    const response = await fetch(`${this.baseUrl}/api/sdk${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'omit',
      headers: {
        'Content-Type': 'application/json',
        ...(authorized ? { Authorization: `Bearer ${this.#token}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(160000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'MIA request failed');
    return result;
  }
  beginPair() { return this.#request('/pair', { plugin_id: this.pluginId }, false); }
  async pollPair(deviceCode) {
    const result = await this.#request('/pair/poll', { device_code: deviceCode }, false);
    if (result.status === 'approved') this.#token = result.access_token;
    // Do not expose credentials to application logs or views.
    return { status: result.status, subject_id: result.subject_id, plugin_id: result.plugin_id };
  }
  disconnect() { this.#token = ''; }
  state() { return this.#request('/state'); }
  invoke(name, args = {}, requestId = crypto.randomUUID()) {
    return this.#request('/actions', { name, arguments: args, request_id: requestId });
  }
  generate(messages, { requestId = crypto.randomUUID(), useTools = true } = {}) {
    return this.#request('/generate', { messages, request_id: requestId, use_tools: useTools });
  }
}

/** Narrow bridge for sandboxed HTML views; no provider keys or bearer tokens. */
export class MiaView {
  #pending = new Map();
  constructor() {
    window.addEventListener('message', event => {
      if (event.source !== window.parent || event.data?.channel !== 'mia-sdk:v1') return;
      const pending = this.#pending.get(event.data.id);
      if (!pending) return;
      this.#pending.delete(event.data.id);
      clearTimeout(pending.timer);
      if (event.data.error) pending.reject(new Error(event.data.error));
      else pending.resolve(event.data.result);
    });
  }
  request(method, params = {}) {
    const id = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error('MIA view request timed out'));
      }, 160000);
      this.#pending.set(id, { resolve, reject, timer });
      window.parent.postMessage({ channel: 'mia-sdk:v1', id, method, params }, '*');
    });
  }
  state() { return this.request('state.get'); }
  invoke(action, args = {}) { return this.request('actions.invoke', { name: action, arguments: args }); }
  generate(text) { return this.request('ai.generate', { text }); }
}
