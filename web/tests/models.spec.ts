import { expect, test } from '@playwright/test'
import { signIn } from './support/helpers'

const KEY = 'sk-test-0123456789SECRETabcd'

test('a model connection is added with a key that is never shown again', async ({ page }) => {
  await signIn(page)
  await page.goto('/models')
  await expect(page.getByText('Начните с одной модели')).toBeVisible()

  await page.getByRole('button', { name: /DeepInfra/ }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('API-ключ').fill(KEY)
  await dialog.getByRole('button', { name: 'Сохранить подключение' }).click()

  const card = page.locator('[data-model="DeepInfra"]')
  await expect(card.getByText('Основная')).toBeVisible()
  await expect(card.getByText(/ключ …abcd/)).toBeVisible()
  expect(await page.content()).not.toContain('SECRET')

  await card.getByRole('button', { name: 'Проверить' }).click()
  await expect(page.getByText(/модель отвечает/)).toBeVisible()
  await expect(card.getByText('Проверено')).toBeVisible()

  // A second model can become the primary one; removing a connection removes the card.
  await page.getByRole('button', { name: /Mistral/ }).click()
  await page.getByRole('dialog').getByLabel('API-ключ').fill('sk-test-second-key-0000')
  await page.getByRole('dialog').getByRole('button', { name: 'Сохранить подключение' }).click()
  const second = page.locator('[data-model="Mistral"]')
  await second.getByRole('button', { name: 'Сделать основной' }).click()
  await expect(second.getByText('Основная')).toBeVisible()
  await expect(card.getByText('Основная')).toHaveCount(0)
  await card.getByRole('button', { name: /Удалить подключение/ }).click()
  await expect(card).toHaveCount(0)
})

test('the chat nudges to connect a model only while there is none', async ({ page }) => {
  await signIn(page)
  await page.goto('/chat')
  await expect(page.getByText('Модель ещё не подключена.')).toBeVisible()
  await page.getByRole('link', { name: 'Подключить модель' }).click()
  await expect(page).toHaveURL(/\/models$/)
})

test('ChatGPT subscription signs in by pasting the address the browser landed on', async ({
  page,
}) => {
  await signIn(page)
  // The fake auth.openai.com consents at once and sends the browser to the loopback address,
  // which does not open on the user's device (hosted Mia, M62-T1).
  let landed = ''
  await page.context().route('https://auth.openai.com/**', async (route) => {
    const url = new URL(route.request().url())
    const answer = await page.request.get(`/__e2e/chatgpt-authorize${url.search}`)
    landed = ((await answer.json()) as { location: string }).location
    await route.fulfill({ status: 200, contentType: 'text/html', body: 'ChatGPT' })
  })
  await page.goto('/models')
  await page.getByRole('button', { name: /ChatGPT \(подписка\)/ }).click()
  const dialog = page.getByRole('dialog')
  const popup = page.waitForEvent('popup')
  await dialog.getByRole('link', { name: 'Открыть вход ChatGPT' }).click()
  await (await popup).close()
  expect(landed).toMatch(/^http:\/\/127\.0\.0\.1:1455\/auth\/callback\?code=/)

  const field = dialog.getByLabel(/Скопируйте этот адрес/)
  await field.fill('http://127.0.0.1:1455/auth/callback?code=x&state=someone-else')
  await dialog.getByRole('button', { name: 'Подключить' }).click()
  await expect(dialog.getByText('Это не тот адрес.', { exact: false })).toBeVisible()

  await field.fill(landed)
  await dialog.getByRole('button', { name: 'Подключить' }).click()
  await expect(page.getByText('ChatGPT подключён')).toBeVisible()
  await expect(dialog).toHaveCount(0)
  const card = page.locator('[data-model="ChatGPT plan"]')
  await expect(card.getByText(/ChatGPT \(подписка\)/)).toBeVisible()
  await expect(card.getByText('Основная')).toBeVisible()
  expect(await page.content()).not.toContain('e2e-code')
})
