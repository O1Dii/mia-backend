// «Войти через Мию» for JS apps (SPA, mobile, server): authorization code + PKCE S256.
// Public client identified by a Client ID Metadata Document URL; no client secret.

export type Tokens = {
  access_token: string
  refresh_token: string
  expires_in: number
  scope: string
  token_type: 'Bearer'
  id_token?: string
}

export type SignInStart = { url: string; state: string; verifier: string; nonce?: string }

export class MiaOAuthError extends Error {
  readonly error: string
  readonly description: string

  constructor(error: string, description = '') {
    super(description ? `${error}: ${description}` : error)
    this.error = error
    this.description = description
  }
}

type Fetch = typeof fetch

function base64url(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function randomToken(size: number): string {
  return base64url(crypto.getRandomValues(new Uint8Array(size)))
}

/** RFC 7636 S256: 64 random bytes as the verifier, base64url(SHA-256) as the challenge. */
export async function createPkce(): Promise<{ verifier: string; challenge: string }> {
  const verifier = randomToken(64)
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return { verifier, challenge: base64url(new Uint8Array(digest)) }
}

function sameString(a: string, b: string): boolean {
  if (a.length !== b.length) return false
  let diff = 0
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i)
  return diff === 0
}

export type MiaOAuthOptions = {
  issuer: string
  clientId: string
  redirectUri: string
  fetch?: Fetch
}

export class MiaOAuth {
  readonly issuer: string
  readonly clientId: string
  readonly redirectUri: string
  private readonly fetcher: Fetch

  constructor(options: MiaOAuthOptions) {
    this.issuer = options.issuer.replace(/\/$/, '')
    this.clientId = options.clientId
    this.redirectUri = options.redirectUri
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis)
  }

  /** Build the authorize URL; keep `state` and `verifier` (e.g. in sessionStorage). */
  async start(options: {
    scope: string
    resource?: string
    nonce?: boolean
  }): Promise<SignInStart> {
    const { verifier, challenge } = await createPkce()
    const state = randomToken(32)
    const params = new URLSearchParams({
      response_type: 'code',
      client_id: this.clientId,
      redirect_uri: this.redirectUri,
      scope: options.scope,
      state,
      code_challenge: challenge,
      code_challenge_method: 'S256',
    })
    if (options.resource) params.set('resource', options.resource)
    let nonce: string | undefined
    if (options.nonce !== false && options.scope.split(' ').includes('openid')) {
      nonce = randomToken(16)
      params.set('nonce', nonce)
    }
    return { url: `${this.issuer}/oauth/authorize?${params}`, state, verifier, nonce }
  }

  /** Check `state` and `iss` (RFC 9207) on the callback URL, then redeem the code. */
  async finish(
    callbackUrl: string,
    expected: { state: string; verifier: string },
  ): Promise<Tokens> {
    const query = new URL(callbackUrl).searchParams
    if (!sameString(query.get('state') ?? '', expected.state)) {
      throw new MiaOAuthError('invalid_state', 'The state does not match this sign-in.')
    }
    const iss = query.get('iss')
    if (iss !== null && iss !== this.issuer) {
      throw new MiaOAuthError('invalid_issuer', 'The response came from another issuer.')
    }
    const error = query.get('error')
    if (error) throw new MiaOAuthError(error, query.get('error_description') ?? '')
    const code = query.get('code')
    if (!code) throw new MiaOAuthError('invalid_request', 'The callback has no code.')
    return this.exchangeCode(code, expected.verifier)
  }

  private async token(form: Record<string, string>): Promise<Tokens> {
    const response = await this.fetcher(`${this.issuer}/oauth/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(form),
    })
    const body = (await response.json()) as Record<string, unknown>
    if (!response.ok) {
      throw new MiaOAuthError(String(body.error), String(body.error_description ?? ''))
    }
    return body as Tokens
  }

  exchangeCode(code: string, verifier: string): Promise<Tokens> {
    return this.token({
      grant_type: 'authorization_code',
      client_id: this.clientId,
      code,
      code_verifier: verifier,
      redirect_uri: this.redirectUri,
    })
  }

  /** Refresh tokens rotate: the old one stops working, store the new one at once. */
  refresh(refreshToken: string, scope?: string): Promise<Tokens> {
    const form: Record<string, string> = {
      grant_type: 'refresh_token',
      client_id: this.clientId,
      refresh_token: refreshToken,
    }
    if (scope) form.scope = scope
    return this.token(form)
  }

  async revoke(token: string): Promise<void> {
    await this.fetcher(`${this.issuer}/oauth/revoke`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ token, client_id: this.clientId }),
    })
  }

  async userinfo(accessToken: string): Promise<Record<string, unknown>> {
    const response = await this.fetcher(`${this.issuer}/oauth/userinfo`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    if (!response.ok) throw new MiaOAuthError('invalid_token', 'The access token was rejected.')
    return (await response.json()) as Record<string, unknown>
  }
}
