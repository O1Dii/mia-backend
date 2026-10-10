import { expect, test, type Locator, type Page } from '@playwright/test'
import { fakeSpeech, horizontalOverflow, newSubject, signIn } from './support/helpers'

// M60 scenario 10 (web half): chat with voice input and read-aloud, one style at 390 / 768 / 1440 px
// in the light and the dark theme. The browser speech APIs are scripted fakes (helpers.fakeSpeech);
// live microphones and voices are the user check. The model is the scripted e2e model.

const widths = [
  { name: '390', width: 390, height: 844 },
  { name: '768', width: 768, height: 1024 },
  { name: '1440', width: 1440, height: 900 },
]

const field = (page: Page) => page.getByRole('textbox', { name: 'Сообщение Мии' })

type Style = {
  font: string
  accent: string
  bg: string
  composerRadius: string
  sendBackground: string
  sendRadius: string
}

async function styleOf(page: Page): Promise<Style> {
  await field(page).fill('проверка стиля')
  const style = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement)
    const composer = document.querySelector('.mw-composer')!
    const send = document.querySelector('button[aria-label="Отправить сообщение"]')!
    return {
      font: getComputedStyle(document.body).fontFamily,
      accent: root.getPropertyValue('--mia-color-accent').trim(),
      bg: root.getPropertyValue('--mia-color-bg').trim(),
      composerRadius: getComputedStyle(composer).borderRadius,
      sendBackground: getComputedStyle(send).backgroundColor,
      sendRadius: getComputedStyle(send).borderRadius,
    }
  })
  await field(page).fill('')
  return style
}

const screens: [string, string, (page: Page) => Locator][] = [
  ['store', '/store', (p) => p.getByRole('heading', { level: 1 }).first()],
  ['dashboard', '/dashboard', (p) => p.getByRole('button', { name: 'Добавить плитку' }).first()],
  ['memory', '/memory', (p) => p.getByRole('heading', { level: 1 }).first()],
  ['inbox', '/inbox', (p) => p.getByRole('heading', { name: 'Входящие и задачи', level: 1 })],
  ['more', '/more', (p) => p.getByRole('link', { name: /Где ваши данные/ })],
]

for (const colorScheme of ['light', 'dark'] as const) {
  test(`scenario 10: voice chat and one style at 390/768/1440 px (${colorScheme})`, async ({
    page,
  }, info) => {
    test.setTimeout(180_000)
    await page.emulateMedia({ colorScheme })
    await fakeSpeech(page)
    await signIn(page, newSubject(`mvp-${colorScheme}`))
    const styles: Record<string, Style> = {}

    for (const size of widths) {
      await page.setViewportSize({ width: size.width, height: size.height })
      await page.goto('/chat')
      await expect(field(page)).toBeVisible()

      // Dictation: microphone → text in the field → send.
      await page.getByRole('button', { name: 'Голосовой ввод' }).click()
      await expect(page.getByRole('button', { name: 'Остановить диктовку' })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
      await page.evaluate(() => window.__speech.say('расскажи, несколько абзацев', true))
      await page.evaluate(() => window.__speech.finish())
      await expect(field(page)).toHaveValue('расскажи, несколько абзацев')
      await field(page).press('Enter')
      await expect(page.getByText('Третий абзац: и вот итог.').last()).toBeVisible()

      // A dictated question is read aloud automatically — the whole answer (display), not the
      // short `speech` line (M75-R1).
      const spoken = () => page.evaluate(() => window.__speech.state.spoken.join(' '))
      await expect.poll(spoken).toContain('Третий абзац')
      expect(await spoken()).toContain('Первый абзац')
      expect(await spoken()).not.toContain('Коротко')

      // «Остановить» stops the reading; «Озвучить» reads the answer again.
      await page.getByRole('button', { name: 'Остановить озвучку' }).click()
      expect(await page.evaluate(() => window.__speech.state.cancelled)).toBeGreaterThan(0)
      const readAgain = page.getByRole('button', { name: 'Озвучить ответ' }).last()
      await expect(readAgain).toBeVisible()
      const before = await page.evaluate(() => window.__speech.state.spoken.length)
      await readAgain.click()
      await expect
        .poll(() => page.evaluate(() => window.__speech.state.spoken.length))
        .toBeGreaterThan(before)
      await page.evaluate(() => window.__speech.endSpeech())
      await expect(page.getByRole('button', { name: 'Озвучить ответ' }).last()).toBeVisible()

      // The composer stays on screen and the chat does not scroll sideways.
      const composer = await page.locator('.mw-composer').boundingBox()
      expect(composer!.y + composer!.height).toBeLessThanOrEqual(size.height + 0.5)
      expect(composer!.x + composer!.width).toBeLessThanOrEqual(size.width + 0.5)
      expect(await horizontalOverflow(page), `chat at ${size.name}px`).toEqual([])
      styles[size.name] = await styleOf(page)
      await page.screenshot({
        path: info.outputPath(`mvp/${colorScheme}-chat-${size.name}.png`),
      })

      for (const [name, path, ready] of screens) {
        await page.goto(path)
        await expect(ready(page)).toBeVisible()
        expect(await horizontalOverflow(page), `${name} at ${size.name}px`).toEqual([])
        await page.screenshot({
          path: info.outputPath(`mvp/${colorScheme}-${name}-${size.name}.png`),
          fullPage: true,
        })
      }
    }

    // One style: the same fonts, colours and shapes at every width.
    const [phone, ...others] = widths.map((w) => styles[w.name])
    for (const other of others) expect(other).toEqual(phone)
    expect(phone.font).toContain('Manrope')
    expect(phone.accent).not.toBe('')
    expect(phone.composerRadius).not.toBe('0px')
  })
}

test('scenario 10: light and dark themes share the accent family and differ in background', async ({
  page,
}) => {
  await signIn(page)
  const read = async (colorScheme: 'light' | 'dark') => {
    await page.emulateMedia({ colorScheme })
    await page.goto('/chat')
    await expect(field(page)).toBeVisible()
    return page.evaluate(() => {
      const root = getComputedStyle(document.documentElement)
      return {
        bg: root.getPropertyValue('--mia-color-bg').trim(),
        text: root.getPropertyValue('--mia-color-text').trim(),
        font: getComputedStyle(document.body).fontFamily,
      }
    })
  }
  const light = await read('light')
  const dark = await read('dark')
  expect(light.font).toEqual(dark.font)
  expect(light.bg).not.toEqual(dark.bg)
  expect(light.text).not.toEqual(dark.text)
})
