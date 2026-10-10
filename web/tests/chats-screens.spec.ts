import { expect, test, type Page } from '@playwright/test'
import {
  callTile,
  horizontalOverflow,
  installBuiltin,
  installationId,
  signIn,
} from './support/helpers'

// M75: chats and the «Сейчас» strip at phone, tablet and desktop widths. Screenshots go to
// test-results/screens/ (not committed).
const widths = [
  { name: '390', width: 390, height: 844 },
  { name: '768', width: 768, height: 1024 },
  { name: '1440', width: 1440, height: 900 },
]
const field = (page: Page) => page.getByRole('textbox', { name: 'Сообщение Мии' })
const list = (page: Page) => page.getByRole('complementary', { name: 'Чаты' })

test('the chat screen fits 390, 768 and 1440 px: list, strip, table, call journal', async ({
  page,
}, info) => {
  test.setTimeout(120_000)
  await signIn(page)
  await installBuiltin(page, 'builtin.timer')
  const timer = await installationId(page, 'builtin.timer')
  await callTile(page, timer, 'active-timers', 'start', { seconds: 900, label: 'Паста' })
  await callTile(page, timer, 'active-timers', 'start', {
    seconds: 3000,
    label: 'Созвон с командой',
  })

  await page.goto('/chat')
  await field(page).fill('Покажи проекты таблицей')
  await field(page).press('Enter')
  await expect(page.getByRole('table')).toBeVisible()
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(
    'Модельное покажи проекты таблицей',
  )
  // A second, pinned chat so that the list has more than one row.
  const viewport = page.viewportSize()!
  const open = async () => {
    if (viewport.width < 1024)
      await page.getByRole('button', { name: 'Список чатов', exact: true }).click()
  }
  await open()
  await list(page).getByRole('button', { name: 'Новый чат', exact: true }).click()
  await field(page).fill('Привет!')
  await field(page).press('Enter')
  await expect(page.locator('.mw-chat [data-role="assistant"]')).toHaveCount(1)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Модельное привет')
  await page.getByRole('button', { name: 'Закрепить чат' }).click()
  await expect(page.getByRole('button', { name: 'Открепить чат' })).toBeVisible()

  for (const size of widths) {
    await page.setViewportSize({ width: size.width, height: size.height })
    await page.goto('/chat')
    await expect(page.getByRole('region', { name: 'Сейчас' }).getByText('Паста')).toBeVisible()
    expect(await horizontalOverflow(page), `chat at ${size.name}px`).toEqual([])
    await page.screenshot({ path: info.outputPath(`screens/chats-${size.name}.png`) })

    // The chat list: a column on a wide screen, a drawer below 1024 px.
    if (size.width < 1024) {
      await expect(list(page)).toBeHidden()
      await page.getByRole('button', { name: 'Список чатов', exact: true }).click()
    }
    await expect(list(page)).toBeVisible()
    // The drawer has slid in completely.
    await expect.poll(async () => (await list(page).boundingBox())?.x).toBeGreaterThanOrEqual(0)
    await expect(list(page).locator('li[data-chat-id]')).toHaveCount(2)
    await expect(list(page).getByLabel('Хранить чаты')).toBeVisible()
    expect(await horizontalOverflow(page), `chat list at ${size.name}px`).toEqual([])
    await page.screenshot({ path: info.outputPath(`screens/chat-list-${size.name}.png`) })
    if (size.width < 1024) {
      // Touch targets of the list and the header are at least 44 px.
      const small = await page.evaluate(
        () =>
          [
            ...document.querySelectorAll(
              '.mw-chats button, .mw-chats select, .mw-chat__head button',
            ),
          ]
            .filter((el) => (el as HTMLElement).offsetParent !== null)
            .map((el) => el.getBoundingClientRect())
            .filter((r) => r.height < 43.5 || (r.width < 43.5 && r.width > 0)).length,
      )
      expect(small).toBe(0)
      await list(page).getByRole('button', { name: 'Закрыть список чатов' }).click()
      await expect(list(page)).toBeHidden()
    }
  }

  // The table of the first chat on a phone: scrolls inside its box.
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: 'Список чатов', exact: true }).click()
  await list(page)
    .locator('li[data-chat-id]', { hasText: 'таблицей' })
    .getByRole('button')
    .first()
    .click()
  await expect(list(page)).toBeHidden()
  await expect(page.getByRole('table')).toBeVisible()
  expect(await horizontalOverflow(page), 'table at 390px').toEqual([])
  await page.screenshot({ path: info.outputPath('screens/chat-table-390.png') })
})
