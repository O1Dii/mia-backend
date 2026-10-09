import { expect, test, type Page, type Route } from '@playwright/test'

// Consent page of «Войти через Мию» (M50) against a mocked /v1/oauth API.
const HANDLE = 'mia_rq_' + 'a'.repeat(43)
const REQUEST = {
  client: {
    client_id: 'https://notes.example/oauth/client.json',
    name: 'Notes',
    client_uri: 'https://notes.example',
    logo_uri: null,
    redirect_host: 'notes.example',
  },
  scopes: [
    {
      scope: 'openid',
      title: 'Узнать, что это вы',
      description: 'ID',
      sensitive: false,
      available: true,
    },
    {
      scope: 'llm.generate',
      title: 'Пользоваться вашей моделью',
      description: 'm',
      sensitive: false,
      available: true,
    },
    {
      scope: 'memory.context',
      title: 'Подмешивать контекст памяти',
      description: 'c',
      sensitive: false,
      available: true,
    },
    {
      scope: 'memory.read:work',
      title: 'Читать память: о работе',
      description: 'r',
      sensitive: true,
      available: true,
    },
    {
      scope: 'jobs',
      title: 'Фоновые задачи',
      description: 'j',
      sensitive: false,
      available: false,
    },
  ],
  memory_categories: ['profile', 'work', 'home', 'interests', 'other'],
  monthly_limit_usd: '5.00',
  max_monthly_limit_usd: '100.00',
  existing_grant: false,
  expires_at: '2030-01-01T00:00:00+00:00',
}

async function mock(page: Page, status = 200, body: unknown = REQUEST) {
  const decisions: { body: Record<string, unknown>; key: string | null }[] = []
  await page.route(`**/v1/oauth/requests/${HANDLE}`, (route: Route) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }),
  )
  await page.route(`**/v1/oauth/requests/${HANDLE}/decision`, async (route: Route) => {
    const request = route.request()
    decisions.push({
      body: request.postDataJSON(),
      key: request.headers()['idempotency-key'] ?? null,
    })
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ redirect_to: 'https://notes.example/callback?code=c&state=s' }),
    })
  })
  await page.route('https://notes.example/**', (route: Route) =>
    route.fulfill({ status: 200, contentType: 'text/html', body: '<p>back in Notes</p>' }),
  )
  return decisions
}

test('approve sends the chosen scopes, categories and limit, then returns to the app', async ({
  page,
}) => {
  const decisions = await mock(page)
  await page.goto(`/oauth.html?request=${HANDLE}`)
  await expect(page.getByTestId('client-name')).toHaveText('Notes')
  await expect(page.getByText('notes.example', { exact: true })).toBeVisible()
  await expect(page.locator('[data-scope="openid"]')).toBeDisabled()
  await expect(page.locator('[data-scope="jobs"]')).not.toBeChecked()
  await page.locator('[data-scope="memory.read:work"]').uncheck()
  await page.getByRole('button', { name: 'Дом' }).click()
  await page.getByTestId('monthly-limit').fill('2.5')
  await page.getByRole('button', { name: 'Разрешить' }).click()
  await expect(page).toHaveURL('https://notes.example/callback?code=c&state=s')
  expect(decisions).toHaveLength(1)
  expect(decisions[0].key).toBeTruthy()
  expect(decisions[0].body).toEqual({
    decision: 'approve',
    scopes: ['openid', 'llm.generate', 'memory.context'],
    memory_categories: ['profile', 'work', 'interests', 'other'],
    monthly_limit_usd: '2.5',
  })
})

test('deny sends only the refusal', async ({ page }) => {
  const decisions = await mock(page)
  await page.goto(`/oauth.html?request=${HANDLE}`)
  await page.getByRole('button', { name: 'Отказать' }).click()
  await expect(page).toHaveURL(/notes\.example\/callback/)
  expect(decisions[0].body).toEqual({ decision: 'deny' })
})

test('a signed-out user is asked to sign in to Mia first', async ({ page }) => {
  await mock(page, 401, { error: { code: 'authentication_required' } })
  await page.goto(`/oauth.html?request=${HANDLE}`)
  await expect(page.getByRole('heading', { name: 'Сначала войдите в Мию' })).toBeVisible()
  const href = await page.getByRole('link', { name: 'Войти в Мию' }).getAttribute('href')
  expect(href).toContain(encodeURIComponent(`/oauth.html?request=${HANDLE}`))
})

test('an expired request explains what to do', async ({ page }) => {
  await mock(page, 410, { error: { code: 'oauth_request_expired' } })
  await page.goto(`/oauth.html?request=${HANDLE}`)
  await expect(page.getByRole('alert')).toContainText('Запрос устарел')
})

test('the consent page refuses to work inside a frame', async ({ page }) => {
  const decisions = await mock(page)
  await page.goto('/oauth.html?request=none')
  await page.setContent(
    `<iframe src="/oauth.html?request=${HANDLE}" width="600" height="600"></iframe>`,
  )
  const frame = page.frameLocator('iframe')
  await expect(frame.getByRole('alert')).toContainText('нельзя открывать внутри другого сайта')
  await expect(frame.getByRole('button', { name: 'Разрешить' })).toHaveCount(0)
  expect(decisions).toHaveLength(0)
})
