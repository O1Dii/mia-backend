import { expect, test, type Page } from '@playwright/test'
import { installTracker, signIn, world } from './support/helpers'

// M17-R1: «Ещё» → «Удалить аккаунт» with a typed confirmation, and the incognito chat.

const field = (page: Page) => page.getByRole('textbox', { name: 'Сообщение Мии' })
const key = () => crypto.randomUUID()

test.describe('account deletion', () => {
  test('cancel keeps the account; typing the word deletes it and revokes the plugin account', async ({
    page,
  }) => {
    await signIn(page)
    await installTracker(page)
    const fact = await page.request.post('/v1/memory/facts', {
      headers: { 'Idempotency-Key': key() },
      data: { text: 'Живёт в Минске', category: 'profile' },
    })
    expect(fact.status()).toBe(201)
    const revokedBefore = (await world(page)).revocations.length

    await page.goto('/more')
    await expect(page.getByRole('heading', { name: 'Аккаунт' })).toBeVisible()
    await page.getByRole('button', { name: 'Удалить аккаунт' }).click()
    const dialog = page.getByRole('dialog', { name: 'Удалить аккаунт?' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText('Это нельзя отменить.')).toBeVisible()
    const confirm = dialog.getByRole('button', { name: 'Удалить навсегда' })
    await expect(confirm).toBeDisabled()

    // Cancel: nothing happens.
    await dialog.getByRole('button', { name: 'Отмена' }).click()
    await expect(dialog).toBeHidden()
    expect((await page.request.get('/v1/me')).status()).toBe(200)

    // A wrong word keeps the button disabled; the right one (any case) enables it.
    await page.getByRole('button', { name: 'Удалить аккаунт' }).click()
    await dialog.getByRole('textbox', { name: 'Введите УДАЛИТЬ' }).fill('удали')
    await expect(confirm).toBeDisabled()
    await dialog.getByRole('textbox', { name: 'Введите УДАЛИТЬ' }).fill('удалить')
    await expect(confirm).toBeEnabled()
    const deletion = page.waitForResponse(
      (r) => r.url().endsWith('/v1/account') && r.request().method() === 'DELETE',
    )
    await confirm.click()
    const answer = await deletion
    expect(answer.status()).toBe(204)
    expect(answer.request().headers()['idempotency-key']).toBeTruthy()

    // Back on the sign-in screen with a note; the old cookie is dead.
    await expect(page.getByText('Аккаунт удалён.', { exact: false })).toBeVisible()
    await expect(
      page.getByRole('heading', { name: 'Ваш интеллект. Ваш ритм жизни.' }),
    ).toBeVisible()
    expect((await page.request.get('/v1/me')).status()).toBe(401)
    expect((await world(page)).revocations.length).toBeGreaterThan(revokedBefore)
  })
})

test.describe('incognito chat', () => {
  test('the toggle sends incognito runs without a chat, shows the sign and leaves no trace', async ({
    page,
  }) => {
    await signIn(page)
    await page.goto('/chat')
    await field(page).fill('Привет!')
    await field(page).press('Enter')
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(1)
    await expect(page.locator('[data-role="assistant"]').last()).toContainText('тестовый ответ')

    const toggle = page.getByRole('button', { name: 'Инкогнито' })
    await expect(toggle).toHaveAttribute('aria-pressed', 'false')
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByRole('heading', { name: 'Инкогнито', level: 1 })).toBeVisible()
    await expect(page.getByText('Мия ничего не запоминает', { exact: false })).toBeVisible()
    await expect(page.locator('[data-role]')).toHaveCount(0)
    await expect(field(page)).toHaveAttribute('placeholder', 'Инкогнито: напишите Мии…')

    const created = page.waitForRequest(
      (r) => r.url().endsWith('/v1/runs') && r.method() === 'POST',
    )
    await field(page).fill('Запомни, что Яна — моя жена')
    await field(page).press('Enter')
    const body = (await created).postDataJSON() as Record<string, unknown>
    expect(body.incognito).toBe(true)
    expect(body).not.toHaveProperty('conversation_id')
    await expect(page.locator('[data-role="assistant"]')).toHaveCount(1)
    await expect(page.getByText('Мия думает…')).toBeHidden()
    // Only the incognito exchange is on screen; the chat's history does not creep back.
    await expect(page.locator('[data-role="user"]')).toHaveCount(1)
    await expect(page.locator('[data-role="user"]')).toContainText('Яна')
    // memory.remember refuses in incognito (memory_incognito): the scripted model says so.
    await expect(page.locator('[data-role="assistant"]').last()).toContainText('Не получилось')

    // Nothing remembered (not even suggested), nothing in the feed.
    const suggested = await (
      await page.request.get('/v1/memory/facts', { params: { status: 'suggested' } })
    ).json()
    expect(suggested.items).toEqual([])
    const feed = JSON.stringify(await (await page.request.get('/v1/feed')).json())
    expect(feed).not.toContain('Яна')

    // Leaving incognito brings the chat back.
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-pressed', 'false')
    await expect(page.getByText('Мия ничего не запоминает', { exact: false })).toBeHidden()
    await expect(page.locator('[data-role="user"]')).toHaveCount(1)
    await expect(page.locator('[data-role="user"]').first()).toContainText('Привет!')
  })

  test('the incognito sign fits a phone screen', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await signIn(page)
    await page.goto('/chat')
    await page.getByRole('button', { name: 'Инкогнито' }).click()
    const banner = page.getByText('Мия ничего не запоминает', { exact: false })
    await expect(banner).toBeVisible()
    const box = await banner.boundingBox()
    expect(box && box.x >= 0 && box.x + box.width <= 390).toBeTruthy()
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    )
    expect(overflow).toBe(false)
  })
})
