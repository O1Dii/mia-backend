import { expect, test, type Page } from '@playwright/test'
import { horizontalOverflow, signIn } from './support/helpers'

async function model(page: Page) {
  await page.request.post('/v1/model-connections', {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    data: { provider: 'deepinfra', label: 'GLM', model: 'm', api_key: 'sk-test-abcd1234' },
  })
}

async function schedule(page: Page, goal: string) {
  const response = await page.request.post('/v1/schedules', {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    data: {
      rule: 'RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=0',
      timezone: 'Europe/Minsk',
      goal,
      delivery: { channels: ['inbox'] },
      kind: 'reminder',
    },
  })
  expect(response.status()).toBe(201)
  return ((await response.json()) as { id: string }).id
}

test.describe('inbox and background tasks', () => {
  test('empty states explain where things will appear', async ({ page }) => {
    await signIn(page)
    await page.goto('/inbox')
    await expect(page.getByRole('heading', { name: 'Входящие и задачи', level: 1 })).toBeVisible()
    await expect(page.getByText(/Пока пусто/)).toBeVisible()
    await page.getByRole('tab', { name: 'Задачи' }).click()
    await expect(page.getByText(/Фоновых задач пока нет/)).toBeVisible()
    await page.getByRole('tab', { name: 'Расписания' }).click()
    await expect(page.getByText(/Расписаний нет/)).toBeVisible()
  })

  test('a schedule can be paused, resumed, run now and then lands in the inbox', async ({
    page,
  }) => {
    await signIn(page)
    await schedule(page, 'Выпить воды')
    await page.goto('/inbox')
    await page.getByRole('tab', { name: 'Расписания' }).click()
    const row = page.locator('[data-record="schedule"]', { hasText: 'Выпить воды' })
    await expect(row).toContainText('Каждый день в 09:00')
    await expect(row).toContainText('Работает')

    await row.getByRole('button', { name: /^Поставить на паузу/ }).click()
    await expect(row).toContainText('На паузе')
    await expect(row.getByRole('button', { name: /^Возобновить/ })).toBeVisible()
    await row.getByRole('button', { name: /^Возобновить/ }).click()
    await expect(row).toContainText('Работает')

    // "Run now" queues a job; the worker delivers the reminder without a model call.
    await row.getByRole('button', { name: /^Запустить сейчас/ }).click()
    await expect(page.getByRole('tab', { name: /^Задачи/, selected: true })).toBeVisible()
    await expect(page.locator('[data-record="job"]').first()).toBeVisible()
    await expect(
      page.getByRole('link', { name: /^Входящие/ }).getByLabel(/непрочитано/),
    ).toBeVisible({ timeout: 30_000 })

    await page.getByRole('tab', { name: /^Входящие/ }).click()
    const item = page.locator('[data-record="inbox"][data-read="false"]').first()
    await expect(item).toContainText('Выпить воды')
    await item.getByRole('button', { name: /^Прочитано:/ }).click()
    await expect(page.locator('[data-record="inbox"][data-read="false"]')).toHaveCount(0)
    await expect(
      page.getByRole('link', { name: /^Входящие/ }).getByLabel(/непрочитано/),
    ).toHaveCount(0)

    // Unread filter hides the item that is read now.
    await page.getByLabel('Только непрочитанные').check()
    await expect(page.getByText('Непрочитанного нет.')).toBeVisible()
  })

  test('a background task runs and its answer arrives with a status', async ({ page }) => {
    await signIn(page)
    await model(page)
    await page.goto('/inbox')
    await page.getByRole('tab', { name: 'Задачи' }).click()
    await page.getByRole('button', { name: 'Новая задача' }).click()
    await page.getByRole('dialog').getByLabel('Что сделать').fill('Собери сводку по проекту')
    await page.getByRole('dialog').getByRole('button', { name: 'Поставить в очередь' }).click()
    const job = page.locator('[data-record="job"]', { hasText: 'Собери сводку по проекту' })
    await expect(job).toBeVisible()
    await expect(job.locator('.mia-chip').first()).toHaveText('Готово', { timeout: 30_000 })
    await expect(job).toContainText('это тестовый ответ')
    // Finished work has no cancel button.
    await expect(job.getByRole('button', { name: /^Отменить/ })).toHaveCount(0)
  })

  test('a schedule is deleted from the list', async ({ page }) => {
    await signIn(page)
    await schedule(page, 'Сделать зарядку')
    await page.goto('/inbox')
    await page.getByRole('tab', { name: 'Расписания' }).click()
    await page.getByRole('button', { name: 'Удалить расписание: Сделать зарядку' }).click()
    await expect(page.getByText(/Расписаний нет/)).toBeVisible()
  })

  test('the screens fit a phone', async ({ page }, info) => {
    await signIn(page)
    await schedule(page, 'Позвонить маме и обсудить всё, что нужно обсудить на этой неделе')
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/inbox')
    await page.getByRole('tab', { name: 'Расписания' }).click()
    await expect(page.locator('[data-record="schedule"]')).toBeVisible()
    expect(await horizontalOverflow(page)).toEqual([])
    await page.screenshot({ path: info.outputPath('screens/inbox-mobile.png'), fullPage: true })
    await page.goto('/more')
    await expect(page.getByRole('link', { name: /Где ваши данные/ })).toBeVisible()
    expect(await horizontalOverflow(page)).toEqual([])
  })
})
