import { expect, test, type Page } from '@playwright/test'
import { horizontalOverflow, signIn } from './support/helpers'

type Sent = { text: string; buttons: string[]; edited?: boolean }

async function botSaid(page: Page): Promise<Sent[]> {
  const response = await page.request.get('/__e2e/telegram/sent')
  return ((await response.json()) as { sent: Sent[] }).sent
}

async function linkBot(page: Page) {
  expect((await page.request.post('/__e2e/telegram/link')).status()).toBe(200)
}

async function press(page: Page, data: string) {
  const response = await page.request.post('/__e2e/telegram/press', { data: { data } })
  expect(response.status()).toBe(200)
}

async function reminder(page: Page, goal: string) {
  const created = await page.request.post('/v1/schedules', {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    // No delivery: the user's notification settings decide where the reminder goes.
    data: {
      rule: 'RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
      timezone: 'Europe/Minsk',
      goal,
      kind: 'reminder',
    },
  })
  expect(created.status()).toBe(201)
  const { id } = (await created.json()) as { id: string }
  const run = await page.request.post(`/v1/schedules/${id}/run`, {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
  })
  expect(run.status()).toBe(202)
}

test.describe('notification channels', () => {
  test('a new user sees the inbox, push on and Telegram off; settings are reachable from «Ещё»', async ({
    page,
  }) => {
    await signIn(page)
    await page.goto('/more')
    await page.getByRole('link', { name: /Уведомления/ }).click()
    await expect(page).toHaveURL(/\/notifications$/)
    await expect(page.getByRole('heading', { name: 'Уведомления', level: 1 })).toBeVisible()

    const inbox = page.locator('[data-channel="inbox"]')
    await expect(inbox).toContainText('Всегда')
    const push = page.locator('[data-channel="push"]')
    await expect(push).toContainText('Не подключён')
    await expect(push).toContainText('Push пока не настроен на сервере')
    await expect(push.getByRole('checkbox')).toBeChecked()
    const telegram = page.locator('[data-channel="telegram"]')
    await expect(telegram).toContainText('Не подключён')
    await expect(telegram).toContainText('подключи Telegram-бота')
    await expect(telegram.getByRole('checkbox')).not.toBeChecked()

    // The table: one row per kind of event; every cell of an enabled channel is on.
    for (const kind of [
      'Таймеры и будильники',
      'Результаты задач',
      'Подтверждения',
      'Сообщения плагинов',
    ]) {
      await expect(
        page.getByRole('checkbox', { name: `${kind} → Входящие (всегда)` }),
      ).toBeDisabled()
      await expect(page.getByRole('checkbox', { name: `${kind} → Push на телефон` })).toBeChecked()
    }
    // A channel that is off cannot be picked in the table.
    await expect(
      page.getByRole('checkbox', { name: 'Результаты задач → Telegram-бот' }),
    ).toBeDisabled()
  })

  test('the bot asks, the answer is saved, and a reminder arrives in the bot', async ({ page }) => {
    await signIn(page)
    await page.goto('/notifications')
    await expect(page.locator('[data-channel="telegram"]')).toContainText('Не подключён')

    await linkBot(page)
    const asked = (await botSaid(page)).find((m) => m.buttons.length > 0)
    expect(asked?.text).toBe('Присылать сюда уведомления Mia?')
    expect(asked?.buttons).toEqual(['Да', 'Нет'])

    // Linked but not answered: connected, still off, and nothing is sent to the bot.
    await page.reload()
    const telegram = page.locator('[data-channel="telegram"]')
    await expect(telegram).toContainText('Подключён')
    await expect(telegram.getByRole('checkbox')).not.toBeChecked()
    await reminder(page, 'Размяться')
    await page.goto('/inbox')
    await expect(page.locator('[data-record="inbox"]').first()).toContainText('Размяться', {
      timeout: 30_000,
    })
    expect((await botSaid(page)).some((m) => m.text.includes('Размяться'))).toBe(false)

    await press(page, 'notify:telegram:yes')
    await page.goto('/notifications')
    await expect(telegram.getByRole('checkbox')).toBeChecked()
    await reminder(page, 'Выпить воды')
    await expect
      .poll(async () => (await botSaid(page)).some((m) => m.text.includes('Выпить воды')), {
        timeout: 30_000,
      })
      .toBe(true)
  })

  test('switches and table cells are saved and decide the routing', async ({ page }) => {
    await signIn(page)
    await linkBot(page)
    await press(page, 'notify:telegram:yes')
    await page.goto('/notifications')
    const telegram = page.locator('[data-channel="telegram"]')
    await expect(telegram.getByRole('checkbox')).toBeChecked()

    // Task results do not go to Telegram; timers still do (reminders are timers).
    const cell = page.getByRole('checkbox', { name: 'Результаты задач → Telegram-бот' })
    await expect(cell).toBeChecked()
    await cell.uncheck()
    await expect(cell).not.toBeChecked()
    await page.reload()
    await expect(
      page.getByRole('checkbox', { name: 'Результаты задач → Telegram-бот' }),
    ).not.toBeChecked()
    await expect(
      page.getByRole('checkbox', { name: 'Таймеры и будильники → Telegram-бот' }),
    ).toBeChecked()

    // Switch the channel off: the whole column is locked, its values are kept.
    await telegram.getByRole('checkbox').uncheck()
    await expect(telegram.getByRole('checkbox')).not.toBeChecked()
    await expect(
      page.getByRole('checkbox', { name: 'Таймеры и будильники → Telegram-бот' }),
    ).toBeDisabled()
    // The switch flips before its save lands; wait for the server to agree.
    const settings = async () => (await page.request.get('/v1/notifications/settings')).json()
    await expect
      .poll(
        async () =>
          (await settings()).channels.find((c: { name: string }) => c.name === 'telegram').enabled,
      )
      .toBe(false)
    const saved = await settings()
    expect(saved.matrix.job_result.telegram).toBe(false)
    expect(saved.matrix.timer_alarm.telegram).toBe(true)

    await reminder(page, 'Позвонить маме')
    await page.goto('/inbox')
    await expect(page.locator('[data-record="inbox"]').first()).toContainText('Позвонить маме', {
      timeout: 30_000,
    })
    expect((await botSaid(page)).some((m) => m.text.includes('Позвонить маме'))).toBe(false)
  })

  test('«Нет» keeps Telegram off', async ({ page }) => {
    await signIn(page)
    await linkBot(page)
    await press(page, 'notify:telegram:no')
    await page.goto('/notifications')
    const telegram = page.locator('[data-channel="telegram"]')
    await expect(telegram).toContainText('Подключён')
    await expect(telegram.getByRole('checkbox')).not.toBeChecked()
    const said = await botSaid(page)
    expect(said.some((m) => m.edited && m.text.includes('приходить не будут'))).toBe(true)
  })

  for (const size of [
    { name: '390', width: 390, height: 844 },
    { name: '768', width: 768, height: 1024 },
    { name: '1440', width: 1440, height: 900 },
  ]) {
    test(`the screen fits ${size.name}px`, async ({ page }) => {
      await page.setViewportSize({ width: size.width, height: size.height })
      await signIn(page)
      await linkBot(page)
      await page.goto('/notifications')
      await expect(page.getByRole('region', { name: 'Таблица уведомлений' })).toBeVisible()
      expect(await horizontalOverflow(page)).toEqual([])
      await page.screenshot({ path: `test-results/notifications-${size.name}.png`, fullPage: true })
    })
  }
})
