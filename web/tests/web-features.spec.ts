import { expect, test, type Page } from '@playwright/test'

async function localModel(page: Page) {
  await page.goto('/#models')
  await page.getByRole('button', { name: /LM Studio \/ local/ }).click()
  await page.getByRole('button', { name: 'Сохранить подключение' }).click()
  await expect(page.getByRole('button', { name: 'Выбрана', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Диалог с Мией', exact: true }).click()
}

test('plugin cards open deep-linked pages with real screenshots, permissions and empty reviews', async ({
  page,
}) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'О плагине Таймер', exact: true }).click()
  await expect(page).toHaveURL(/#plugin\/mia.timer$/)
  await expect(page.getByRole('heading', { name: 'Таймер', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Отзывы пользователей' })).toBeVisible()
  await expect(page.getByText('Первый опыт — ваш', { exact: true })).toBeVisible()
  const screenshot = page.getByAltText('Таймер: интерфейс плагина')
  await expect(screenshot).toBeVisible()
  await expect
    .poll(() => screenshot.evaluate((img: HTMLImageElement) => img.naturalWidth))
    .toBeGreaterThan(100)
  await page.getByRole('button', { name: 'На дашборде', exact: true }).click()
  await expect(page.getByAltText('Таймер: виджет на дашборде')).toBeVisible()
  await page.getByRole('button', { name: 'Добавить в пространство', exact: true }).click()
  await page.getByRole('button', { name: 'Добавить плагин', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Открыть плагин', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Таймер', exact: true })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('hosted app requires Google sign-in before loading personal workspace', async ({ page }) => {
  let workspaceRequests = 0
  await page.route('**/api/web/auth/session', (route) =>
    route.fulfill({
      json: {
        required: true,
        user: null,
        google_client_id: '',
        google_hub: true,
        login_url: '/api/auth/google/login?return_to=%2Fmia%2F%3Fsignin%3Dgoogle',
      },
    }),
  )
  await page.route('**/api/web/workspace', (route) => {
    workspaceRequests++
    return route.fulfill({ status: 401, json: { detail: 'Войдите через Google.' } })
  })
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Продолжить с Google' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Продолжить с Google' })).toHaveAttribute(
    'href',
    '/api/auth/google/login?return_to=%2Fmia%2F%3Fsignin%3Dgoogle',
  )
  expect(workspaceRequests).toBe(0)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('review editor publishes, updates and removes the signed-in user review', async ({ page }) => {
  await page.route('**/api/web/auth/session', (route) =>
    route.fulfill({
      json: {
        required: false,
        user: { id: 'fixture-user', name: 'Автор теста', email: 'fixture@example.org' },
        google_client_id: '',
        google_hub: false,
        login_url: '',
      },
    }),
  )
  let items: { author: string; rating: number; text: string; updated_at: number; mine: boolean }[] =
    []
  await page.route('**/api/web/plugins/mia.timer/reviews', (route) => {
    const request = route.request()
    if (request.method() === 'POST') {
      const value = request.postDataJSON()
      items = [
        {
          author: 'Автор теста',
          rating: value.rating,
          text: value.text,
          updated_at: Date.now(),
          mine: true,
        },
      ]
    }
    if (request.method() === 'DELETE') items = []
    return route.fulfill({
      json: { items, count: items.length, average: items[0]?.rating ?? null },
    })
  })
  await page.goto('/#plugin/mia.timer')
  await page.getByRole('button', { name: 'Добавить в пространство', exact: true }).click()
  await page.getByRole('button', { name: 'Добавить плагин', exact: true }).click()
  await page.getByLabel('Оценка плагина').selectOption('4')
  await page.getByLabel('Текст отзыва').fill('Удобный таймер для работы.')
  await page.getByRole('button', { name: 'Опубликовать отзыв' }).click()
  await expect(page.locator('.review-card')).toContainText('Удобный таймер для работы.')
  await page.getByLabel('Текст отзыва').fill('Пользуюсь каждый день.')
  await page.getByRole('button', { name: 'Опубликовать отзыв' }).click()
  await expect(page.locator('.review-card')).toHaveCount(1)
  await expect(page.locator('.review-card')).toContainText('Пользуюсь каждый день.')
  await page.getByRole('button', { name: 'Удалить мой отзыв' }).click()
  await expect(page.locator('.review-card')).toHaveCount(0)
})

test('dictation edits text without sending and assistant replies can be spoken and stopped', async ({
  page,
}) => {
  await page.addInitScript(() => {
    const state = { recognition: null as any, spoken: [] as string[], cancels: 0 }
    ;(window as any).__voice = state
    ;(window as any).SpeechRecognition = class {
      onresult: any
      onerror: any
      onend: any
      start() {
        state.recognition = this
      }
      abort() {
        this.onend?.()
      }
      stop() {
        this.onend?.()
      }
    }
    ;(window as any).SpeechSynthesisUtterance = class {
      text: string
      constructor(text: string) {
        this.text = text
      }
    }
    Object.defineProperty(window, 'speechSynthesis', {
      value: {
        getVoices: () => [],
        speak: (utterance: any) => state.spoken.push(utterance.text),
        cancel: () => state.cancels++,
      },
      configurable: true,
    })
  })
  await localModel(page)
  let turns = 0
  await page.route('**/api/web/chat', (route) => {
    turns++
    expect(route.request().postDataJSON().messages[0].content).toBe('Привет Мия')
    return route.fulfill({
      json: { text: 'Привет! Чем помочь?', model: 'local-model', actions: [] },
    })
  })
  await page.getByRole('button', { name: 'Голосовой ввод', exact: true }).click()
  await page.evaluate(() => {
    ;(window as any).__voice.recognition.onresult({
      results: [{ isFinal: true, 0: { transcript: 'Привет Мия' } }],
    })
  })
  await expect(page.getByLabel('Сообщение Мии')).toHaveValue('Привет Мия')
  expect(turns).toBe(0)
  await page.getByLabel('Озвучивать ответы').check()
  await page.getByRole('button', { name: 'Отправить сообщение' }).click()
  await expect(page.getByText('Привет! Чем помочь?', { exact: true })).toBeVisible()
  await expect
    .poll(() => page.evaluate(() => (window as any).__voice.spoken.join('')))
    .toContain('Привет! Чем помочь?')
  await page.getByRole('button', { name: 'Остановить озвучку' }).click()
  await page.getByRole('button', { name: 'Озвучить ответ' }).click()
  await expect.poll(() => page.evaluate(() => (window as any).__voice.spoken.length)).toBe(2)
  const before = await page.evaluate(() => (window as any).__voice.cancels)
  await page.getByRole('button', { name: 'Дашборд', exact: true }).click()
  await expect
    .poll(() => page.evaluate(() => (window as any).__voice.cancels))
    .toBeGreaterThan(before)
})

test('denied microphone gives an actionable error and leaves text available', async ({ page }) => {
  await page.addInitScript(() => {
    ;(window as any).SpeechRecognition = class {
      onerror: any
      start() {
        this.onerror({ error: 'not-allowed' })
      }
      abort() {}
    }
  })
  await localModel(page)
  await page.getByRole('button', { name: 'Голосовой ввод', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Разрешите доступ к микрофону')
  await page.getByLabel('Сообщение Мии').fill('Можно написать текст')
  await expect(page.getByRole('button', { name: 'Отправить сообщение' })).toBeEnabled()
})

test('Google return completes one hub exchange under StrictMode and expired session locks the UI', async ({
  page,
}) => {
  let authenticated = false
  let exchanges = 0
  await page.route('**/api/web/auth/session', (route) =>
    route.fulfill({
      json: {
        required: true,
        user: authenticated
          ? { id: 'google-fixture', name: 'Тест входа', email: 'fixture@example.org' }
          : null,
        google_client_id: '',
        google_hub: true,
        login_url: '/api/auth/google/login?return_to=%2Fmia%2F%3Fsignin%3Dgoogle',
      },
    }),
  )
  await page.route('**/api/web/auth/hub', (route) => {
    exchanges++
    authenticated = true
    return route.fulfill({
      json: { user: { id: 'google-fixture', name: 'Тест входа', email: 'fixture@example.org' } },
    })
  })
  await page.goto('/?signin=google')
  await expect(page.locator('article')).toHaveCount(7)
  await expect(page.getByText('Тест входа', { exact: true })).toBeVisible()
  expect(exchanges).toBe(1)
  expect(new URL(page.url()).search).toBe('')
  authenticated = false
  await page.route('**/api/web/workspace', (route) =>
    route.fulfill({ status: 401, json: { detail: 'Войдите через Google.' } }),
  )
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('link', { name: 'Продолжить с Google' })).toBeVisible()
  await expect(page.locator('article')).toHaveCount(0)
})

test('unsupported speech recognition preserves keyboard input', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'SpeechRecognition', { value: undefined, configurable: true })
    Object.defineProperty(window, 'webkitSpeechRecognition', {
      value: undefined,
      configurable: true,
    })
  })
  await localModel(page)
  await expect(page.getByRole('button', { name: 'Голосовой ввод', exact: true })).toBeDisabled()
  await page.getByLabel('Сообщение Мии').fill('Обычный текст')
  await expect(page.getByRole('button', { name: 'Отправить сообщение' })).toBeEnabled()
})
