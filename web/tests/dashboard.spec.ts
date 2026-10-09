import { expect, test } from '@playwright/test'
import {
  addTile,
  callTile,
  horizontalOverflow,
  installBuiltin,
  installTracker,
  installationId,
  signIn,
  tileFrame,
  world,
} from './support/helpers'

test.describe('dashboard: tiles are MCP Apps in sandboxed iframes', () => {
  test('an empty dashboard points at the store', async ({ page }) => {
    await signIn(page)
    await page.goto('/dashboard')
    await expect(page.getByRole('heading', { name: 'На дашборде пока пусто' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Открыть магазин' })).toBeVisible()
  })

  test('the timer tile counts down and takes commands without the model', async ({ page }) => {
    await signIn(page)
    await installBuiltin(page, 'builtin.timer')
    await addTile(page, 'Таймеры', '2x1')

    const tile = tileFrame(page, 'Таймеры')
    const modelCalls = (await world(page)).model_calls
    await expect(tile.getByText('Нет таймеров')).toBeVisible()

    // A command from the iframe: tools/call → host → backend router → timer plugin, no LLM.
    await tile.getByRole('button', { name: '5 мин' }).click()
    const time = tile.locator('.time').first()
    await expect(time).toHaveText(/^0?[45]:\d\d$/)
    const first = await time.innerText()
    await expect.poll(async () => time.innerText(), { timeout: 8000 }).not.toBe(first)
    await expect(tile.getByText('Таймер 5 мин')).toBeVisible()

    // Pause freezes the countdown; resume continues it.
    await tile.getByRole('button', { name: '⏸' }).click()
    await expect(tile.getByRole('button', { name: '▶' })).toBeVisible()
    const frozen = await time.innerText()
    await page.waitForTimeout(2200)
    expect(await time.innerText()).toBe(frozen)
    await tile.getByRole('button', { name: '▶' }).click()
    await expect.poll(async () => time.innerText(), { timeout: 8000 }).not.toBe(frozen)

    // The timer lives on the server: it is still there after a reload.
    await page.reload()
    await expect(tileFrame(page, 'Таймеры').getByText('Таймер 5 мин')).toBeVisible()
    expect((await world(page)).model_calls).toBe(modelCalls)
  })

  test('a timer started elsewhere shows up on refresh, again without the model', async ({
    page,
  }) => {
    await signIn(page)
    await installBuiltin(page, 'builtin.timer')
    await addTile(page, 'Таймеры', '2x1')
    const modelCalls = (await world(page)).model_calls
    const tile = tileFrame(page, 'Таймеры')
    await expect(tile.getByText('Нет таймеров')).toBeVisible()

    const id = await installationId(page, 'builtin.timer')
    const started = await callTile(page, id, 'active-timers', 'start', {
      seconds: 600,
      label: 'Из другого окна',
    })
    expect(started.status).toBe(200)
    await expect(tile.getByText('Из другого окна')).toHaveCount(0)

    // The host runs the manifest's refresh tool (get) and pushes ui/notifications/tool-result.
    await page.getByRole('button', { name: 'Обновить плитку Таймеры' }).click()
    await expect(tile.getByText('Из другого окна')).toBeVisible()
    expect((await world(page)).model_calls).toBe(modelCalls)
  })

  test('a tile refreshes by itself on the manifest schedule', async ({ page }) => {
    await page.clock.install()
    await signIn(page)
    await installBuiltin(page, 'builtin.timer')
    await addTile(page, 'Таймеры', '2x1')
    const tile = tileFrame(page, 'Таймеры')
    await expect(tile.getByText('Нет таймеров')).toBeVisible()
    const id = await installationId(page, 'builtin.timer')
    await callTile(page, id, 'active-timers', 'start', { seconds: 900, label: 'Сам обновился' })
    // The manifest asks for a refresh every 30 s while the tab is visible.
    await page.clock.runFor(31_000)
    await expect(tile.getByText('Сам обновился')).toBeVisible()
  })

  test('layout: resize, move and remove tiles; it survives a reload', async ({ page }) => {
    await signIn(page)
    await installBuiltin(page, 'builtin.timer')
    await installBuiltin(page, 'builtin.notes')
    await addTile(page, 'Таймеры', '2x1')
    await addTile(page, 'Заметки', '2x2')

    const timers = page.locator('[data-tile="active-timers"]')
    await expect(timers).toHaveAttribute('data-size', '2x1')
    await page.getByRole('button', { name: 'Изменить', exact: true }).click()
    const group = page.getByRole('group', { name: 'Настройки плитки Таймеры' })
    await group.getByRole('combobox').selectOption('1x1')
    await expect(timers).toHaveAttribute('data-size', '1x1')
    await group.getByRole('button', { name: /Сдвинуть «Таймеры» позже/ }).click()
    await expect(page.locator('.mw-tiles h2').first()).toHaveText('Заметки')

    await page.reload()
    await expect(page.locator('[data-tile="active-timers"]')).toHaveAttribute('data-size', '1x1')
    await expect(page.locator('.mw-tiles h2').first()).toHaveText('Заметки')

    await page.getByRole('button', { name: 'Изменить', exact: true }).click()
    await page.getByRole('button', { name: 'Убрать плитку «Таймеры»' }).click()
    await expect(page.locator('[data-tile="active-timers"]')).toHaveCount(0)
    await page.reload()
    await expect(page.locator('[data-tile="notes"]').or(page.locator('.mw-tiles li'))).toHaveCount(
      1,
    )
  })

  test('a remote plugin tile works through the account connection', async ({ page }) => {
    await signIn(page)
    await installTracker(page)
    await addTile(page, 'Задачи Tracker', '2x2')
    const tile = tileFrame(page, 'Задачи Tracker')
    // The first data arrives by the manifest's refresh tool (list_issues on the remote server).
    await expect(tile.getByText('Задача A-1')).toBeVisible()
    await expect(tile.getByText('Задача A-2')).toBeVisible()
  })

  test('mobile: tiles stack in one column without sideways scroll', async ({ page }, info) => {
    await signIn(page)
    await installBuiltin(page, 'builtin.timer')
    await addTile(page, 'Таймеры', '2x1')
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/dashboard')
    await expect(tileFrame(page, 'Таймеры').getByText('Нет таймеров')).toBeVisible()
    expect(await horizontalOverflow(page)).toEqual([])
    const box = await page.locator('[data-tile="active-timers"]').boundingBox()
    expect(box!.width).toBeGreaterThan(300)
    await page.screenshot({ path: info.outputPath('screens/dashboard-mobile.png'), fullPage: true })
  })
})

test.describe('isolation: a hostile tile cannot reach the host', () => {
  test('the iframe has an opaque origin and no access to host cookies, DOM or the API', async ({
    page,
  }) => {
    await signIn(page)
    await installTracker(page)
    await addTile(page, 'Проба изоляции', '2x2')
    // A cookie readable by host scripts, the kind an attacker would want.
    await page.evaluate(() => {
      document.cookie = 'host_marker=top-secret; path=/'
    })

    const frame = page.locator('iframe[title="Плитка: Проба изоляции"]')
    await expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
    const probe = tileFrame(page, 'Проба изоляции')
    await expect(probe.locator('body[data-done="true"]')).toBeAttached({ timeout: 15_000 })
    const report = JSON.parse(await probe.locator('#report').innerText()) as Record<string, string>

    expect(report.origin).toBe('null')
    for (const key of [
      'parentDocument',
      'parentRoot',
      'parentCookie',
      'ownCookie',
      'localStorage',
      'topLocation',
    ])
      expect(report[key], key).toMatch(/^blocked:/)
    expect(JSON.stringify(report)).not.toContain('top-secret')
    expect(report.fetchMe).toBe('blocked')
    // Everything the tile does goes through the host bridge, and the host says no.
    expect(report.writeExternal).toMatch(/^refused:/)
    expect(report.foreignTool).toMatch(/^refused:/)
    expect(report.rawMethod).toBe('error:-32601')
    expect((await world(page)).calls.filter(([name]) => name === 'create_issue')).toHaveLength(0)

    // The host page itself is untouched by the tile.
    await expect(page.locator('#root')).toBeVisible()
  })

  test('the host refuses write_external from a tile at the API too', async ({ page }) => {
    await signIn(page)
    await installTracker(page)
    const id = await installationId(page, 'team.tracker')
    const refused = await callTile(page, id, 'issues', 'create_issue', { title: 'x' })
    expect(refused.status).toBe(403)
    expect(refused.body.error.code).toBe('tool_permission_denied')
    const foreign = await callTile(page, id, 'issues', 'memory.remember', { text: 'x' })
    expect(foreign.status).toBe(404)
    expect((await world(page)).calls.filter(([name]) => name === 'create_issue')).toHaveLength(0)
  })

  test("another user's tile is not reachable", async ({ page, browser }) => {
    await signIn(page)
    await installBuiltin(page, 'builtin.timer')
    const id = await installationId(page, 'builtin.timer')
    const other = await browser.newContext({ baseURL: page.url().split('/').slice(0, 3).join('/') })
    const stranger = await other.newPage()
    await signIn(stranger)
    const response = await stranger.request.get(`/v1/installations/${id}/tiles/active-timers`)
    expect(response.status()).toBe(404)
    await other.close()
  })
})
