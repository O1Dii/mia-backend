import { expect, test, type Locator, type Page } from '@playwright/test'
import {
  horizontalOverflow,
  installTracker,
  installationId,
  signIn,
  world,
} from './support/helpers'

// Key screens at phone, tablet and desktop widths: no horizontal scroll, screenshots for review.
const widths = [
  { name: '390', width: 390, height: 844 },
  { name: '768', width: 768, height: 1024 },
  { name: '1440', width: 1440, height: 900 },
]

async function seed(page: Page) {
  const writesBefore = (await world(page)).calls.filter(([name]) => name === 'create_issue').length
  await installTracker(page)
  await page.request.post('/v1/model-connections', {
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    data: {
      provider: 'deepinfra',
      label: 'GLM',
      model: 'zai-org/GLM-5.3-Flash',
      api_key: 'sk-test-abcd1234',
    },
  })
  const headers = () => ({ 'Idempotency-Key': crypto.randomUUID() })
  const tracker = await installationId(page, 'team.tracker')
  const layout = (await (await page.request.get('/v1/dashboard')).json()) as {
    expected_version: number
  }
  await page.request.put('/v1/dashboard', {
    headers: headers(),
    data: {
      expected_version: layout.expected_version,
      tiles: [
        {
          id: crypto.randomUUID(),
          installation_id: tracker,
          tile_id: 'issues',
          size: '2x2',
          position: 0,
          mobile: { size: '2x2', position: 0 },
          settings: {},
        },
      ],
    },
  })
  await page.request.post('/v1/memory/facts', {
    headers: headers(),
    data: { text: 'Люблю зелёный чай', category: 'interests' },
  })
  await page.request.post('/v1/memory/import', {
    headers: headers(),
    data: { format: 'text', source: 'chatgpt', content: '- Живу в Минске' },
  })
  await page.goto('/chat')
  const field = page.getByRole('textbox', { name: 'Сообщение Мии' })
  await field.fill('Покажи карточку на сегодня')
  await field.press('Enter')
  await expect(page.getByRole('heading', { name: 'Сегодня', level: 3 })).toBeVisible()
  await field.fill('Создай задачу Позвонить Олегу')
  await field.press('Enter')
  await expect(page.getByRole('region', { name: /Подтверждение: .*Позвонить Олегу/ })).toBeVisible()
  expect((await world(page)).calls.filter(([name]) => name === 'create_issue')).toHaveLength(
    writesBefore,
  )
}

test('key screens fit 390, 768 and 1440 px without horizontal scroll', async ({ page }, info) => {
  test.setTimeout(180_000)
  await signIn(page)
  await seed(page)
  // [name, path, what shows that the screen has its data]
  const screens: [string, string, (page: Page) => Locator][] = [
    ['chat', '/chat', (p) => p.getByRole('heading', { name: 'Сегодня', level: 3 })],
    [
      'dashboard',
      '/dashboard',
      (p) => p.frameLocator('iframe[title="Плитка: Задачи Tracker"]').getByText('Задача A-1'),
    ],
    ['memory', '/memory', (p) => p.getByRole('button', { name: /^Запомнить: Живу в Минске/ })],
    ['inbox', '/inbox', (p) => p.getByRole('heading', { name: 'Входящие и задачи', level: 1 })],
    ['data', '/data', (p) => p.getByText(/Фактов: 1/)],
    ['more', '/more', (p) => p.getByRole('link', { name: /Где ваши данные/ })],
    ['approvals', '/approvals', (p) => p.getByRole('button', { name: 'Одобрить' })],
    ['store', '/store', (p) => p.getByRole('link', { name: 'О плагине Tracker' })],
    ['plugin', '/store/plugin/team.tracker', (p) => p.getByText('Что сможет Мия')],
    ['add-url', '/store/add-url', (p) => p.getByRole('button', { name: 'Проверить сервер' })],
    ['installed', '/installed', (p) => p.locator('[data-plugin="team.tracker"]')],
    ['models', '/models', (p) => p.getByText('GLM', { exact: true })],
  ]
  for (const size of widths) {
    await page.setViewportSize({ width: size.width, height: size.height })
    for (const [name, path, ready] of screens) {
      await page.goto(path)
      await expect(ready(page).first()).toBeVisible()
      expect(await horizontalOverflow(page), `${name} at ${size.name}px`).toEqual([])
      await page.screenshot({
        path: info.outputPath(`screens/${name}-${size.name}.png`),
        fullPage: name !== 'chat',
      })
    }
  }
})

test('touch targets of the shell are at least 44 px on a phone', async ({ page }) => {
  await signIn(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/store')
  const small = await page.evaluate(
    () =>
      [...document.querySelectorAll('nav a, header button, header a')]
        .map((el) => el.getBoundingClientRect())
        .filter((r) => r.height < 44 || r.width < 44).length,
  )
  expect(small).toBe(0)
})

test('on a phone the header keeps the brand and sign-out while the navigation sits at the bottom', async ({
  page,
}) => {
  await signIn(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/store')
  await expect(page.getByRole('link', { name: 'Мия — чат' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Выйти' })).toBeVisible()
  const nav = await page.getByRole('navigation', { name: 'Главная навигация' }).boundingBox()
  expect(nav!.y + nav!.height).toBeGreaterThan(844 - 2)
  await expect(page.getByRole('link', { name: 'Подтверждения' })).toBeVisible()
})

test('the sign-in screen fits every width', async ({ page }, info) => {
  for (const size of widths) {
    await page.setViewportSize({ width: size.width, height: size.height })
    await page.goto('/chat')
    await expect(page.getByRole('heading', { name: /Ваш интеллект/ })).toBeVisible()
    expect(await horizontalOverflow(page), `login at ${size.name}px`).toEqual([])
    await page.screenshot({ path: info.outputPath(`screens/login-${size.name}.png`) })
  }
})
