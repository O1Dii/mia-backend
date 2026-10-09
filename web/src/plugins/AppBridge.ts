// Host side of MCP Apps (ui/initialize, tools/call, ui/notifications/*): JSON-RPC 2.0 over
// postMessage with one sandboxed iframe. Written against the MCP Apps specification instead of
// pulling a UI client library: the surface Mia needs is five methods, and every one is policed here.
//
// Trust model: the iframe has an opaque origin (sandbox="allow-scripts", no allow-same-origin), so
// it cannot read host cookies, storage or DOM and its fetches are cross-origin without credentials.
// Everything it can do goes through this bridge: only allowlisted methods are answered, `tools/call`
// is forwarded to the host callback (which asks the backend; the backend enforces the tool policy),
// and messages from any window other than the frame are dropped.

export const MCP_APP_PROTOCOL_VERSION = '2026-01-26'

export type CallToolResult = {
  content: { type: 'text'; text: string }[]
  structuredContent?: Record<string, unknown>
  isError?: boolean
}

export type HostContext = {
  theme: 'light' | 'dark'
  locale: string
  timeZone: string
  platform: 'web'
  displayMode: 'inline'
}

export type BridgeHost = {
  /** Forward `tools/call` to the backend; never throws (errors become isError results). */
  callTool: (name: string, args: Record<string, unknown>) => Promise<CallToolResult>
  /** The iframe asked to open a link; only https is ever passed on. */
  openLink: (url: string) => void
  /** The app finished its handshake. */
  onInitialized: () => void
  /** The frame navigated away from its document: it is no longer trusted. */
  onTampered: () => void
}

type Rpc = { jsonrpc: '2.0'; id?: number | string; method?: string; params?: unknown }

const METHODS = new Set([
  'ui/initialize',
  'ui/notifications/initialized',
  'ui/notifications/size-changed',
  'ui/open-link',
  'tools/call',
  'ping',
])
const MAX_IN_FLIGHT = 4
const WINDOW_MS = 10_000
const MAX_CALLS_PER_WINDOW = 40

function isRpc(value: unknown): value is Rpc {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as Rpc).jsonrpc === '2.0' &&
    (typeof (value as Rpc).method === 'string' || (value as Rpc).id !== undefined)
  )
}

export class AppBridge {
  private frame: HTMLIFrameElement
  private host: BridgeHost
  private context: HostContext
  private loads = 0
  private inFlight = 0
  private calls: number[] = []
  private initialized = false
  private disposed = false

  constructor(frame: HTMLIFrameElement, host: BridgeHost, context: HostContext) {
    this.frame = frame
    this.host = host
    this.context = context
    window.addEventListener('message', this.onMessage)
    frame.addEventListener('load', this.onLoad)
  }

  get ready(): boolean {
    return this.initialized
  }

  dispose() {
    this.disposed = true
    window.removeEventListener('message', this.onMessage)
    this.frame.removeEventListener('load', this.onLoad)
  }

  /** Push a tool result (refresh, or the first data) to the app: `ui/notifications/tool-result`. */
  notifyToolResult(result: CallToolResult) {
    if (this.initialized)
      this.send({ jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: result })
  }

  setContext(context: HostContext) {
    this.context = context
    if (this.initialized)
      this.send({
        jsonrpc: '2.0',
        method: 'ui/notifications/host-context-changed',
        params: context,
      })
  }

  private onLoad = () => {
    // srcdoc loads once. A second load means the document navigated itself somewhere else.
    this.loads += 1
    if (this.loads > 1 && !this.disposed) {
      this.disposed = true
      window.removeEventListener('message', this.onMessage)
      this.host.onTampered()
    }
  }

  private send(
    message: Rpc | { jsonrpc: '2.0'; id: number | string; result?: unknown; error?: unknown },
  ) {
    // The frame has an opaque origin, so "*" is the only possible target; the window is ours.
    this.frame.contentWindow?.postMessage(message, '*')
  }

  private reply(id: number | string, result: unknown) {
    this.send({ jsonrpc: '2.0', id, result })
  }

  private fail(id: number | string, code: number, message: string) {
    this.send({ jsonrpc: '2.0', id, error: { code, message } })
  }

  private onMessage = (event: MessageEvent) => {
    if (this.disposed || event.source !== this.frame.contentWindow) return
    const message: unknown = event.data
    if (!isRpc(message) || typeof message.method !== 'string') return
    const { id, method } = message
    if (!METHODS.has(method)) {
      if (id !== undefined) this.fail(id, -32601, `Method not found: ${method}`)
      return
    }
    const params = (typeof message.params === 'object' && message.params) || {}
    switch (method) {
      case 'ping':
        if (id !== undefined) this.reply(id, {})
        return
      case 'ui/initialize':
        if (id !== undefined)
          this.reply(id, {
            protocolVersion: MCP_APP_PROTOCOL_VERSION,
            hostInfo: { name: 'mia-web', version: '1.0.0' },
            hostCapabilities: { serverTools: {}, openLinks: {} },
            hostContext: { ...this.context, availableDisplayModes: ['inline'] },
          })
        return
      case 'ui/notifications/initialized':
        if (!this.initialized) {
          this.initialized = true
          this.host.onInitialized()
        }
        return
      case 'ui/notifications/size-changed':
        return // tiles have a fixed size on the grid
      case 'ui/open-link': {
        const url = String((params as { url?: unknown }).url ?? '')
        if (id === undefined) return
        if (/^https:\/\//i.test(url)) {
          this.host.openLink(url)
          this.reply(id, {})
        } else this.fail(id, -32602, 'Only https links can be opened')
        return
      }
      case 'tools/call': {
        if (id === undefined) return
        const { name, arguments: args } = params as { name?: unknown; arguments?: unknown }
        if (typeof name !== 'string' || !name || name.length > 128) {
          this.fail(id, -32602, 'Invalid tool name')
          return
        }
        const now = Date.now()
        this.calls = this.calls.filter((t) => now - t < WINDOW_MS)
        if (this.inFlight >= MAX_IN_FLIGHT || this.calls.length >= MAX_CALLS_PER_WINDOW) {
          this.fail(id, -32000, 'Too many tool calls')
          return
        }
        this.calls.push(now)
        this.inFlight += 1
        const argumentsObject =
          typeof args === 'object' && args !== null && !Array.isArray(args)
            ? (args as Record<string, unknown>)
            : {}
        void this.host
          .callTool(name, argumentsObject)
          .then((result) => this.reply(id, result))
          .catch(() =>
            this.reply(id, {
              content: [{ type: 'text', text: 'Не удалось выполнить действие.' }],
              isError: true,
            } satisfies CallToolResult),
          )
          .finally(() => {
            this.inFlight -= 1
          })
        return
      }
    }
  }
}
