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
export type TileSize = '1x1' | '2x1' | '2x2' | '4x2' | '4x4'
export type ManifestTile = {
  id: string
  title?: string
  resourceUri?: string
  sizes: TileSize[]
  mobile?: 'card' | 'tile' | 'hidden'
}
export type CatalogEntry = {
  plugin_id: string
  version: string
  manifest: {
    server?: { kind?: string; auth?: string }
    listing?: { author?: string }
    surfaces?: { tiles?: ManifestTile[] }
  }
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

export type ChatGptSignIn = {
  attempt_id: string
  authorize_url: string
  expires_at: string
  /** The backend listens on 127.0.0.1 itself (local install); otherwise the address is pasted. */
  listening?: boolean
}
export type ChatGptSignInStatus = {
  attempt_id: string
  status: 'pending' | 'completed' | 'failed'
  error_code: string | null
  connection_id: string | null
}

/** `GET /downloads/mia.json` (M62-T1): the Android build this server offers. */
export type AndroidApp = {
  url: string
  size_bytes: number
  updated_at: string
  version_name: string | null
  version_code: number | null
}

// ------------------------------------------------------------------------------ dashboard
export type TilePlacement = { size: TileSize; position: number }
export type DashboardTile = {
  id: string
  installation_id: string
  tile_id: string
  size: TileSize
  position: number
  mobile: TilePlacement
  settings: Record<string, unknown>
  state_version: number
}
export type DashboardTileInput = Omit<DashboardTile, 'state_version'>
export type Dashboard = { expected_version: number; tiles: DashboardTile[] }
export type TileDocument = {
  installation_id: string
  plugin_id: string
  tile_id: string
  title: string | null
  resource_uri: string
  html: string
  csp: string
  sizes: TileSize[]
  mobile: 'card' | 'tile' | 'hidden'
  refresh: { tool: string; every_seconds: number } | null
  tools: string[]
}
export type TileToolResult = {
  status: string
  is_error: boolean
  content: string
  structured_content: Record<string, unknown> | null
  error_code: string | null
}

// --------------------------------------------------------------------------------- memory
export type MemoryCategory =
  'profile' | 'work' | 'home' | 'health' | 'finance' | 'interests' | 'other'
export type RecordStatus = 'suggested' | 'active' | 'rejected'
export type MemorySource = { type: string; ref?: string }
export type Fact = {
  kind: 'fact'
  id: string
  text: string
  category: MemoryCategory
  source: MemorySource
  status: RecordStatus
  sensitivity: 'normal' | 'sensitive'
  scope: 'user' | 'automation'
  created_at: string
  updated_at: string
  last_used_at: string | null
}
export type Person = {
  kind: 'person'
  id: string
  display_name: string
  aliases: string[]
  relation?: string
  identifiers: Record<string, string | number>
  trusted?: boolean
  source: MemorySource
  status: RecordStatus
  sensitivity: 'normal' | 'sensitive'
  created_at: string
  updated_at: string
  last_used_at: string | null
}
export type Preference = {
  kind: 'preference'
  id: string
  key: string
  value: string | number | boolean
  source: MemorySource
  created_at: string
  updated_at: string
}
export type Retention = '30d' | '1y' | 'forever' | 'none'
export type MemorySettings = { autosave_categories: MemoryCategory[]; history_retention: Retention }
export type MemoryAccess = {
  at: string
  actor: 'run' | 'user' | 'app' | 'system'
  action: string
  record_count: number
  run_id?: string
  app_client_id?: string
}

// --------------------------------------------------------------------- background and inbox
export type JobStatus =
  'queued' | 'running' | 'waiting_approval' | 'succeeded' | 'failed' | 'cancelled'
export type Job = {
  id: string
  goal: string
  status: JobStatus
  origin: { type: 'run' | 'schedule' | 'app' | 'user'; id: string }
  run_id?: string | null
  attempt: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  result: AssistantResponse | null
  error: { code: string; message: string } | null
  kind?: 'run' | 'reminder'
}
export type Schedule = {
  id: string
  rule: string
  timezone: string
  goal: string
  delivery: { channels: string[] }
  misfire_policy: string
  status: 'active' | 'paused'
  next_run_at: string | null
  last_run_at: string | null
  created_at: string
  kind?: 'run' | 'reminder'
}
export type InboxItem = {
  id: string
  source: { type: 'job' | 'schedule' | 'plugin' | 'system'; id: string }
  title: string
  response: AssistantResponse
  created_at: string
  read_at: string | null
}
export type ConnectedApp = {
  id: string
  client_id: string
  client_name: string
  client_uri?: string | null
  scopes: string[]
  memory_categories: string[]
  monthly_limit_usd: string
  month_spend_usd?: string
  created_at: string
  updated_at: string
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
    http<{ connection_id: string; authorization_url?: string; status?: 'connected' }>(
      'POST',
      '/v1/connections',
      {
        body: { installation_id: installationId, return_to: returnTo },
      },
    ),
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
  chatgptSignIn: () => http<ChatGptSignIn>('POST', '/v1/model-connections/chatgpt-plan/sign-in'),
  chatgptSignInStatus: (attemptId: string) =>
    http<ChatGptSignInStatus>(
      'GET',
      `/v1/model-connections/chatgpt-plan/sign-in/${enc(attemptId)}`,
    ),
  chatgptSignInCallback: (attemptId: string, callbackUrl: string) =>
    http<ChatGptSignInStatus>(
      'POST',
      `/v1/model-connections/chatgpt-plan/sign-in/${enc(attemptId)}/callback`,
      { body: { callback_url: callbackUrl }, timeoutMs: 45000 },
    ),

  // ---- Android app download (public, no session)
  androidApp: () => http<AndroidApp>('GET', '/downloads/mia.json', { quiet401: true }),

  // ---- dashboard (MCP Apps tiles)
  dashboard: () => http<Dashboard>('GET', '/v1/dashboard'),
  putDashboard: (dashboard: { expected_version: number; tiles: DashboardTileInput[] }) =>
    http<Dashboard>('PUT', '/v1/dashboard', { body: dashboard }),
  tileDocument: (installationId: string, tileId: string) =>
    http<TileDocument>('GET', `/v1/installations/${enc(installationId)}/tiles/${enc(tileId)}`),
  tileCall: (installationId: string, tileId: string, name: string, args: Record<string, unknown>) =>
    http<TileToolResult>(
      'POST',
      `/v1/installations/${enc(installationId)}/tiles/${enc(tileId)}/tool-calls`,
      { body: { name, arguments: args }, timeoutMs: 70000 },
    ),

  // ---- memory
  facts: (status: RecordStatus, cursor?: string) =>
    http<{ items: Fact[]; next_cursor: string | null }>('GET', '/v1/memory/facts', {
      query: { status, cursor, limit: 100 },
    }),
  createFact: (text: string, category: MemoryCategory) =>
    http<Fact>('POST', '/v1/memory/facts', { body: { text, category } }),
  patchFact: (
    id: string,
    patch: Partial<Pick<Fact, 'text' | 'category'>> & { status?: 'active' | 'rejected' },
  ) => http<Fact>('PATCH', `/v1/memory/facts/${enc(id)}`, { body: patch }),
  deleteFact: (id: string) => http<void>('DELETE', `/v1/memory/facts/${enc(id)}`),
  people: (status: RecordStatus) =>
    http<{ items: Person[] }>('GET', '/v1/memory/people', { query: { status } }),
  createPerson: (input: { display_name: string; relation?: string; aliases?: string[] }) =>
    http<Person>('POST', '/v1/memory/people', { body: input }),
  patchPerson: (
    id: string,
    patch: {
      display_name?: string
      relation?: string
      aliases?: string[]
      trusted?: boolean
      status?: 'active' | 'rejected'
    },
  ) => http<Person>('PATCH', `/v1/memory/people/${enc(id)}`, { body: patch }),
  deletePerson: (id: string) => http<void>('DELETE', `/v1/memory/people/${enc(id)}`),
  preferences: () => http<{ items: Preference[] }>('GET', '/v1/memory/preferences'),
  putPreference: (key: string, value: string | number | boolean) =>
    http<Preference>('PUT', `/v1/memory/preferences/${enc(key)}`, { body: { value } }),
  memorySettings: () => http<MemorySettings>('GET', '/v1/memory/settings'),
  putMemorySettings: (settings: Partial<MemorySettings>) =>
    http<MemorySettings>('PUT', '/v1/memory/settings', { body: settings }),
  memoryAccess: (limit = 30) =>
    http<{ items: MemoryAccess[] }>('GET', '/v1/memory/access-log', { query: { limit } }),
  importMemory: (format: 'markdown' | 'text' | 'json', source: string, content: string) =>
    http<{ batch_id: string; facts: number; people: number }>('POST', '/v1/memory/import', {
      body: { format, source, content },
    }),
  forgetMemory: () => http<void>('DELETE', '/v1/memory'),

  // ---- background tasks and inbox
  jobs: () =>
    http<{ items: Job[]; next_cursor: string | null }>('GET', '/v1/jobs', { query: { limit: 50 } }),
  createJob: (goal: string) =>
    http<Job>('POST', '/v1/jobs', { body: { goal, delivery: { channels: ['inbox'] } } }),
  cancelJob: (id: string) => http<Job>('POST', `/v1/jobs/${enc(id)}/cancel`),
  schedules: () => http<{ items: Schedule[] }>('GET', '/v1/schedules'),
  patchSchedule: (id: string, patch: { status?: 'active' | 'paused' }) =>
    http<Schedule>('PATCH', `/v1/schedules/${enc(id)}`, { body: patch }),
  runSchedule: (id: string) => http<Job>('POST', `/v1/schedules/${enc(id)}/run`),
  deleteSchedule: (id: string) => http<void>('DELETE', `/v1/schedules/${enc(id)}`),
  inbox: (unread?: boolean, cursor?: string) =>
    http<{ items: InboxItem[]; next_cursor: string | null }>('GET', '/v1/inbox', {
      query: { unread: unread ? 'true' : undefined, cursor, limit: 50 },
    }),
  markRead: (id: string) => http<void>('POST', `/v1/inbox/${enc(id)}/read`),

  // ---- connected apps (M50)
  apps: () => http<{ items: ConnectedApp[] }>('GET', '/v1/apps'),
  revokeApp: (grantId: string) => http<void>('DELETE', `/v1/apps/${enc(grantId)}`),
}
