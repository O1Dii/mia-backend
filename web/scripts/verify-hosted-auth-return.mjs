// Anonymous live regression: a real cross-site browser navigation back to MIA.
// No Google account, session token, API key or paid model call is used.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { chromium } from '@playwright/test'

const origin = process.env.MIA_SMOKE_ORIGIN ?? 'https://mindmapgen.xyz'
const landing = `${origin}/mia/?signin=google`
const external = createServer((request, response) => {
  if (request.url === '/redirect') {
    response.writeHead(303, { Location: landing }).end()
  } else {
    response.writeHead(200, { 'Content-Type': 'text/html' })
    response.end('<a href="/redirect">Return from identity provider</a>')
  }
})
await new Promise((resolve) => external.listen(0, '127.0.0.1', resolve))
const browser = await chromium.launch()
try {
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto(`http://127.0.0.1:${external.address().port}/`)
  const responsePromise = page.waitForResponse(
    (response) => response.url() === landing && response.request().isNavigationRequest(),
  )
  await page.getByRole('link', { name: 'Return from identity provider' }).click()
  const response = await responsePromise
  const headers = await response.request().allHeaders()
  assert.equal(headers['sec-fetch-site'], 'cross-site')
  assert.equal(headers['sec-fetch-mode'], 'navigate')
  assert.equal(headers['sec-fetch-dest'], 'document')
  assert.equal(response.status(), 200)
  await page.getByRole('link', { name: 'Продолжить с Google' }).waitFor()
  assert.equal(errors.length, 0, errors.join('\n'))
  // This anonymous browser has no real hub cookie: do not invent a signed-in user.
  const auth = await (await page.request.get(`${origin}/mia/api/web/auth/session`)).json()
  assert(auth.required && auth.google_hub && auth.user === null)
  assert.equal((await page.request.get(`${origin}/mia/api/web/workspace`)).status(), 401)
  const blocked = await page.request.get(`${origin}/mia/api/web/auth/session`, {
    headers: {
      'Sec-Fetch-Site': 'cross-site',
      'Sec-Fetch-Mode': 'navigate',
      'Sec-Fetch-Dest': 'document',
    },
  })
  assert.equal(blocked.status(), 403)
  console.log(
    'Hosted auth return PASS: real cross-site page navigation 200; login UI loads; anonymous workspace 401; cross-site API 403',
  )
} finally {
  await browser.close()
  external.closeAllConnections()
  await new Promise((resolve) => external.close(resolve))
}
