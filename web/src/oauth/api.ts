// Consent API of «Войти через Мию» (M50): GET/POST /v1/oauth/requests/{handle}.
// Same-origin cookie session (mia_session); this entry does not use src/lib/api.ts.

export type ScopeItem = {
  scope: string
  title: string
  description: string
  sensitive: boolean
  available: boolean
}

export type ConsentRequest = {
  client: {
    client_id: string
    name: string
    client_uri: string | null
    logo_uri: string | null
    redirect_host: string
  }
  scopes: ScopeItem[]
  memory_categories: string[]
  monthly_limit_usd: string
  max_monthly_limit_usd: string
  existing_grant: boolean
  expires_at: string
}

export type Decision = {
  decision: 'approve' | 'deny'
  scopes?: string[]
  memory_categories?: string[]
  monthly_limit_usd?: string
}

export class ConsentError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code)
  }
}

const base = () => import.meta.env.BASE_URL.replace(/\/$/, '')

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${base()}${path}`, {
      ...init,
      credentials: 'same-origin',
      headers: { Accept: 'application/json', ...init.headers },
    })
  } catch {
    throw new ConsentError(0, 'network')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new ConsentError(response.status, String(body?.error?.code ?? 'unknown'))
  }
  return (await response.json()) as T
}

export const consentApi = {
  load: (handle: string) =>
    call<ConsentRequest>(`/v1/oauth/requests/${encodeURIComponent(handle)}`),
  decide: (handle: string, decision: Decision, idempotencyKey: string) =>
    call<{ redirect_to: string }>(`/v1/oauth/requests/${encodeURIComponent(handle)}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(decision),
    }),
}

export const signInUrl = () =>
  `${base()}/?return_to=${encodeURIComponent(window.location.pathname + window.location.search)}`
