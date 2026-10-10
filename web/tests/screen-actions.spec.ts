import { expect, test, type Page } from '@playwright/test'
import { installBuiltin, signIn, world } from './support/helpers'

// M41-R1: a tool's screen-only action (the one-time Telegram bot link) is a button in the chat,
// live and after a reload, and never reaches the model.
const BOT_LINK = 'https://t.me/mia_e2e_bot?start=E2E-ONE-TIME-TOKEN'
const field = (page: Page) => page.getByRole('textbox', { name: 'Сообщение Мии' })

async function ask(page: Page, text: string) {
  await page.goto('/chat')
  await expect(page.getByRole('heading', { name: 'С чего начнём?' })).toBeVisible()
  await field(page).fill(text)
  await field(page).press('Enter')
  await expect(page.locator('[data-role="assistant"]').last()).toContainText(
    'Открой ссылку на экране',
  )
}

async function feedWritten(page: Page, count: number) {
  await expect
    .poll(
      async () =>
        ((await (await page.request.get('/v1/feed')).json()) as { items: unknown[] }).items.length,
    )
    .toBe(count)
}

test.describe('screen-only actions from tool results', () => {
  test('«подключи telegram бота» shows «Открыть Telegram» with the link, also after a reload', async ({
    page,
  }) => {
    await signIn(page)
    await installBuiltin(page, 'builtin.telegram')
    const leaksBefore = (await world(page)).link_leaks
    await ask(page, 'Подключи telegram бота')

    const reply = page.locator('[data-role="assistant"]').last()
    await expect(reply.getByText('connect bot · готово')).toBeVisible()
    const button = reply.getByRole('link', { name: 'Открыть Telegram' })
    await expect(button).toBeVisible()
    await expect(button).toHaveAttribute('href', BOT_LINK)
    await expect(button).toHaveAttribute('target', '_blank')
    await expect(button).toHaveAttribute('rel', /noopener/)
    // The answer text itself has no link: the model never saw it.
    await expect(reply.locator('.mia-bubble')).not.toContainText('t.me')

    await feedWritten(page, 2)
    await page.reload()
    const restored = page.locator('[data-role="assistant"]').last()
    await expect(restored.getByRole('link', { name: 'Открыть Telegram' })).toHaveAttribute(
      'href',
      BOT_LINK,
    )
    expect((await world(page)).link_leaks).toBe(leaksBefore)
  })

  test('an expired link renders a disabled button, live and from the feed', async ({ page }) => {
    await signIn(page)
    await installBuiltin(page, 'builtin.telegram')
    await ask(page, 'Подключи бота по просроченной ссылке')

    const reply = page.locator('[data-role="assistant"]').last()
    const stale = reply.getByRole('button', { name: /Открыть Telegram · ссылка устарела/ })
    await expect(stale).toBeDisabled()
    await expect(reply.getByRole('link', { name: 'Открыть Telegram' })).toHaveCount(0)

    await feedWritten(page, 2)
    await page.reload()
    const restored = page.locator('[data-role="assistant"]').last()
    await expect(
      restored.getByRole('button', { name: /Открыть Telegram · ссылка устарела/ }),
    ).toBeDisabled()
    // The feed no longer hands out the expired link at all.
    const feed = (await (await page.request.get('/v1/feed')).json()) as {
      items: { actions?: { url?: string; expired?: boolean }[] }[]
    }
    expect(feed.items[0].actions).toEqual([expect.objectContaining({ expired: true })])
    expect(feed.items[0].actions?.[0].url).toBeUndefined()
  })
})
