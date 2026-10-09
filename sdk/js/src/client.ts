// Thin client of the Mia API: OpenAI-compatible completions/responses for app tokens,
// runs + SSE events for first-party tokens. Paths come from the generated operation table.
import { OPERATIONS, type OperationId } from './operations.ts'

export class MiaApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(`${status} ${code}: ${message}`)
    this.status = status
    this.code = code
  }
}

export type ChatMessage = {
  role: 'system' | 'developer' | 'user' | 'assistant' | 'tool'
  content: string | null
  tool_calls?: unknown[]
  tool_call_id?: string
}

export type MiaExtensions = {
  context?: boolean
  tools?: string[]
  budget?: { max_usd: number }
}

export type ChatRequest = {
  model?: string
  messages: ChatMessage[]
  tools?: unknown[]
  max_tokens?: number
  mia?: MiaExtensions
  [key: string]: unknown
}

export type SseEvent = { event: string | null; data: string }

type Fetch = typeof fetch

/** Parse a `text/event-stream` body into events (comments and keep-alives skipped). */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseEvent> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let event: string | null = null
  let data: string[] = []
  const flush = function* (): Generator<SseEvent> {
    if (data.length) yield { event, data: data.join('\n') }
    event = null
    data = []
  }
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let index: number
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, '')
      buffer = buffer.slice(index + 1)
      if (line === '') yield* flush()
      else if (line.startsWith(':')) continue
      else if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
    }
  }
  yield* flush()
}

function newKey(): string {
  return crypto.randomUUID()
}

export type MiaClientOptions = {
  baseUrl: string
  accessToken: string | (() => string | Promise<string>)
  fetch?: Fetch
}

export class MiaClient {
  readonly baseUrl: string
  private readonly token: MiaClientOptions['accessToken']
  private readonly fetcher: Fetch

  constructor(options: MiaClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, '')
    this.token = options.accessToken
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis)
  }

  private async send(
    id: OperationId,
    init: { params?: Record<string, string>; body?: unknown; idempotencyKey?: string } = {},
  ): Promise<Response> {
    const operation = OPERATIONS[id]
    let path: string = operation.path
    for (const [name, value] of Object.entries(init.params ?? {})) {
      path = path.replace(`{${name}}`, encodeURIComponent(value))
    }
    const token = typeof this.token === 'function' ? await this.token() : this.token
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` }
    if (init.body !== undefined) headers['Content-Type'] = 'application/json'
    if (operation.idempotencyKey) headers['Idempotency-Key'] = init.idempotencyKey ?? newKey()
    const response = await this.fetcher(this.baseUrl + path, {
      method: operation.method,
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    })
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as {
        error?: { code?: string; message?: string }
      } | null
      throw new MiaApiError(
        response.status,
        body?.error?.code ?? 'unknown',
        body?.error?.message ?? response.statusText,
      )
    }
    return response
  }

  async call<T>(id: OperationId, init: Parameters<MiaClient['send']>[1] = {}): Promise<T> {
    const response = await this.send(id, init)
    return (response.status === 204 ? undefined : await response.json()) as T
  }

  // -- OpenAI-compatible (scope llm.generate) ---------------------------------------------

  chatCompletion(request: ChatRequest): Promise<Record<string, any>> {
    return this.call('createChatCompletion', {
      body: { model: 'mia:default', ...request, stream: false },
    })
  }

  async *streamChatCompletion(request: ChatRequest): AsyncGenerator<Record<string, any>> {
    const response = await this.send('createChatCompletion', {
      body: { model: 'mia:default', ...request, stream: true },
    })
    for await (const { data } of readSse(response.body!)) {
      if (data === '[DONE]') return
      const chunk = JSON.parse(data) as Record<string, any>
      if (chunk.error) throw new MiaApiError(0, chunk.error.code, chunk.error.message)
      yield chunk
    }
  }

  response(request: Record<string, unknown>): Promise<Record<string, any>> {
    return this.call('createResponse', {
      body: { model: 'mia:default', ...request, stream: false },
    })
  }

  async models(): Promise<string[]> {
    const listed = await this.call<{ data: { id: string }[] }>('listAppModels')
    return listed.data.map((model) => model.id)
  }

  // -- runs (first-party tokens) ------------------------------------------------------------

  createRun(text: string, profile: 'chat' | 'voice' = 'chat', idempotencyKey?: string) {
    return this.call<Record<string, any>>('createRun', {
      body: { profile, input: { text } },
      idempotencyKey,
    })
  }

  cancelRun(runId: string) {
    return this.call<Record<string, any>>('cancelRun', { params: { run_id: runId } })
  }

  async *runEvents(runId: string): AsyncGenerator<Record<string, any>> {
    const response = await this.send('streamRunEvents', { params: { run_id: runId } })
    for await (const { event, data } of readSse(response.body!)) {
      yield JSON.parse(data) as Record<string, any>
      if (event === 'run.completed' || event === 'run.failed' || event === 'run.cancelled') return
    }
  }
}
