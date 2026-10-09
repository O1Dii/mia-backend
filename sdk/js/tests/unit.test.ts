import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { test } from 'node:test'
import {
  MiaClient,
  MiaOAuth,
  MiaOAuthError,
  OPERATIONS,
  createPkce,
  readSse,
} from '../src/index.ts'

test('PKCE is S256 over a 43+ character verifier', async () => {
  const { verifier, challenge } = await createPkce()
  assert.match(verifier, /^[A-Za-z0-9_-]{43,128}$/)
  const expected = createHash('sha256').update(verifier).digest('base64url')
  assert.equal(challenge, expected)
})

test('start builds an authorize URL with S256, state and nonce', async () => {
  const oauth = new MiaOAuth({
    issuer: 'https://mia.test/',
    clientId: 'https://notes.example/oauth/client.json',
    redirectUri: 'https://notes.example/callback',
  })
  const start = await oauth.start({
    scope: 'openid llm.generate',
    resource: 'https://mia.test/mcp',
  })
  const url = new URL(start.url)
  assert.equal(url.origin + url.pathname, 'https://mia.test/oauth/authorize')
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256')
  assert.equal(url.searchParams.get('state'), start.state)
  assert.equal(url.searchParams.get('nonce'), start.nonce)
  assert.equal(url.searchParams.get('resource'), 'https://mia.test/mcp')
})

test('finish refuses a foreign state or issuer before any request', async () => {
  let calls = 0
  const oauth = new MiaOAuth({
    issuer: 'https://mia.test',
    clientId: 'https://notes.example/c.json',
    redirectUri: 'https://notes.example/cb',
    fetch: async () => {
      calls += 1
      return new Response('{}')
    },
  })
  await assert.rejects(
    oauth.finish('https://notes.example/cb?code=x&state=evil', { state: 'good', verifier: 'v' }),
    (error: unknown) => error instanceof MiaOAuthError && error.error === 'invalid_state',
  )
  await assert.rejects(
    oauth.finish('https://notes.example/cb?code=x&state=good&iss=https://evil.test', {
      state: 'good',
      verifier: 'v',
    }),
    (error: unknown) => error instanceof MiaOAuthError && error.error === 'invalid_issuer',
  )
  assert.equal(calls, 0)
})

test('SSE parser handles split chunks, comments and multi-line data', async () => {
  const text = ': keepalive\n\nevent: a\ndata: {"x":\ndata: 1}\n\ndata: [DONE]\n\n'
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const part of [text.slice(0, 7), text.slice(7, 30), text.slice(30)]) {
        controller.enqueue(encoder.encode(part))
      }
      controller.close()
    },
  })
  const events = []
  for await (const event of readSse(body)) events.push(event)
  assert.deepEqual(events, [
    { event: 'a', data: '{"x":\n1}' },
    { event: null, data: '[DONE]' },
  ])
})

test('the operation table comes from the contract and drives idempotency', async () => {
  assert.equal(OPERATIONS.createChatCompletion.path, '/v1/chat/completions')
  assert.equal(OPERATIONS.createChatCompletion.idempotencyKey, false)
  assert.equal(OPERATIONS.createRun.idempotencyKey, true)
  const seen: Headers[] = []
  const client = new MiaClient({
    baseUrl: 'https://mia.test',
    accessToken: () => 'tok',
    fetch: async (_url, init) => {
      seen.push(new Headers(init?.headers))
      return new Response(JSON.stringify({ id: 'r' }), { status: 201 })
    },
  })
  await client.createRun('hi')
  assert.equal(seen[0].get('authorization'), 'Bearer tok')
  assert.ok(seen[0].get('idempotency-key'))
})
