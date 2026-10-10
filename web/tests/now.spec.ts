import { expect, test, type Page } from '@playwright/test'
import { callTile, installBuiltin, installationId, signIn, world } from './support/helpers'

// M75: the «Сейчас» strip: timers count down in the browser, cancel works without the model.
const strip = (page: Page) => page.getByRole('region', { name: 'Сейчас' })

async function startTimer(page: Page, seconds: number, label: string) {
  await installBuiltin(page, 'builtin.timer')
  const id = await installationId(page, 'builtin.timer')
  const started = await callTile(page, id, 'active-timers', 'start', { seconds, label })
  expect(started.status).toBe(200)
}

test('a timer counts down live and is cancelled from the strip without the model', async ({
  page,
}) => {
  await signIn(page)
  await startTimer(page, 120, 'Чай')
  const modelCalls = (await world(page)).model_calls
  await page.goto('/chat')
  const item = strip(page).locator('.mw-now__item', { hasText: 'Чай' })
  await expect(item).toBeVisible()
  const timer = item.getByRole('timer')
  const seconds = async () => {
    const [m, s] = (await timer.innerText()).split(':').map(Number)
    return m * 60 + s
  }
  const first = await seconds()
  expect(first).toBeLessThanOrEqual(120)
  expect(first).toBeGreaterThan(100)
  await expect.poll(seconds, { timeout: 5000 }).toBeLessThan(first)

  await item.getByRole('button', { name: 'Отменить: Чай' }).click()
  await expect(strip(page)).toHaveCount(0)
  const now = (await (await page.request.get('/v1/now')).json()) as { items: unknown[] }
  expect(now.items).toEqual([])
  expect((await world(page)).model_calls).toBe(modelCalls)
  await page.reload()
  await expect(strip(page)).toHaveCount(0)
})

test('a timer that runs out leaves the strip by itself', async ({ page }) => {
  await signIn(page)
  await startTimer(page, 3, 'Скоро')
  await page.goto('/chat')
  await expect(strip(page).getByText('Скоро')).toBeVisible()
  await expect(strip(page)).toHaveCount(0, { timeout: 10_000 })
})

test('the strip is shared by all chats and refreshes when the tab gets focus', async ({ page }) => {
  await signIn(page)
  await page.goto('/chat')
  await expect(strip(page)).toHaveCount(0)
  await startTimer(page, 300, 'Из другого окна')
  await page.goto('/chat')
  await expect(strip(page).getByText('Из другого окна')).toBeVisible()
  await page
    .getByRole('complementary', { name: 'Чаты' })
    .getByRole('button', { name: 'Новый чат', exact: true })
    .click()
  await expect(strip(page).getByText('Из другого окна')).toBeVisible()
  const id = await installationId(page, 'builtin.timer')
  await callTile(page, id, 'active-timers', 'start', { seconds: 400, label: 'Второй' })
  await expect(strip(page).getByText('Второй')).toHaveCount(0)
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(strip(page).getByText('Второй')).toBeVisible()
})

test('a background task shows its state and is cancelled through the jobs API', async ({
  page,
}) => {
  await signIn(page)
  const jobId = '7d1b6d62-3c5a-4d4e-9b6e-0f2b6f3c1a11'
  let cancelled = 0
  const item = {
    kind: 'job',
    id: jobId,
    label: 'Собрать отчёт по рынку',
    state: 'running',
    deadline_at: null,
    remaining_ms: null,
    duration_ms: null,
    started_at: new Date().toISOString(),
    plugin_id: null,
    installation_id: null,
    job_id: jobId,
    run_id: null,
    conversation_id: null,
    cancel: { type: 'job', job_id: jobId },
  }
  await page.route('**/v1/now', (route) =>
    route.fulfill({
      json: { server_time: new Date().toISOString(), items: cancelled ? [] : [item] },
    }),
  )
  await page.route(`**/v1/jobs/${jobId}/cancel`, (route) => {
    cancelled += 1
    return route.fulfill({ json: { id: jobId, status: 'cancelled' } })
  })
  await page.goto('/chat')
  const card = strip(page).locator('.mw-now__item')
  await expect(card).toContainText('Собрать отчёт по рынку')
  await expect(card).toContainText('выполняется')
  await card.getByRole('button', { name: 'Отменить: Собрать отчёт по рынку' }).click()
  await expect(strip(page)).toHaveCount(0)
  expect(cancelled).toBe(1)
})

test('a failed cancel keeps the item and says so', async ({ page }) => {
  await signIn(page)
  await startTimer(page, 200, 'Упрямый')
  await page.goto('/chat')
  await page.route('**/tool-calls', (route) =>
    route.fulfill({ status: 503, json: { error: { code: 'service_unavailable', message: 'x' } } }),
  )
  await strip(page).getByRole('button', { name: 'Отменить: Упрямый' }).click()
  await expect(strip(page).getByRole('alert')).toBeVisible()
  await expect(strip(page).getByText('Упрямый')).toBeVisible()
})
