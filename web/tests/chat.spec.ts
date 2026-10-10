import { expect, test, type Page } from '@playwright/test'
import { fakeSpeech, installTracker, newSubject, noRecognition, signIn } from './support/helpers'

const field = (page: Page) => page.getByRole('textbox', { name: 'Сообщение Мии' })
const ANSWER = 'Привет! Я Мия, это тестовый ответ.'

test.describe('chat on runs', () => {
  test('the first run streams into a reply with markdown, a followup chip and no stray state', async ({
    page,
  }) => {
    await signIn(page)
    await page.goto('/chat')
    await expect(page.getByRole('heading', { name: 'С чего начнём?' })).toBeVisible()
    await field(page).fill('Привет!')
    await field(page).press('Enter')
    await expect(page.getByText('Мия думает…')).toBeVisible()
    const reply = page.locator('[data-role="assistant"]').last()
    await expect(reply.getByText('Привет! Я', { exact: false })).toBeVisible()
    await expect(reply.locator('strong', { hasText: 'Мия' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Расскажи ещё' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Отправить сообщение' })).toBeDisabled()

    // A followup chip sends itself.
    await page.getByRole('button', { name: 'Расскажи ещё' }).click()
    await expect(page.locator('[data-role="user"]')).toHaveCount(2)
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(2)
  })

  test('history comes from the feed after a reload, newest at the bottom, card included', async ({
    page,
  }) => {
    await signIn(page)
    await page.goto('/chat')
    await field(page).fill('Покажи карточку на сегодня')
    await field(page).press('Enter')
    await expect(page.getByRole('heading', { name: 'Сегодня', level: 3 })).toBeVisible()
    await field(page).fill('Привет!')
    await field(page).press('Enter')
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(2)

    // The feed entry of the last answer is written when the run completes.
    await expect
      .poll(
        async () =>
          ((await (await page.request.get('/v1/feed')).json()) as { items: unknown[] }).items
            .length,
      )
      .toBe(4)
    await page.reload()
    const bubbles = page.locator('[data-role]')
    await expect(bubbles).toHaveCount(4)
    await expect(bubbles.nth(0)).toContainText('Покажи карточку на сегодня')
    await expect(bubbles.nth(3)).toContainText('тестовый ответ')
    await expect(page.getByRole('heading', { name: 'Сегодня', level: 3 })).toBeVisible()
  })

  test('older history is paged in on request without duplicates', async ({ page }) => {
    await signIn(page)
    await page.goto('/chat')
    // 18 runs through the API: 36 feed entries, more than one page of 30.
    for (let i = 0; i < 18; i++) {
      const run = await page.request.post('/v1/runs', {
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        data: { profile: 'chat', input: { text: `Вопрос ${i}` } },
      })
      const { id } = (await run.json()) as { id: string }
      await page.request.get(`/v1/runs/${id}/events`)
    }
    await page.reload()
    await expect(page.locator('[data-role]')).toHaveCount(30)
    await page.getByRole('button', { name: 'Показать более ранние' }).click()
    await expect(page.locator('[data-role]')).toHaveCount(36)
    await expect(page.getByRole('button', { name: 'Показать более ранние' })).toHaveCount(0)
    await expect(page.locator('[data-role="user"]').first()).toContainText('Вопрос 0')
  })

  test('a dropped event stream resumes from the last cursor and shows the answer once', async ({
    page,
  }) => {
    await signIn(page)
    await page.goto('/chat')
    const resumed: (string | undefined)[] = []
    let first = true
    await page.route('**/v1/runs/*/events', async (route) => {
      resumed.push(route.request().headers()['last-event-id'])
      if (!first) return route.continue()
      first = false
      const response = await route.fetch()
      const frames = (await response.text()).trim().split('\n\n')
      // Cut the stream after the first event, as a broken connection would.
      await route.fulfill({
        response,
        body: frames[0] + '\n\n',
      })
    })
    await field(page).fill('Привет!')
    await field(page).press('Enter')
    // The chat list shows the answer as a preview too: look inside the conversation.
    await expect(
      page.getByRole('region', { name: 'Чат с Мией' }).getByText('тестовый ответ'),
    ).toHaveCount(1)
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(1)
    expect(resumed.length).toBeGreaterThanOrEqual(2)
    expect(resumed[0]).toBeUndefined()
    expect(resumed[1]).toBeTruthy()
  })

  test('a run waiting for approval is picked up again after a page reload', async ({ page }) => {
    await signIn(page)
    await installTracker(page)
    await page.goto('/chat')
    await field(page).fill('Создай задачу После перезагрузки')
    await field(page).press('Enter')
    await expect(
      page.getByRole('region', { name: /Подтверждение: .*После перезагрузки/ }),
    ).toBeVisible()

    await page.reload()
    const approval = page.getByRole('region', { name: /Подтверждение: .*После перезагрузки/ })
    await expect(approval).toBeVisible()
    await expect(page.locator('[data-role="user"]')).toHaveCount(1)
    await approval.getByRole('button', { name: 'Одобрить' }).click()
    await expect(page.getByText('Готово, задача создана.')).toBeVisible()
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(1)
  })

  test('the not-signed-in browser sees the sign-in screen, not the app', async ({ page }) => {
    await page.goto('/chat')
    await expect(page.getByRole('heading', { name: /Ваш интеллект/ })).toBeVisible()
    await expect(page.getByRole('navigation', { name: 'Главная навигация' })).toHaveCount(0)
  })
})

test.describe('voice input and read-aloud (fake browser APIs)', () => {
  test.beforeEach(async ({ page }) => {
    await fakeSpeech(page)
    await signIn(page, newSubject('voice'))
    await page.goto('/chat')
  })

  test('dictation fills the field and waits for the user to send', async ({ page }) => {
    await page.getByRole('button', { name: 'Голосовой ввод' }).click()
    await expect(page.getByRole('button', { name: 'Остановить диктовку' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await page.evaluate(() => window.__speech.say('привет', false))
    await expect(field(page)).toHaveValue('привет')
    await page.evaluate(() => window.__speech.say('привет мия', true))
    await page.evaluate(() => window.__speech.finish())
    await expect(field(page)).toHaveValue('привет мия')
    await expect(page.getByRole('button', { name: 'Голосовой ввод' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    // Nothing was sent by itself.
    await expect(page.locator('[data-role]')).toHaveCount(0)
    // Dictation appends to what was already typed.
    await field(page).fill('Сначала так,')
    await page.getByRole('button', { name: 'Голосовой ввод' }).click()
    await page.evaluate(() => window.__speech.say('потом так', true))
    await page.evaluate(() => window.__speech.finish())
    await expect(field(page)).toHaveValue('Сначала так, потом так')
  })

  test('a dictated question gets its answer read aloud automatically', async ({ page }) => {
    await page.getByRole('button', { name: 'Голосовой ввод' }).click()
    await page.evaluate(() => window.__speech.say('привет, как дела', true))
    await page.evaluate(() => window.__speech.finish())
    await expect(field(page)).toHaveValue('привет, как дела')
    await field(page).press('Enter')
    await expect(page.getByText('тестовый ответ').first()).toBeVisible()
    await expect.poll(() => page.evaluate(() => window.__speech.state.spoken)).toEqual([ANSWER])
    await expect(page.getByRole('button', { name: 'Остановить озвучку' })).toBeVisible()
    await page.evaluate(() => window.__speech.endSpeech())
    await expect(page.getByRole('button', { name: 'Озвучить ответ' })).toBeVisible()
  })

  test('a typed question is not read aloud, the Озвучить button reads and stops', async ({
    page,
  }) => {
    await field(page).fill('Привет!')
    await field(page).press('Enter')
    await expect(page.getByText('тестовый ответ').first()).toBeVisible()
    expect(await page.evaluate(() => window.__speech.state.spoken)).toEqual([])

    await page.getByRole('button', { name: 'Озвучить ответ' }).click()
    await expect.poll(() => page.evaluate(() => window.__speech.state.spoken.length)).toBe(1)
    await page.getByRole('button', { name: 'Остановить озвучку' }).click()
    await expect(page.getByRole('button', { name: 'Озвучить ответ' })).toBeVisible()
    expect(await page.evaluate(() => window.__speech.state.cancelled)).toBeGreaterThan(0)
  })

  test('"Always read aloud" speaks typed questions too and is remembered', async ({ page }) => {
    await page.getByRole('button', { name: 'Голос', exact: true }).click()
    await page.getByLabel('Всегда озвучивать ответы').check()
    await page.reload()
    await page.getByRole('button', { name: 'Голос', exact: true }).click()
    await expect(page.getByLabel('Всегда озвучивать ответы')).toBeChecked()
    await field(page).fill('Привет!')
    await field(page).press('Enter')
    await expect.poll(() => page.evaluate(() => window.__speech.state.spoken)).toEqual([ANSWER])
  })

  test('"Send at once" sends right after dictation ends', async ({ page }) => {
    await page.getByRole('button', { name: 'Голос', exact: true }).click()
    await page.getByLabel('Отправлять сразу после диктовки').check()
    await page.getByRole('button', { name: 'Голосовой ввод' }).click()
    await page.evaluate(() => window.__speech.say('привет', true))
    await page.evaluate(() => window.__speech.finish())
    await expect(page.locator('[data-role="user"]')).toHaveText('привет')
    await expect(page.getByText('тестовый ответ').first()).toBeVisible()
    await expect.poll(() => page.evaluate(() => window.__speech.state.spoken)).toEqual([ANSWER])
  })
})

test('a browser without speech recognition hides the microphone and says why', async ({ page }) => {
  await noRecognition(page)
  await signIn(page)
  await page.goto('/chat')
  await expect(page.getByRole('button', { name: 'Голосовой ввод' })).toHaveCount(0)
  await expect(page.getByText(/Диктовка недоступна в этом браузере/)).toBeVisible()
  await field(page).fill('Привет!')
  await field(page).press('Enter')
  await expect(page.getByText('тестовый ответ').first()).toBeVisible()
})
