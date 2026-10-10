import { expect, test } from '@playwright/test'
import { signIn } from './support/helpers'

// M72: the store's «Где работает» block, device capabilities on the consent screen, and the risk
// of a server added by URL taken from its MCP hints (editable).

for (const [id, name, cloud, capability] of [
  ['builtin.alarm', 'Будильник', 'Будильники хранятся и срабатывают на сервере Mia', 'alarm.clock'],
  ['builtin.timer', 'Таймер', 'Таймеры считает и запускает сервер Mia', 'alarm.clock'],
  ['builtin.telegram', 'Telegram', 'Бот Mia присылает результаты', 'telegram.draft'],
] as const) {
  test(`${name}: «Где работает» and the phone capability`, async ({ page }) => {
    await signIn(page)
    await page.goto(`/store/plugin/${id}`)
    const where = page.getByRole('region', { name: 'Где работает' })
    await expect(where).toBeVisible()
    await expect(where.locator('[data-where="cloud"]')).toContainText('В облаке Mia')
    await expect(where.locator('[data-where="cloud"]')).toContainText(cloud)
    await expect(where.locator('[data-where="device"]')).toContainText('На телефоне — по запросу')
    const device = where.getByRole('region', { name: 'Возможности устройства' })
    await expect(device.locator(`[data-capability="${capability}"]`)).toBeVisible()

    // The consent screen lists the same capabilities before anything is installed.
    await page.getByRole('button', { name: 'Установить', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByRole('region', { name: 'Возможности устройства' })).toBeVisible()
    await expect(dialog.locator(`[data-capability="${capability}"]`)).toContainText(
      'Модель получит',
    )
  })
}

test('a cloud-only plugin says the phone is not involved', async ({ page }) => {
  await signIn(page)
  await page.goto('/store/plugin/builtin.notes')
  const where = page.getByRole('region', { name: 'Где работает' })
  await expect(where.locator('[data-where="cloud"]')).toContainText('В облаке Mia')
  await expect(where.locator('[data-where="device"]')).toContainText('Телефон не участвует')
  await expect(where.getByRole('region', { name: 'Возможности устройства' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Установить', exact: true }).click()
  await expect(
    page.getByRole('dialog').getByRole('region', { name: 'Возможности устройства' }),
  ).toHaveCount(0)
})

test('a server by URL: risk comes from its hints and the user can change it', async ({ page }) => {
  await signIn(page)
  await page.goto('/store/add-url')
  await page.getByRole('textbox', { name: 'Адрес сервера' }).fill('https://big.test.example/mcp')
  await page.getByRole('button', { name: 'Проверить сервер' }).click()
  await expect(page.getByRole('heading', { name: 'Сервер big.test.example' })).toBeVisible()

  const risk = (tool: string) => page.getByLabel(`Риск: ${tool}`, { exact: true })
  await expect(risk('list issues')).toHaveValue('read')
  await expect(risk('delete comment')).toHaveValue('write_external')
  await expect(risk('create issue')).toHaveValue('write_external') // no hint: not trusted
  await expect(page.getByText('Чтение сервер выполняет без вопросов')).toBeVisible()

  // The user trusts create_issue to be reversible; the permission list follows the choice.
  const permissions = page
    .getByRole('heading', { name: 'Разрешения' })
    .locator('xpath=following-sibling::ul[1]')
  await expect(permissions).not.toContainText('Менять данные плагина')
  await risk('create issue').selectOption('write_reversible')
  await expect(permissions).toContainText('Менять данные плагина')

  await page.getByRole('button', { name: 'Разрешить и добавить' }).click()
  await expect(page).toHaveURL(/\/installed$/)

  // The saved choice is what the re-pick screen proposes.
  await page
    .locator('[data-plugin^="custom."]')
    .getByRole('link', { name: 'Выбрать инструменты' })
    .click()
  await expect(risk('create issue')).toHaveValue('write_reversible')
  await expect(risk('list issues')).toHaveValue('read')
})
