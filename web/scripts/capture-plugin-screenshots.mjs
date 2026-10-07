// Real screenshots of built-in plugin interfaces. Uses an isolated, disposable browser session.
import { chromium } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
const output = fileURLToPath(new URL('../public/screenshots/', import.meta.url))
const browser = await chromium.launch()
try {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 1080 },
    reducedMotion: 'reduce',
  })
  const page = await context.newPage()
  await page.goto('http://127.0.0.1:5173/')
  await page.waitForSelector('article')
  const ids = ['timer', 'clock', 'notes', 'focus', 'alarm', 'web', 'digest']
  await page.evaluate(async (ids) => {
    const post = async (path, body) =>
      (
        await fetch('/api/web' + path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Mia-Web': '1' },
          body: JSON.stringify(body),
        })
      ).json()
    for (const id of ids) await post('/plugins/install', { plugin_id: 'mia.' + id })
    for (const text of [
      'Сделать главное дело дня',
      'Выбрать время для прогулки',
      'Записать новую идею',
    ])
      await post('/actions', { name: 'mia_notes__add', arguments: { text } })
  }, ids)
  await mkdir(output, { recursive: true })
  for (const id of ids) {
    await page.goto(`http://127.0.0.1:5173/#plugin/mia.${id}`)
    await page.getByRole('button', { name: 'Открыть плагин', exact: true }).click()
    await page.getByRole('dialog').waitFor()
    await page.evaluate(() => document.fonts.ready)
    await page.getByRole('dialog').screenshot({ path: `${output}/${id}-widget.png` })
    await page.getByLabel('Закрыть', { exact: true }).click()
    await page.evaluate(async (id) => {
      await fetch('/api/web/dashboard', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Mia-Web': '1' },
        body: JSON.stringify({ widgets: [{ id: 'shot', plugin_id: 'mia.' + id, size: 'wide' }] }),
      })
    }, id)
    await page.goto('http://127.0.0.1:5173/#dashboard')
    await page.waitForSelector(`[data-plugin="mia.${id}"]`)
    await page
      .locator(`[data-plugin="mia.${id}"]`)
      .screenshot({ path: `${output}/${id}-dashboard.png` })
  }
  console.log('Captured 14 real plugin screenshots; no model requests or real accounts used.')
} finally {
  await browser.close()
}
