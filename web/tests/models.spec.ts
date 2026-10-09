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
