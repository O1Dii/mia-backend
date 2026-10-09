import { expect, test, type Locator, type Page } from '@playwright/test'
import { installTracker, signIn } from './support/helpers'

// M24-R1 (UC-M24-2): on a phone the store's dialogs use most of the screen, scroll inside and keep their
// actions on screen. "Android WebView" is the page as the app shows it on SM-S938B: WebView user agent and
// the CSS viewport the app's WebView has under its native top bar (412 × 727 px, measured on the device).
const ANDROID_WEBVIEW_UA =
  'Mozilla/5.0 (Linux; Android 16; SM-S938B Build/BP4A.251205.006; wv) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Version/4.0 Chrome/153.0.8010.57 Mobile Safari/537.36'

const phones = [
  { name: 'phone 390', viewport: { width: 390, height: 844 } },
  {
    name: 'Android WebView',
    viewport: { width: 412, height: 727 },
    userAgent: ANDROID_WEBVIEW_UA,
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2.625,
  },
]

type Box = { top: number; bottom: number; height: number }

/** The dialog box and whether its main actions are fully inside both the dialog and the viewport. */
async function measure(dialog: Locator, actions: string[]) {
  const box = await dialog.evaluate((el): Box => {
    const r = el.getBoundingClientRect()
    return { top: r.top, bottom: r.bottom, height: r.height }
  })
  const visible: Record<string, boolean> = {}
  for (const name of actions) {
    const button = dialog.getByRole('button', { name, exact: true })
    visible[name] = await button.evaluate((el) => {
      const r = el.getBoundingClientRect()
      const d = el.closest('[role="dialog"]')!.getBoundingClientRect()
      return r.height > 0 && r.top >= d.top && r.bottom <= d.bottom + 0.5 && r.bottom <= innerHeight
    })
  }
  return { box, visible }
}

async function expectTall(page: Page, dialog: Locator, actions: string[], minShare = 0.7) {
  await expect(dialog).toBeVisible()
  // Radix animates the dialog in; measure the settled box.
  await page.waitForTimeout(300)
  const viewport = page.viewportSize()!.height
  const { box, visible } = await measure(dialog, actions)
  expect(box.top, 'dialog starts on screen').toBeGreaterThanOrEqual(0)
  expect(box.bottom, 'dialog ends on screen').toBeLessThanOrEqual(viewport + 0.5)
  expect(box.height, `dialog height ${box.height} of ${viewport}`).toBeGreaterThanOrEqual(
    minShare * viewport,
  )
  for (const name of actions) expect(visible[name], `action «${name}» on screen`).toBe(true)
}

for (const phone of phones) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${phone.name}, ${colorScheme}`, () => {
      const { name: _name, ...options } = phone
      test.use({ ...options, colorScheme })

      test('the install consent uses most of the screen and keeps its actions visible', async ({
        page,
      }, info) => {
        await signIn(page)
        await page.goto('/store/plugin/team.tracker')
        await page.getByRole('button', { name: 'Установить', exact: true }).click()
        const dialog = page.getByRole('dialog')
        await expect(dialog.getByText('create issue')).toBeVisible()
        await expectTall(page, dialog, ['Отмена', 'Установить'])
        // The consent content scrolls inside the dialog, never the page behind it.
        const scrolls = await dialog.evaluate((el) => el.scrollHeight > el.clientHeight)
        if (scrolls) {
          await dialog.evaluate((el) => el.scrollTo(0, el.scrollHeight))
          await expectTall(page, dialog, ['Отмена', 'Установить'])
        }
        await page.screenshot({ path: info.outputPath(`consent-${colorScheme}.png`) })
      })

      test('the remove confirmation fits the phone', async ({ page }, info) => {
        test.skip(
          colorScheme === 'dark',
          'sizes do not depend on the theme; covered by the light run',
        )
        await signIn(page)
        await installTracker(page)
        await page.goto('/installed')
        const card = page.locator('[data-plugin="team.tracker"]')
        await expect(card).toBeVisible()
        await card.getByRole('button', { name: /^Удалить/ }).click()
        const remove = page.getByRole('dialog')
        await expect(remove).toBeVisible()
        await page.waitForTimeout(300)
        const { box, visible } = await measure(remove, ['Удалить'])
        expect(box.top).toBeGreaterThanOrEqual(0)
        expect(box.bottom).toBeLessThanOrEqual(page.viewportSize()!.height + 0.5)
        // A short confirmation is sized by its content, but never collapsed below its own text.
        expect(box.height).toBeGreaterThan(120)
        expect(visible['Удалить']).toBe(true)
        await page.screenshot({ path: info.outputPath('remove.png') })
      })
    })
  }
}
