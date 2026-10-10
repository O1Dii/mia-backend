import { expect, test } from '@playwright/test'
import { signIn, world } from './support/helpers'

// M15-R1: a server with as many tools as Linear's (76) is added by URL, the user picks the tools,
// the chat calls one; an installation the model cannot get is shown with a reason.

test('a 76-tool server: pick tools, consent, see the count, call from chat', async ({ page }) => {
  await signIn(page)
  await page.goto('/store/add-url')
  await page.getByRole('textbox', { name: 'Адрес сервера' }).fill('https://big.test.example/mcp')
  await page.getByRole('button', { name: 'Проверить сервер' }).click()

  await expect(page.getByRole('heading', { name: 'Сервер big.test.example' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Инструменты (76)' })).toBeVisible()
  await expect(page.getByText('Выбрано 58 из 76')).toBeVisible()
  await expect(page.getByText('Удаление и архивирование не отмечены')).toBeVisible()
  const tools = page.getByRole('list', { name: 'Инструменты сервера' })
  await expect(tools.getByRole('checkbox', { name: 'list issues', exact: true })).toBeChecked()
  const remove = tools.getByRole('checkbox', { name: 'delete comment', exact: true })
  await expect(remove).not.toBeChecked()
  await remove.check()
  await expect(page.getByText('Выбрано 59 из 76')).toBeVisible()

  await page.getByRole('button', { name: 'Снять все' }).click()
  await expect(page.getByRole('button', { name: 'Разрешить и добавить' })).toBeDisabled()
  await page.getByRole('button', { name: 'Рекомендуемые' }).click()
  await remove.check()
  await page.getByRole('button', { name: 'Разрешить и добавить' }).click()

  await expect(page).toHaveURL(/\/installed$/)
  const row = page.locator('[data-plugin^="custom."]')
  await expect(row.getByText('Работает')).toBeVisible()
  await expect(row.getByText('Инструментов: 59')).toBeVisible()

  // Re-picking starts from the current choice.
  await row.getByRole('link', { name: 'Выбрать инструменты' }).click()
  await expect(page.getByText('Выбрано 59 из 76')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Сохранить выбор' })).toBeEnabled()
  await page.getByRole('button', { name: 'Отмена' }).click()
  await expect(page).toHaveURL(/\/installed$/)

  await page.goto('/chat')
  // Type only once the history has loaded: a late history load replaces the sent message.
  await expect(page.getByRole('heading', { name: 'С чего начнём?' })).toBeVisible()
  const field = page.getByRole('textbox', { name: 'Сообщение Мии' })
  await field.fill('Покажи список задач')
  await field.press('Enter')
  // M72: the server marks list_issues read-only, so it runs without asking.
  await expect(page.getByText('Вот что нашлось.')).toBeVisible()
  await expect(page.getByRole('region', { name: /Подтверждение/ })).toHaveCount(0)
  expect((await world(page)).big_calls.map(([name]) => name)).toContain('list_issues')
})

test('an installation the model cannot get is shown with the reason, not hidden', async ({
  page,
}) => {
  await signIn(page)
  await page.goto('/store/add-url')
  await page.getByRole('textbox', { name: 'Адрес сервера' }).fill('https://big.test.example/mcp')
  await page.getByRole('button', { name: 'Проверить сервер' }).click()
  await page.getByRole('button', { name: 'Разрешить и добавить' }).click()
  await expect(page).toHaveURL(/\/installed$/)

  // What the API reports for a stored manifest over the limit (the test-server Linear case).
  await page.route('**/v1/installations/*/health', async (route) => {
    const id = route.request().url().split('/').at(-2) ?? ''
    await route.fulfill({
      json: {
        installation_id: id,
        plugin_id: 'custom.u0',
        state: 'needs_attention',
        problem: 'manifest_invalid',
        reason: 'too_many_tools',
        tools: 0,
        declared_tools: 100,
        tool_limit: 96,
      },
    })
  })
  await page.reload()
  const row = page.locator('[data-plugin^="custom."]')
  await expect(row.getByText('Требует внимания')).toBeVisible()
  await expect(row.getByRole('note')).toContainText('Инструментов больше, чем Мия может передать')
  await expect(row.getByRole('link', { name: 'Выбрать инструменты' })).toBeVisible()
})
