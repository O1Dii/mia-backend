import { http } from './http'

// ---------------------------------------------------------------- shared shapes (contracts/mvp/v1)
export type Risk = 'read' | 'write_reversible' | 'write_external'
export type Display =
  | { kind: 'text'; markdown: string }
  | { kind: 'card'; title?: string; markdown: string; source_tool_call_id?: string }
  | {
      kind: 'app'
      plugin_id: string
      resource_uri: string
      structured_content?: Record<string, unknown>
      fallback_markdown?: string
    }
  | { kind: 'link'; url: string; title: string }
export type AssistantResponse = { speech: string; display: Display | null; followups: string[] }

export type User = {
  id: string
  email: string
  display_name: string | null
  avatar_url: string | null
}

export type FeedItem = {
  id: string
  role: 'user' | 'assistant'
  source: string
  text: string
  display: Display | null
  run_id: string | null
  created_at: string
}
export type FeedPage = { items: FeedItem[]; next_cursor: string | null }

export type Run = {
  id: string
  profile: 'voice' | 'chat'
  status: 'running' | 'waiting_approval' | 'cancel_requested' | 'completed' | 'failed' | 'cancelled'
  response?: AssistantResponse
  error?: { code: string; message: string }
  conversation_id?: string
}

export type Approval = {
  id: string
  run_id: string
  tool_call_id: string
  plugin_id?: string
  tool: string
  risk: Risk
  summary: string
  arguments: Record<string, unknown>
  args_hash: string
  status: 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled'
  created_at: string
  expires_at: string
  decision: { decision: 'approve' | 'deny'; decided_at: string; channel: string } | null
}

// ------------------------------------------------------------------------------- store
export type Rating = { average: number | null; count: number }
export type CatalogEntry = {
  plugin_id: string
  version: string
  manifest: { server?: { kind?: string; auth?: string }; listing?: { author?: string } }
  name: string
  description: string
  category: string
  author: string | null
  kind: 'builtin' | 'remote' | 'declarative'
  tagline: string | null
  color: string | null
  art: string | null
  featured: boolean
  rating: Rating
  installs: number
  icon_url: string | null
  screenshots: string[]
  published_at: string
}
export type ConsentTool = { name: string; description: string; risk: Risk; voice: boolean }
export type Consent = {
  plugin_id: string
  version: string
  tools_digest: string
  tools: ConsentTool[]
  grants: { id: string; text: string }[]
  network: string[]
  public_web: boolean
  llm: { via_host: boolean; monthly_budget_usd: number | null }
  memory: string[]
  notify: boolean
  schedules: boolean
  device_tools: string[]
  requires_connection: boolean
  stores_state: boolean
}
export type CatalogDetail = CatalogEntry & { versions: string[]; consent: Consent }
export type Review = {
  id: string
  plugin_id: string
  rating: number
  text: string
  author: string
  mine: boolean
  updated_at: string
}
export type ReviewList = { items: Review[]; rating: Rating }

export type InstallationStatus = 'active' | 'needs_connection' | 'needs_consent' | 'disabled'
export type Installation = {
  id: string
  plugin_id: string
  version: string
  status: InstallationStatus
  custom_url: string | null
  source: 'catalog' | 'custom' | 'legacy_web'
  grants: string[]
  tools_digest: string | null
  state_version: number
  created_at: string
  updated_at: string
}
export type UpdateInfo = {
  installation_id: string
  plugin_id: string
  installed_version: string
  latest_version: string
  update_available: boolean
  added_grants: string[]
  removed_grants: string[]
  digest_changed: boolean
  consent: Consent | null
}
export type Discovery = {
  installation_id: string
  state: 'needs_connection' | 'ready'
  tools: { name: string; description: string; risk: Risk }[]
  tools_digest: string | null
  grants: { id: string; text: string }[]
  host: string
  skipped_tools: string[]
}
export type Connection = {
  id: string
  installation_id: string
  plugin_id: string
  status: 'pending' | 'connected' | 'expired' | 'revoked'
  account_label: string | null
  scopes: string | null
}

// ------------------------------------------------------------------- model connections
export type ModelProvider =
  'openai' | 'anthropic' | 'gemini' | 'deepinfra' | 'groq' | 'mistral' | 'ollama' | 'local'
export type ModelConnection = {
  id: string
  provider: string
  label: string
  model: string
  base_url: string | null
  capability: 'tools' | 'chat_only'
  has_key: boolean
  key_last4: string | null
  status: 'active' | 'paused' | 'needs_reauth'
  paused_until: string | null
  is_primary: boolean
  verified: boolean
  last_tested_at: string | null
  last_test_ok: boolean | null
  last_test_error_code: string | null
  created_at: string
}

const enc = encodeURIComponent

export const api = {
  // ---- account
  me: () => http<User>('GET', '/v1/me', { quiet401: true }),
  nonce: () => http<{ nonce: string }>('POST', '/v1/auth/web/nonce', { body: undefined }),
  googleLogin: (credential: string) =>
    http<{ user: User }>('POST', '/v1/auth/web/google', { body: { credential } }),
  logout: () => http<void>('POST', '/v1/auth/web/logout'),

  // ---- chat
  feed: (before?: string, limit = 30) =>
    http<FeedPage>('GET', '/v1/feed', { query: { before, limit } }),
  createRun: (text: string, key: string) =>
    http<Run>('POST', '/v1/runs', {
      body: { profile: 'chat', input: { text } },
      key,
    }),
  getRun: (id: string) => http<Run>('GET', `/v1/runs/${enc(id)}`),
  cancelRun: (id: string) => http<Run>('POST', `/v1/runs/${enc(id)}/cancel`),
  approvals: (status: Approval['status'] = 'pending') =>
    http<{ items: Approval[] }>('GET', '/v1/approvals', { query: { status, limit: 100 } }),
  decide: (approval: Approval, decision: 'approve' | 'deny') =>
    http<Approval>('POST', `/v1/approvals/${enc(approval.id)}/decision`, {
      body: { decision, args_hash: approval.args_hash },
      key: `${approval.id}-${decision}-${approval.args_hash.slice(-12)}`.padEnd(16, '0'),
    }),

  // ---- store
  plugins: (query?: string, category?: string, cursor?: string) =>
    http<{ items: CatalogEntry[]; next_cursor: string | null }>('GET', '/v1/plugins', {
      query: { query, category, cursor, limit: 50 },
    }),
  plugin: (id: string) => http<CatalogDetail>('GET', `/v1/plugins/${enc(id)}`),
  install: (id: string, consent: Consent) =>
    http<Installation>('POST', `/v1/plugins/${enc(id)}/install`, {
      body: {
        version: consent.version,
        tools_digest: consent.tools_digest,
        grants: consent.grants.map((g) => g.id),
      },
    }),
  reviews: (id: string) => http<ReviewList>('GET', `/v1/plugins/${enc(id)}/reviews`),
  putReview: (id: string, rating: number, text: string) =>
    http<void>('PUT', `/v1/plugins/${enc(id)}/reviews/me`, { body: { rating, text } }),
  deleteReview: (id: string) => http<void>('DELETE', `/v1/plugins/${enc(id)}/reviews/me`),
  installations: () => http<{ items: Installation[] }>('GET', '/v1/installations'),
  installation: (id: string) => http<Installation>('GET', `/v1/installations/${enc(id)}`),
  createCustom: (url: string) =>
    http<Installation>('POST', '/v1/installations', { body: { custom_mcp_url: url } }),
  discovery: (id: string) => http<Discovery>('GET', `/v1/installations/${enc(id)}/discovery`),
  consentCustom: (installation: Installation, discovery: Discovery) =>
    http<Installation>('POST', `/v1/installations/${enc(installation.id)}/consent`, {
      body: {
        expected_version: installation.state_version,
        tools_digest: discovery.tools_digest,
        grants: discovery.grants.map((g) => g.id),
      },
    }),
  uninstall: (id: string) => http<void>('DELETE', `/v1/installations/${enc(id)}`),
  updateInfo: (id: string) => http<UpdateInfo>('GET', `/v1/installations/${enc(id)}/update`),
  upgrade: (installation: Installation, consent: Consent) =>
    http<Installation>('POST', `/v1/installations/${enc(installation.id)}/upgrade`, {
      body: {
        expected_version: installation.state_version,
        version: consent.version,
        tools_digest: consent.tools_digest,
        grants: consent.grants.map((g) => g.id),
      },
    }),
  connections: () => http<{ items: Connection[] }>('GET', '/v1/connections'),
  startConnection: (installationId: string, returnTo: string) =>
    http<{ connection_id: string; authorization_url: string }>('POST', '/v1/connections', {
      body: { installation_id: installationId, return_to: returnTo },
    }),
  disconnect: (id: string) => http<void>('DELETE', `/v1/connections/${enc(id)}`),

  // ---- models
  models: () => http<{ items: ModelConnection[] }>('GET', '/v1/model-connections'),
  addModel: (input: {
    provider: ModelProvider
    label: string
    model: string
    api_key?: string
    base_url?: string
    capability?: 'tools' | 'chat_only'
    make_primary?: boolean
  }) => http<ModelConnection>('POST', '/v1/model-connections', { body: input }),
  patchModel: (id: string, input: { make_primary?: boolean; label?: string }) =>
    http<ModelConnection>('PATCH', `/v1/model-connections/${enc(id)}`, { body: input }),
  removeModel: (id: string) => http<void>('DELETE', `/v1/model-connections/${enc(id)}`),
  testModel: (id: string) =>
    http<{ ok: boolean; latency_ms: number; error_code: string | null }>(
      'POST',
      `/v1/model-connections/${enc(id)}/test`,
      { timeoutMs: 90000 },
    ),
  resumeModel: (id: string) =>
    http<ModelConnection>('POST', `/v1/model-connections/${enc(id)}/resume`),
}
