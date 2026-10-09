import { errorText } from './errors'

// Same origin and the same path prefix as the page (a build with MIA_BASE_PATH=/mia calls
// /mia/v1/...; the dev server proxies /v1 to the backend). Set VITE_API_BASE only when the API
// lives on another origin that allows credentialed requests.
const API_BASE = (import.meta.env.VITE_API_BASE ?? import.meta.env.BASE_URL).replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean
  readonly details: Record<string, unknown>
  constructor(
    status: number,
    code: string,
    retryable = false,
    details: Record<string, unknown> = {},
    message?: string,
  ) {
    super(message ?? errorText(code))
    this.status = status
    this.code = code
    this.retryable = retryable
    this.details = details
  }
}

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

export function newKey(): string {
  return crypto.randomUUID()
}

type Options = {
  body?: unknown
  query?: Record<string, string | number | undefined | null>
  signal?: AbortSignal
  key?: string
  timeoutMs?: number
  quiet401?: boolean
}

export async function http<T>(method: string, path: string, options: Options = {}): Promise<T> {
  const query = new URLSearchParams()
  for (const [name, value] of Object.entries(options.query ?? {}))
    if (value !== undefined && value !== null && value !== '') query.set(name, String(value))
  const url = apiUrl(path) + (query.size ? `?${query}` : '')
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET') headers['Idempotency-Key'] = options.key ?? newKey()
  const timeout = AbortSignal.timeout(options.timeoutMs ?? 30000)
  let response: Response
  try {
    response = await fetch(url, {
      method,
      headers,
      credentials: 'same-origin',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal ? AbortSignal.any([options.signal, timeout]) : timeout,
    })
  } catch (error) {
    if (options.signal?.aborted) throw error
    throw new ApiError(0, 'network_error', true)
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      error?: { code?: string; retryable?: boolean; details?: Record<string, unknown> }
    } | null
    const code = body?.error?.code ?? `http_${response.status}`
    if (response.status === 401 && !options.quiet401)
      window.dispatchEvent(new Event('mia:unauthorized'))
    throw new ApiError(response.status, code, body?.error?.retryable ?? false, body?.error?.details)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function messageOf(error: unknown, fallback = 'Не удалось выполнить действие.'): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error && error.name === 'AbortError') return fallback
  return fallback
}
