// End-to-end run of @mia/sdk against a live Mia (driven by tests/oauth/test_sdk_postgres.py).
// The consent step uses the test first-party principal (x-test-user header).
import assert from 'node:assert/strict'
import { MiaClient, MiaOAuth } from '../src/index.ts'

const env = (name: string) => {
  const value = process.env[name]
  if (!value) throw new Error(`${name} is required`)
  return value
}
const base = env('MIA_BASE_URL')
const issuer = env('MIA_ISSUER')
const user = env('MIA_TEST_USER')
const live: typeof fetch = (input, init) =>
  fetch(String(input instanceof Request ? input.url : input).replace(issuer, base), init)

const oauth = new MiaOAuth({
  issuer,
  clientId: env('MIA_CLIENT_ID'),
  redirectUri: env('MIA_REDIRECT_URI'),
  fetch: live,
})
const start = await oauth.start({ scope: 'openid llm.generate' })
const authorize = await live(start.url, { redirect: 'manual' })
assert.equal(authorize.status, 302)
const handle = new URL(authorize.headers.get('location')!).searchParams.get('request')!
const headers = { 'x-test-user': user }
assert.equal((await live(`${base}/v1/oauth/requests/${handle}`, { headers })).status, 200)
const decided = await live(`${base}/v1/oauth/requests/${handle}/decision`, {
  method: 'POST',
  headers: {
    ...headers,
    'Content-Type': 'application/json',
    'Idempotency-Key': crypto.randomUUID(),
  },
  body: JSON.stringify({ decision: 'approve' }),
})
const { redirect_to } = (await decided.json()) as { redirect_to: string }
const tokens = await oauth.finish(redirect_to, start)
assert.equal(tokens.scope, 'openid llm.generate')
assert.ok(tokens.id_token)

const mia = new MiaClient({ baseUrl: base, accessToken: tokens.access_token })
assert.deepEqual(await mia.models(), ['mia:default'])
const reply = await mia.chatCompletion({ messages: [{ role: 'user', content: 'Привет' }] })
assert.equal(reply.choices[0].message.content, env('MIA_EXPECT_1'))
let streamed = ''
for await (const chunk of mia.streamChatCompletion({
  messages: [{ role: 'user', content: 'Ещё' }],
})) {
  streamed += chunk.choices[0]?.delta?.content ?? ''
}
assert.equal(streamed, env('MIA_EXPECT_2'))

const rotated = await oauth.refresh(tokens.refresh_token)
assert.notEqual(rotated.refresh_token, tokens.refresh_token)
await oauth.revoke(rotated.refresh_token)
await assert.rejects(oauth.userinfo(rotated.access_token))
console.log('E2E-OK')
