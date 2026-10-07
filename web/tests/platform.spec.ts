import { expect, test, type Page } from '@playwright/test'

async function install(page: Page, name: string) {
  const card = page
    .locator('article')
    .filter({ has: page.getByRole('heading', { name, exact: true }) })
  await card.getByRole('button', { name: 'Добавить в пространство' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('button', { name: 'Добавить плагин', exact: true }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  return card
}

test('catalog search and categories work, installation persists after reload', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('article')).toHaveCount(7)
  await page.getByRole('button', { name: 'Продуктивность', exact: true }).click()
  await expect(page.locator('article')).toHaveCount(2)
  await page.getByRole('button', { name: 'Все плагины', exact: true }).click()
  await page.getByLabel('Поиск плагинов').fill('таймер')
  await expect(page.locator('article')).toHaveCount(1)
  await install(page, 'Таймер')
  await page.reload()
  await page.getByRole('button', { name: 'Мои плагины' }).click()
  await expect(page.getByRole('heading', { name: 'Таймер', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Удалить плагин Таймер' }).click()
  await expect(
    page.getByRole('heading', { name: 'Здесь начинается ваше пространство' }),
  ).toBeVisible()
})

test('notes persist and can be completed and removed', async ({ page }) => {
  await page.goto('/')
  const card = await install(page, 'Мысли вслух')
  await card.getByRole('button', { name: 'Открыть плагин' }).click()
  await page.getByLabel('Новая заметка').fill('Проверить новый web POC')
  await page.getByRole('button', { name: 'Сохранить мысль' }).click()
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click()
  await page.reload()
  await page
    .locator('article')
    .filter({ hasText: 'Мысли вслух' })
    .getByRole('button', { name: 'Открыть плагин' })
    .click()
  await expect(page.getByText('Проверить новый web POC', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Выполнить задачу' }).click()
  await expect(page.locator('.note-row')).toHaveClass(/done/)
  await page.getByRole('button', { name: 'Удалить заметку' }).click()
  await expect(page.locator('.note-row')).toHaveCount(0)
})

test('timer continues outside its dialog and signals exactly once', async ({ page }) => {
  await page.clock.install()
  await page.goto('/')
  const card = await install(page, 'Таймер')
  await card.getByRole('button', { name: 'Открыть плагин' }).click()
  await page.getByLabel('Длительность в минутах').fill('0.05')
  await page.getByRole('button', { name: 'Начать', exact: true }).click()
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click()
  await expect(page.getByLabel('Активные таймеры')).toBeVisible()
  await page.clock.runFor(3500)
  await expect(page.getByLabel('Активные таймеры')).not.toBeVisible()
  await expect(page.getByText('⏰ Таймер', { exact: true })).toHaveCount(1)
  await page.clock.runFor(1500)
  await expect(page.getByText('⏰ Таймер', { exact: true })).toHaveCount(1)
})

test('world clocks support adding time zones', async ({ page }) => {
  await page.goto('/')
  const card = await install(page, 'Мировое время')
  await card.getByRole('button', { name: 'Открыть плагин' }).click()
  await expect(page.locator('.world-clock')).toHaveCount(3)
  await page.getByLabel('Часовой пояс').selectOption('America/New_York')
  await page.getByRole('button', { name: 'Добавить', exact: true }).click()
  await expect(page.locator('.world-clock')).toHaveCount(4)
})

test('focus sessions can pause and switch to a break', async ({ page }) => {
  await page.clock.install()
  await page.goto('/')
  const card = await install(page, 'В потоке')
  await card.getByRole('button', { name: 'Открыть плагин' }).click()
  await expect(page.locator('.countdown-display')).toHaveText('25:00')
  await page.getByRole('button', { name: 'Начать', exact: true }).click()
  await page.getByRole('button', { name: 'Пауза', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Продолжить', exact: true })).toBeVisible()
  const paused = await page.locator('.countdown-display').textContent()
  await page.clock.runFor(60000)
  await expect(page.locator('.countdown-display')).toHaveText(paused!)
  await page.getByRole('button', { name: '5 мин · перерыв' }).click()
  await expect(page.locator('.countdown-display')).toHaveText('05:00')
})

test('alarm fires outside the dialog at the scheduled local time', async ({ page }) => {
  await page.clock.install()
  await page.goto('/')
  const card = await install(page, 'Мягкий старт')
  await card.getByRole('button', { name: 'Открыть плагин' }).click()
  const nextMinute = await page.evaluate(() => {
    const date = new Date(Date.now() + 60000)
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  })
  await page.getByLabel('Время будильника').fill(nextMinute)
  await page.getByLabel('Название будильника').fill('Время для прогулки')
  await page.getByRole('button', { name: 'Установить будильник' }).click()
  await page.getByRole('button', { name: 'Закрыть', exact: true }).click()
  await expect(page.getByLabel('Активные таймеры')).toBeVisible()
  const untilAlarm = await page.evaluate(async () => {
    const state = await (await fetch('/api/web/workspace')).json()
    return state.states['mia.alarm'].deadline - Date.now() + 300
  })
  await page.clock.runFor(untilAlarm)
  await expect(page.getByText('⏰ Время для прогулки', { exact: true })).toHaveCount(1)
  await expect(page.getByLabel('Активные таймеры')).not.toBeVisible()
})

test('JSON recipes validate permissions, install and can be shared', async ({ page }) => {
  await page.goto('/#studio')
  await page.getByRole('button', { name: 'Рецепт v1' }).click()
  const valid = await page.getByLabel('JSON-рецепт плагина').inputValue()
  const invalid = JSON.parse(valid)
  invalid.permissions.push('system.exec')
  await page.getByLabel('JSON-рецепт плагина').fill(JSON.stringify(invalid))
  await page.getByRole('button', { name: 'Добавить рецепт', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('permissions')
  await page.getByLabel('JSON-рецепт плагина').fill(valid)
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Скачать JSON' }).click()
  expect((await downloadPromise).suggestedFilename()).toBe('community.day-planner.json')
  await page.getByRole('button', { name: 'Добавить рецепт', exact: true }).click()
  await page.getByRole('button', { name: 'Добавить плагин', exact: true }).click()
  await page.getByRole('button', { name: 'Мои плагины' }).click()
  await expect(page.getByRole('heading', { name: 'План моего дня' })).toBeVisible()
})

test('provider keys never enter browser storage or public API responses', async ({ page }) => {
  await page.goto('/#models')
  const synthetic = await page.evaluate(() => crypto.randomUUID())
  await page.getByRole('button', { name: /OpenAI Ваш API-ключ/ }).click()
  await page.getByLabel('API-ключ', { exact: true }).fill(synthetic)
  await page.getByRole('button', { name: 'Сохранить подключение' }).click()
  await expect(page.locator('.connection-row')).toHaveCount(1)
  const state = await page.evaluate(async () => ({
    local: JSON.stringify(localStorage),
    session: JSON.stringify(sessionStorage),
    connections: await (await fetch('/api/web/connections')).text(),
    cookie: document.cookie,
  }))
  for (const value of Object.values(state)) expect(value).not.toContain(synthetic)
  expect(state.cookie).not.toContain('mia_web_session')
  await page.getByRole('button', { name: 'Удалить OpenAI', exact: true }).click()
  await expect(page.locator('.connection-row')).toHaveCount(0)
})

test('mobile layout has no horizontal overflow and navigation works', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.locator('article')).toHaveCount(7)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('button', { name: 'Открыть меню' }).click()
  await page.getByRole('button', { name: 'Модели и ключи', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Ваш интеллект. Ваш выбор.' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})
