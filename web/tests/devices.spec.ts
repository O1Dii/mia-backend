import { expect, test, type Page } from '@playwright/test'
import { horizontalOverflow, installBuiltin, signIn } from './support/helpers'

const key = () => ({ 'Idempotency-Key': crypto.randomUUID() })

const REPORT = {
  os: 'Android 15',
  model: 'Samsung SM-S938B',
  app_version: '0.9.0',
  notifications: 'granted',
  capabilities: [
    { capability: 'contacts.read', os_permission: 'denied' },
    { capability: 'alarm.clock', os_permission: 'not_required' },
    { capability: 'apps.open', os_permission: 'not_required' },
    { capability: 'telegram.draft', os_permission: 'not_required' },
  ],
}

/** The Mia app signs in on a phone of the same Google account (bearer, like Android). */
async function phone(page: Page, subject: string, name: string) {
  const id = crypto.randomUUID()
  const nonce = await page.request.post('/v1/auth/google/nonce', { headers: key() })
  const login = await page.request.post('/v1/auth/google', {
    headers: key(),
    data: {
      id_token: `google:${subject}`,
      nonce: ((await nonce.json()) as { nonce: string }).nonce,
      device: { id, platform: 'android', app_version: '0.9.0', device_name: name },
    },
  })
  expect(login.status()).toBe(200)
  const token = ((await login.json()) as { access_token: string }).access_token
  const bearer = { Authorization: `Bearer ${token}` }
  const push = await page.request.put('/v1/devices/push-token', {
    headers: { ...bearer, ...key() },
    data: { device_id: id, provider: 'fcm', token: `fcm-${id}` },
  })
  expect(push.status()).toBe(204)
  return { id, bearer }
}

/** Two phones and this browser: the Galaxy is open now, the Pixel was seen 12 minutes ago. */
async function threeDevices(page: Page) {
  const subject = await signIn(page)
  const galaxy = await phone(page, subject, 'Galaxy S25')
  const beat = await page.request.post('/v1/devices/heartbeat', {
    headers: { ...galaxy.bearer, ...key() },
    data: { report: REPORT },
  })
  expect(beat.status()).toBe(200)
  const declared = await page.request.post('/__e2e/devices/declare', {
    data: {
      device_id: galaxy.id,
      tools: ['device.contacts.search', 'device.alarm.set', 'device.app.open', 'telegram.handoff'],
    },
  })
  expect(((await declared.json()) as { saved: boolean }).saved).toBe(true)
  const pixel = await phone(page, subject, 'Pixel 8')
  await page.request.post('/__e2e/devices/seen', { data: { device_id: pixel.id, minutes_ago: 12 } })
  return { galaxy, pixel }
}

test.describe('«Устройства»', () => {
  test('two phones and this browser are listed with their statuses; the cloud row stands apart', async ({
    page,
  }) => {
    await threeDevices(page)
    await page.goto('/more')
    await page.getByRole('link', { name: /Устройства/ }).click()
    await expect(page).toHaveURL(/\/devices$/)
    await expect(page.getByRole('heading', { name: 'Устройства', level: 1 })).toBeVisible()

    const rows = page.locator('a[data-kind]')
    await expect(rows).toHaveCount(3)
    const browser = rows.nth(0)
    await expect(browser).toHaveAttribute('data-kind', 'browser')
    await expect(browser).toContainText('Это устройство')
    await expect(browser.locator('[data-presence]')).toHaveText('онлайн')

    const galaxy = rows.filter({ hasText: 'Galaxy S25' })
    await expect(galaxy.locator('[data-presence]')).toHaveText('онлайн')
    await expect(galaxy).toContainText('Samsung SM-S938B · Android 15 · Мия 0.9.0')
    await expect(galaxy).toContainText('Возможности: готово 3 из 4')
    const pixel = rows.filter({ hasText: 'Pixel 8' })
    await expect(pixel.locator('[data-presence]')).toHaveText('был(а) 12 мин назад')
    await expect(pixel).not.toContainText('Это устройство')

    const cloud = page.locator('[data-device="cloud"]')
    await expect(cloud).toContainText('Облако Mia')
    await expect(cloud).toContainText('онлайн')
    await expect(cloud).toContainText('даже когда все устройства выключены')
    // Nothing on the screen looks like a phone number.
    const text = await page.locator('main').innerText()
    expect(text).not.toMatch(/\+\d[\d\s()-]{7,}/)
  })

  test('the phone card: capabilities, plugins using them, grant revoke and the access log', async ({
    page,
  }) => {
    const { galaxy } = await threeDevices(page)
    await installBuiltin(page, 'builtin.alarm')
    await page.request.post('/__e2e/devices/grant', { data: { device_id: galaxy.id } })

    await page.goto('/devices')
    await page.locator('a[data-kind]').filter({ hasText: 'Galaxy S25' }).click()
    await expect(page).toHaveURL(new RegExp(`/devices/${galaxy.id}$`))
    await expect(page.getByRole('heading', { name: 'Galaxy S25', level: 1 })).toBeVisible()

    const contacts = page.locator('[data-capability="contacts.read"]')
    await expect(contacts).toContainText('поиск в контактах телефона')
    await expect(contacts).toContainText('нужно разрешение Android')
    await expect(contacts).toContainText('без номеров')
    const alarm = page.locator('[data-capability="alarm.clock"]')
    await expect(alarm).toContainText('готово')
    await expect(alarm.locator('[data-plugin="builtin.alarm"]')).toContainText(
      'Будильник (по желанию)',
    )

    const grant = page.getByRole('list', { name: 'Постоянные разрешения' }).locator('li')
    await expect(grant).toContainText('браузер Chrome на macOS: будильник в «Часах» телефона')
    await grant.getByRole('button', { name: 'Отозвать' }).click()
    await expect(page.getByText('Разрешение отозвано')).toBeVisible()
    await expect(page.getByText(/Нет: каждый запрос с другого устройства/)).toBeVisible()

    await page.getByRole('button', { name: 'Показать журнал доступа' }).click()
    const log = page.getByRole('list', { name: 'Журнал доступа' })
    await expect(log).toContainText('поиск в контактах телефона')
    await expect(log).toContainText('отклонено')
    await expect(log).toContainText('браузер Chrome на macOS')
  })

  test('rename and disconnect a phone; this browser cannot disconnect itself', async ({ page }) => {
    const { pixel } = await threeDevices(page)
    await page.goto(`/devices/${pixel.id}`)
    await page.getByRole('button', { name: 'Переименовать' }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByRole('textbox', { name: 'Имя устройства' }).fill('Старый Pixel')
    await dialog.getByRole('button', { name: 'Сохранить' }).click()
    await expect(page.getByRole('heading', { name: 'Старый Pixel', level: 1 })).toBeVisible()

    await page.getByRole('button', { name: 'Отключить устройство' }).click()
    await expect(page.getByRole('dialog')).toContainText('push на этот телефон больше не придут')
    await page.getByRole('dialog').getByRole('button', { name: 'Отключить', exact: true }).click()
    await expect(page).toHaveURL(/\/devices$/)
    await expect(page.locator('a[data-kind]')).toHaveCount(2)
    await expect(page.locator('a[data-kind]').filter({ hasText: 'Старый Pixel' })).toHaveCount(0)
    // The disconnected phone is signed out.
    const me = await page.request.get('/v1/me', { headers: pixel.bearer })
    expect(me.status()).toBe(401)

    await page.locator('a[data-kind="browser"]').click()
    await expect(page.getByRole('button', { name: 'Отключить устройство' })).toHaveCount(0)
    await expect(page.getByText(/Чтобы отключить его, выйдите из аккаунта/)).toBeVisible()
    await expect(page.getByText(/браузер показывает Мию/)).toBeVisible()
  })

  test('fits a phone screen', async ({ page }) => {
    const { galaxy } = await threeDevices(page)
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/devices')
    await expect(page.locator('a[data-kind]')).toHaveCount(3)
    expect(await horizontalOverflow(page)).toEqual([])
    await page.goto(`/devices/${galaxy.id}`)
    await expect(page.locator('[data-capability="alarm.clock"]')).toBeVisible()
    expect(await horizontalOverflow(page)).toEqual([])
  })
})
