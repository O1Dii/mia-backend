import { expect, test, type Page } from '@playwright/test'
import { fakeSpeech, installBuiltin, installTracker, newSubject, signIn } from './support/helpers'

// M75: chats (ADR-0017) on the web: list, new chat, switching, rename, pin, delete, a feed per
// chat, voice into the current chat, retention, result cards, tables, the call journal.
const field = (page: Page) => page.getByRole('textbox', { name: 'Сообщение Мии' })
const list = (page: Page) => page.getByRole('complementary', { name: 'Чаты' })
const reply = (page: Page) => page.locator('.mw-chat [data-role="assistant"]')
const row = (page: Page, text: string | RegExp) =>
  list(page).locator('li.mw-chats__item', { hasText: text })
const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
const chatId = (page: Page) => page.url().split('/').pop() as string

type ChatRow = { id: string; title: string | null; title_source: string | null; pinned: boolean }
async function chatsOf(page: Page): Promise<ChatRow[]> {
  const body = (await (await page.request.get('/v1/chats')).json()) as { items: ChatRow[] }
  return body.items
}

async function say(page: Page, text: string) {
  await field(page).fill(text)
  await field(page).press('Enter')
}

test.describe('chat list', () => {
  test('new chat, switch, rename, pin and delete; every chat keeps its own history', async ({
    page,
  }) => {
    await signIn(page)
    await page.goto('/chat')
    await expect(page.getByRole('heading', { name: 'С чего начнём?' })).toBeVisible()

    // The first message creates the chat; the address names it and the model titles it.
    await say(page, 'Привет!')
    await expect(reply(page)).toHaveCount(1)
    await expect(reply(page).last()).toContainText('тестовый ответ')
    await expect(page).toHaveURL(new RegExp(`/chat/${UUID}$`))
    const first = chatId(page)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Модельное привет')
    await expect(row(page, 'Модельное привет')).toHaveCount(1)

    // «Новый чат» opens an empty draft; nothing is created until the first message.
    await list(page).getByRole('button', { name: 'Новый чат', exact: true }).click()
    await expect(page).toHaveURL(/\/chat\/new$/)
    await expect(page.getByRole('heading', { name: 'С чего начнём?' })).toBeVisible()
    await expect(page.locator('[data-role]')).toHaveCount(0)
    expect(await chatsOf(page)).toHaveLength(1)
    await say(page, 'Покажи карточку на сегодня')
    await expect(page.getByRole('heading', { name: 'Сегодня', level: 3 })).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`/chat/${UUID}$`))
    const second = chatId(page)
    expect(second).not.toBe(first)
    await expect(list(page).locator('li[data-chat-id]')).toHaveCount(2)

    // Switching shows the other chat's own feed, not a mix.
    await row(page, 'Модельное привет').getByRole('button').first().click()
    await expect(page).toHaveURL(new RegExp(`/chat/${first}$`))
    await expect(page.locator('[data-role="user"]')).toHaveText(['Привет!'])
    await expect(page.getByRole('heading', { name: 'Сегодня', level: 3 })).toHaveCount(0)
    await row(page, 'Покажи карточку').getByRole('button').first().click()
    await expect(page.getByRole('heading', { name: 'Сегодня', level: 3 })).toBeVisible()
    await expect(page.locator('[data-role="user"]')).toHaveText(['Покажи карточку на сегодня'])

    // Rename: the user's name wins and survives a reload.
    await row(page, 'Покажи карточку')
      .getByRole('button', { name: /Действия чата/ })
      .click()
    await page.getByRole('button', { name: 'Переименовать' }).click()
    await page.getByRole('textbox', { name: 'Название чата' }).fill('Планы на неделю')
    await page.getByRole('textbox', { name: 'Название чата' }).press('Enter')
    await expect(row(page, 'Планы на неделю')).toHaveCount(1)
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Планы на неделю')
    const renamed = (await chatsOf(page)).find((c) => c.id === second)
    expect(renamed).toMatchObject({ title: 'Планы на неделю', title_source: 'user' })
    await page.reload()
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Планы на неделю')

    // Pin: the pinned chat goes to the top of the list, also after the other one is newer.
    await say(page, 'Привет!')
    await expect(reply(page)).toHaveCount(2)
    await expect(reply(page).last()).toContainText('тестовый ответ')
    await row(page, 'Модельное привет')
      .getByRole('button', { name: /Действия чата/ })
      .click()
    await page.getByRole('button', { name: 'Закрепить', exact: true }).click()
    await expect(list(page).locator('li[data-chat-id]').first()).toHaveAttribute(
      'data-chat-id',
      first,
    )
    await expect(list(page).locator('li[data-pinned="true"]')).toHaveCount(1)
    expect((await chatsOf(page)).find((c) => c.id === first)?.pinned).toBe(true)
    // …and from the header of the open chat.
    await row(page, 'Модельное привет').getByRole('button').first().click()
    await page.getByRole('button', { name: 'Открепить чат' }).click()
    await expect(list(page).locator('li[data-pinned="true"]')).toHaveCount(0)
    expect((await chatsOf(page)).find((c) => c.id === first)?.pinned).toBe(false)

    // Delete asks first, then falls back to the remaining chat.
    await row(page, 'Модельное привет')
      .getByRole('button', { name: /Действия чата/ })
      .click()
    await page.getByRole('button', { name: 'Удалить', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Удалить чат?' })
    await dialog.getByRole('button', { name: 'Отмена' }).click()
    await expect(row(page, 'Модельное привет')).toHaveCount(1)
    await row(page, 'Модельное привет')
      .getByRole('button', { name: /Действия чата/ })
      .click()
    await page.getByRole('button', { name: 'Удалить', exact: true }).click()
    await dialog.getByRole('button', { name: 'Удалить чат' }).click()
    await expect(list(page).locator('li[data-chat-id]')).toHaveCount(1)
    await expect(page).toHaveURL(new RegExp(`/chat/${second}$`))
    expect(await chatsOf(page)).toHaveLength(1)
  })

  test('/chat opens the latest chat; an unknown address falls back to it', async ({ page }) => {
    await signIn(page)
    await page.goto('/chat')
    await say(page, 'Привет!')
    await expect(reply(page)).toHaveCount(1)
    await expect(reply(page).last()).toContainText('тестовый ответ')
    const id = chatId(page)
    await page.goto('/chat')
    await expect(page).toHaveURL(new RegExp(`/chat/${id}$`))
    await expect(page.locator('[data-role]')).toHaveCount(2)
    await page.goto('/chat/00000000-0000-4000-8000-000000000000')
    await expect(page).toHaveURL(new RegExp(`/chat/${id}$`))
    await expect(page.locator('[data-role]')).toHaveCount(2)
    // The «Чат» tab returns to the open chat, not to the latest one.
    await page.getByRole('link', { name: 'Дашборд' }).click()
    await page.getByRole('link', { name: 'Чат', exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`/chat/${id}$`))
  })

  test('a dictated question goes into the chat on screen', async ({ page }) => {
    await fakeSpeech(page)
    await signIn(page, newSubject('voice'))
    await page.goto('/chat')
    await say(page, 'Привет!')
    await expect(reply(page)).toHaveCount(1)
    await expect(reply(page).last()).toContainText('тестовый ответ')
    const first = chatId(page)
    await list(page).getByRole('button', { name: 'Новый чат', exact: true }).click()

    const bodies: Record<string, unknown>[] = []
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/v1/runs'))
        bodies.push(request.postDataJSON())
    })
    await page.getByRole('button', { name: 'Голосовой ввод' }).click()
    await page.evaluate(() => window.__speech.say('как дела', true))
    await page.evaluate(() => window.__speech.finish())
    await expect(field(page)).toHaveValue('как дела')
    await field(page).press('Enter')
    await expect(reply(page)).toHaveCount(1)
    await expect(reply(page).last()).toContainText('тестовый ответ')
    expect(bodies).toHaveLength(1)
    const second = chatId(page)
    expect(second).not.toBe(first)
    expect(bodies[0]).toMatchObject({ conversation_id: second, input: { text: 'как дела' } })
    // The answer is read aloud, as for any dictated question.
    await expect.poll(() => page.evaluate(() => window.__speech.state.spoken.length)).toBe(1)
    // Switching drops what was half-typed: it belongs to the previous chat.
    await field(page).fill('недописано')
    await row(page, 'Модельное привет').getByRole('button').first().click()
    await expect(field(page)).toHaveValue('')
  })

  test('the retention setting offers 30, 60, 180 days and never, and is remembered', async ({
    page,
  }) => {
    await signIn(page)
    await page.goto('/chat')
    const select = page.getByLabel('Хранить чаты')
    await expect(select).toHaveValue('60')
    await expect(select.locator('option')).toHaveText([
      '30 дней',
      '60 дней',
      '180 дней',
      'Не удалять',
    ])
    await select.selectOption('30')
    await expect
      .poll(
        async () =>
          ((await (await page.request.get('/v1/chats/settings')).json()) as any).retention_days,
      )
      .toBe(30)
    await page.reload()
    await expect(page.getByLabel('Хранить чаты')).toHaveValue('30')
    await page.getByLabel('Хранить чаты').selectOption('never')
    await expect
      .poll(
        async () =>
          ((await (await page.request.get('/v1/chats/settings')).json()) as any).retention_days,
      )
      .toBeNull()
    await page.reload()
    await expect(page.getByLabel('Хранить чаты')).toHaveValue('never')
  })
})

test.describe('answers and steps', () => {
  test('a Markdown table in an answer is a real table, safe and scrollable', async ({ page }) => {
    await signIn(page)
    await page.goto('/chat')
    await say(page, 'Покажи проекты таблицей')
    const table = page.getByRole('table')
    await expect(table).toBeVisible()
    await expect(table.getByRole('columnheader')).toHaveText([
      'Проект',
      'Статус',
      'Задач',
      'Ответственный',
      'Ближайший срок',
      'Комментарий',
    ])
    await expect(table.getByRole('row')).toHaveCount(4)
    await expect(table.getByRole('cell', { name: 'идёт' }).locator('strong')).toBeVisible()
    await expect(table.getByRole('link', { name: 'Сайт' })).toHaveAttribute(
      'href',
      'https://example.test/site',
    )
    await expect(table.getByRole('columnheader', { name: 'Задач' })).toHaveCSS(
      'text-align',
      'right',
    )
    await expect(table.getByRole('columnheader', { name: 'Статус' })).toHaveCSS(
      'text-align',
      'center',
    )
    await expect(page.locator('.mw-md').getByText('|')).toHaveCount(0)

    // On a phone the table scrolls inside its own box, the page does not scroll sideways.
    await page.setViewportSize({ width: 390, height: 844 })
    const box = page.getByRole('region', { name: 'Таблица' })
    expect(await box.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })

  test('the call journal: chips in a row, arguments and the reason open on a tap', async ({
    page,
  }) => {
    await signIn(page)
    await page.goto('/chat')
    await page.route('**/v1/runs/*/events', async (route) => {
      const runId = /runs\/([^/]+)\/events/.exec(route.request().url())![1]
      const frame = (n: number, type: string, payload: Record<string, unknown>) =>
        `id: c${n}\nevent: ${type}\ndata: ${JSON.stringify({
          event_id: crypto.randomUUID(),
          run_id: runId,
          sequence: n,
          cursor: `c${n}`,
          type,
          at: new Date().toISOString(),
          payload,
        })}\n\n`
      const body =
        frame(1, 'tool.call.started', {
          tool_call_id: 'call-a',
          tool: 'builtin.internet.search',
          risk: 'read',
          executor: 'server',
          arguments: { query: 'курс доллара', contact: '[email]' },
        }) +
        frame(2, 'tool.call.completed', { tool_call_id: 'call-a', status: 'succeeded' }) +
        frame(3, 'tool.call.started', {
          tool_call_id: 'call-b',
          tool: 'builtin.news.refresh',
          risk: 'read',
          executor: 'server',
          arguments: { query: 'ИИ', hours: 48 },
        }) +
        frame(4, 'tool.call.completed', {
          tool_call_id: 'call-b',
          status: 'failed',
          error: { code: 'tool_execution_failed', message: 'Tool execution failed.' },
          error_detail: 'Поиск новостей по запросу «ИИ» не удался: источник недоступен.',
        }) +
        frame(5, 'tool.call.started', {
          tool_call_id: 'call-c',
          tool: 'builtin.timer.start',
          risk: 'write_reversible',
          executor: 'server',
        }) +
        frame(6, 'tool.call.completed', {
          tool_call_id: 'call-c',
          status: 'denied',
          error: { code: 'approval_denied', message: 'denied' },
        }) +
        frame(7, 'response.final', {
          response: { speech: 'Не всё получилось.', display: null, followups: [] },
        }) +
        frame(8, 'run.completed', {})
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body })
    })
    await say(page, 'Найди новости')
    const steps = page.getByRole('list', { name: 'Действия Мии' })
    await expect(steps.locator('.mw-step')).toHaveCount(3)

    // Everything is folded at first: chips only, no reason, no arguments.
    await expect(page.getByTestId('step-reason')).toHaveCount(0)
    await expect(page.getByText('курс доллара')).toHaveCount(0)

    // A successful step: a tap shows its redacted arguments.
    const ok = steps.getByRole('button', { name: 'search · готово' })
    await expect(ok).toBeVisible()
    await ok.click()
    await expect(page.getByRole('term').first()).toHaveText('query')
    await expect(page.getByText('курс доллара')).toBeVisible()
    await expect(page.getByText('[email]')).toBeVisible()

    // A failed step: the reason is behind its chip, with the arguments.
    await steps.getByRole('button', { name: 'refresh · не удалось' }).click()
    await expect(page.getByTestId('step-reason')).toContainText('источник недоступен')
    await expect(page.getByTestId('step-reason')).toContainText('Причина:')
    await expect(page.getByText('48')).toBeVisible()

    // Without a reason from the tool the public error code is told in words.
    await steps.getByRole('button', { name: 'start · отклонено' }).click()
    await expect(page.getByTestId('step-reason')).toHaveText('Причина: Вы отклонили действие.')
  })

  test('the arguments of a real call reach the step', async ({ page }) => {
    await signIn(page)
    await installTracker(page)
    await page.goto('/chat')
    await say(page, 'Создай задачу Купить хлеб')
    await page
      .getByRole('region', { name: /Подтверждение: .*Купить хлеб/ })
      .getByRole('button', { name: 'Одобрить' })
      .click()
    await expect(page.getByText('Готово, задача создана.')).toBeVisible()
    await page.getByRole('button', { name: 'create issue · готово' }).click()
    await expect(page.getByRole('term')).toHaveText('title')
    await expect(page.getByRole('definition')).toHaveText('Купить хлеб')
  })

  test('after a lost event stream the chat re-reads the feed and shows the screen action', async ({
    page,
  }) => {
    await signIn(page)
    await installBuiltin(page, 'builtin.telegram')
    await page.goto('/chat')
    // Every event stream fails at once (404 is not retried): the run itself still goes on.
    await page.route('**/v1/runs/*/events', (route) =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: { code: 'run_not_found', message: 'gone' } }),
      }),
    )
    await say(page, 'Подключи telegram бота')
    const reply = page.locator('[data-role="assistant"]').last()
    await expect(reply.getByRole('link', { name: 'Открыть Telegram' })).toHaveAttribute(
      'href',
      'https://t.me/mia_e2e_bot?start=E2E-ONE-TIME-TOKEN',
    )
    await expect(reply).toContainText('Открой ссылку на экране')
    await expect(page.getByText('Связь потеряна')).toHaveCount(0)
  })
})

test.describe('background task result card', () => {
  test('the result of a task started in chat A lands in A, not in B, and in the inbox', async ({
    page,
  }) => {
    test.setTimeout(120_000)
    await signIn(page)
    await page.goto('/chat')
    await say(page, 'Сделай подробный отчёт в фоне')
    await expect(page).toHaveURL(new RegExp(`/chat/${UUID}$`))
    const a = chatId(page)
    const card = page.locator('[data-card="job_result"]')
    // The chat is not reloaded: the strip notices the finished task and brings the card in.
    await expect(card).toBeVisible({ timeout: 60_000 })
    await expect(card).toContainText('Задача готова')
    await expect(card.getByRole('link', { name: 'Во входящих' })).toBeVisible()

    await list(page).getByRole('button', { name: 'Новый чат', exact: true }).click()
    await say(page, 'Привет!')
    await expect(reply(page)).toHaveCount(1)
    await expect(reply(page).last()).toContainText('тестовый ответ')
    await expect(page.locator('[data-card="job_result"]')).toHaveCount(0)
    await page.goto(`/chat/${a}`)
    await expect(page.locator('[data-card="job_result"]')).toHaveCount(1)
    await page.goto('/inbox')
    await expect(page.getByText('Сделай подробный отчёт в фоне').first()).toBeVisible()
  })
})
