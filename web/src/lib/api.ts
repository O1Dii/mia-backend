export type ProviderId =
  'openai' | 'anthropic' | 'gemini' | 'deepinfra' | 'groq' | 'mistral' | 'ollama' | 'local'
export type Connection = {
  id: string
  provider: ProviderId
  label: string
  model: string
  base_url: string
  has_key: boolean
  tool_mode: 'native' | 'json'
}
export type Message = { role: 'user' | 'assistant'; content: string }

export const webPath = (path: string) => `${import.meta.env.BASE_URL.replace(/\/$/, '')}${path}`

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(webPath(`/api/web${path}`), {
      ...init,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Mia-Web': '1', ...init.headers },
      signal: init.signal ?? AbortSignal.timeout(160000),
    })
  } catch {
    throw new Error('Локальный сервер недоступен или запрос истёк. Проверьте запуск web API.')
  }
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('mia:unauthorized'))
    const body = await response.json().catch(() => null)
    throw new Error(typeof body?.detail === 'string' ? body.detail : 'Не удалось выполнить запрос.')
  }
  return response.status === 204 ? (undefined as T) : response.json()
}

export const api = {
  auth: () => request<AuthSession>('/auth/session'),
  loginNonce: () => post<{ nonce: string }>('/auth/nonce', {}),
  googleLogin: (credential: string) => post<{ user: WebUser }>('/auth/google', { credential }),
  hubLogin: () => post<{ user: WebUser }>('/auth/hub', {}),
  logout: () => post<{ ok: boolean }>('/auth/logout', {}),
  reviews: (id: string) => request<Reviews>(`/plugins/${encodeURIComponent(id)}/reviews`),
  review: (id: string, rating: number, text: string) =>
    post<Reviews>(`/plugins/${encodeURIComponent(id)}/reviews`, { rating, text }),
  deleteReview: (id: string) =>
    request<Reviews>(`/plugins/${encodeURIComponent(id)}/reviews`, { method: 'DELETE' }),
  connections: () => request<Connection[]>('/connections'),
  add: (input: {
    provider: ProviderId
    label: string
    model: string
    api_key: string
    base_url?: string
    tool_mode?: 'native' | 'json'
  }) => request<Connection>('/connections', { method: 'POST', body: JSON.stringify(input) }),
  remove: (id: string) =>
    request<void>(`/connections/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  chat: (
    id: string,
    messages: Message[],
    options: { request_id?: string; use_tools?: boolean; plugin_id?: string } = {},
  ) =>
    request<ChatResult>('/chat', {
      method: 'POST',
      body: JSON.stringify({
        connection_id: id,
        messages,
        request_id: crypto.randomUUID(),
        ...options,
      }),
    }),
  workspace: () => request<WorkspaceSnapshot>('/workspace'),
  select: (connection_id: string) =>
    post<WorkspaceSnapshot>('/connections/select', { connection_id }),
  install: (plugin_id: string, manifest?: unknown) =>
    post<WorkspaceSnapshot>('/plugins/install', { plugin_id, manifest }),
  uninstall: (id: string) =>
    request<WorkspaceSnapshot>(`/plugins/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  action: (name: string, args: Record<string, unknown>, request_id = crypto.randomUUID()) =>
    post<ActionResult & { workspace: WorkspaceSnapshot }>('/actions', {
      name,
      arguments: args,
      request_id,
    }),
  layout: (widgets: WidgetLayout[]) => post<WorkspaceSnapshot>('/dashboard', { widgets }),
  digestSettings: (topics: string, interval_minutes: number) =>
    post<WorkspaceSnapshot>('/digest/settings', { topics, interval_minutes }),
  apps: () => request<{ id: string; plugin_id: string; expires_in: number }[]>('/apps'),
  inspectApp: (user_code: string) =>
    post<{ plugin_id: string; name: string; permissions: string[]; subject_id: string }>(
      '/apps/inspect',
      { user_code },
    ),
  approveApp: (user_code: string) => post<{ id: string }>('/apps/approve', { user_code }),
  revokeApp: (id: string) => request<void>(`/apps/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}

export const providerOptions: {
  id: ProviderId
  name: string
  mark: string
  color: string
  model: string
  local?: boolean
  url?: string
}[] = [
  { id: 'openai', name: 'OpenAI', mark: '◎', color: 'mint', model: 'gpt-4.1-mini' },
  { id: 'anthropic', name: 'Anthropic', mark: '✳', color: 'peach', model: 'claude-sonnet-4-5' },
  { id: 'gemini', name: 'Google Gemini', mark: '✦', color: 'blue', model: 'gemini-2.5-flash' },
  {
    id: 'deepinfra',
    name: 'DeepInfra',
    mark: '◈',
    color: 'violet',
    model: 'zai-org/GLM-5.3-Flash',
  },
  { id: 'groq', name: 'Groq', mark: '↯', color: 'peach', model: 'llama-3.3-70b-versatile' },
  { id: 'mistral', name: 'Mistral', mark: '▧', color: 'yellow', model: 'mistral-small-latest' },
  {
    id: 'ollama',
    name: 'Ollama',
    mark: '◉',
    color: 'mint',
    model: 'llama3.2',
    local: true,
    url: 'http://127.0.0.1:11434',
  },
  {
    id: 'local',
    name: 'LM Studio / local',
    mark: '⌘',
    color: 'blue',
    model: 'local-model',
    local: true,
    url: 'http://127.0.0.1:1234/v1',
  },
]

function post<T>(path: string, body: unknown) {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body) })
}
export type ActionResult = {
  name?: string
  ok: boolean
  plugin_id?: string
  data?: Record<string, unknown>
  error?: string
}
export type ChatResult = {
  text: string
  model: string
  actions?: ActionResult[]
  warning?: string | null
}
export type WidgetLayout = { id: string; plugin_id: string; size: 'small' | 'medium' | 'wide' }
export type PluginState = {
  deadline?: number | null
  remaining?: number
  label?: string
  status?: string
  zones?: string[]
  notes?: { id: string; text: string; done: boolean }[]
  topics?: string
  interval_minutes?: number
  summary?: string
  model?: string
  items?: { title: string; url: string; source: string; published_at: string; snippet: string }[]
  updated_at?: number
  error?: string | null
  query?: string
  answer?: string
  [field: string]: unknown
}
export type WorkspaceSnapshot = {
  subject_id: string
  revision: number
  installed: string[]
  manifests: Record<string, import('../plugins/catalog').ApplicationManifest>
  states: Record<string, PluginState>
  layout: WidgetLayout[]
  active_connection_id: string
  audit: { id: string; name: string; plugin_id: string; ok: boolean; at: number }[]
  server_time: number
}

export type WebUser = { id: string; name: string; email: string }
export type AuthSession = {
  user: WebUser | null
  required: boolean
  google_client_id: string
  google_hub: boolean
  login_url: string
}
export type Reviews = {
  count: number
  average: number | null
  items: { author: string; rating: number; text: string; updated_at: number; mine: boolean }[]
}
