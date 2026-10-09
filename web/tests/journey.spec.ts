import { expect, test } from '@playwright/test'
import { fakeProvider, signIn, world } from './support/helpers'

test.describe('store journey: install, connect, call in chat, approve, result', () => {
  test('a plugin is installed, its account connected, called from chat after approval', async ({
    page,
  }) => {
    await signIn(page)
    await fakeProvider(page)
    await page.goto('/store')

    await expect(page.getByRole('heading', { name: 'Магазин' })).toBeVisible()
    await page.getByRole('link', { name: 'О плагине Tracker' }).click()
    await expect(page.getByRole('heading', { name: 'Tracker', level: 1 })).toBeVisible()

    // Consent shows tools with risk, grants and the account note before anything is installed.
    await page.getByRole('button', { name: 'Установить', exact: true }).click()
    const consent = page.getByRole('dialog')
    await expect(consent.getByText('create issue')).toBeVisible()
    await expect(consent.getByText('Действие во внешнем сервисе')).toBeVisible()
    await expect(consent.getByText(/войти в аккаунт сервиса/)).toBeVisible()
    await consent.getByRole('button', { name: 'Установить', exact: true }).click()

    // The provider consent happens outside; the callback returns to the store.
    await expect(page).toHaveURL(/\/store\/connected\?plugin=team\.tracker&connection=ok/)
    await expect(page.getByRole('heading', { name: 'Аккаунт подключён' })).toBeVisible()

    await page.getByRole('link', { name: 'Попробовать в чате' }).click()
    const writes = async () =>
      (await world(page)).calls.filter(([name]) => name === 'create_issue').length
    const writesBefore = await writes()
    const field = page.getByRole('textbox', { name: 'Сообщение Мии' })
    await field.fill('Создай задачу Купить молоко')
    await field.press('Enter')

    // The write needs the user's decision and nothing has happened yet.
    const approval = page.getByRole('region', { name: /Подтверждение: .*Купить молоко/ })
    await expect(approval).toBeVisible()
    await expect(approval.getByText('Ждёт вашего решения')).toBeVisible()
    expect(await writes()).toBe(writesBefore)
    await expect(
      page.getByRole('link', { name: /Подтверждения/ }).getByLabel('ожидают: 1'),
    ).toBeVisible()

    await approval.getByRole('button', { name: 'Одобрить' }).click()
    await expect(page.getByText('Готово, задача создана.')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Tracker', level: 3 })).toBeVisible()
    await expect(approval.getByText('Одобрено')).toBeVisible()
    expect((await world(page)).calls).toContainEqual(['create_issue', { title: 'Купить молоко' }])
    expect(await writes()).toBe(writesBefore + 1)

    // The decision is in the approvals history and the badge is gone.
    await page.getByRole('link', { name: 'Подтверждения' }).click()
    await expect(page.getByText('Ничего не ждёт решения')).toBeVisible()
    await expect(
      page.getByRole('region', { name: /Купить молоко/ }).getByText('Одобрено'),
    ).toBeVisible()

    // Installed list: account is shown; disconnect, then uninstall revokes the provider token.
    await page.getByRole('link', { name: 'Мои плагины' }).click()
    const row = page.locator('[data-plugin="team.tracker"]')
    await expect(row.getByText('Работает')).toBeVisible()
    await expect(row.getByText(/аккаунт: auth\.test\.example/)).toBeVisible()
    await row.getByRole('button', { name: 'Удалить Tracker' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Удалить', exact: true }).click()
    await expect(page.getByText('Пока ничего не установлено')).toBeVisible()
    await expect.poll(async () => (await world(page)).revocations.length).toBeGreaterThan(0)
  })

  test('denying the approval leaves the world untouched', async ({ page }) => {
    await signIn(page)
    await fakeProvider(page)
    await page.goto('/store/plugin/team.tracker')
    await page.getByRole('button', { name: 'Установить', exact: true }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Установить', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Аккаунт подключён' })).toBeVisible()

    const before = (await world(page)).calls.length
    await page.goto('/chat')
    const field = page.getByRole('textbox', { name: 'Сообщение Мии' })
    await field.fill('Создай задачу Отменённая')
    await field.press('Enter')
    // Decide on the approvals screen instead of the chat card.
    await page.getByRole('link', { name: /Подтверждения/ }).click()
    const approval = page.getByRole('region', { name: /Подтверждение: .*Отменённая/ })
    await approval.getByRole('button', { name: 'Отклонить' }).click()
    await page.getByRole('link', { name: 'Чат', exact: true }).click()
    await expect(page.getByText('Хорошо, ничего не делаю.')).toBeVisible()
    expect((await world(page)).calls).toHaveLength(before)
  })

  test('another browser cannot finish a connection flow it did not start', async ({
    page,
    browser,
  }) => {
    await signIn(page)
    await page.goto('/store/plugin/team.tracker')
    await page.getByRole('button', { name: 'Установить', exact: true }).click()
    // Capture the provider redirect instead of following it in the owner's browser.
    let callback = ''
    await page.route('https://auth.test.example/**', async (route) => {
      const url = new URL(route.request().url())
      const answer = await page.request.get(`/__e2e/authorize${url.search}`)
      callback = ((await answer.json()) as { location: string }).location
      await route.abort()
    })
    await page.getByRole('dialog').getByRole('button', { name: 'Установить', exact: true }).click()
    await expect.poll(() => callback).not.toBe('')

    const other = await browser.newContext()
    const stranger = await other.newPage()
    await signIn(stranger)
    const baseURL = test.info().project.use.baseURL as string
    await stranger.goto(callback.replace(/^https?:\/\/[^/]+/, baseURL))
    await expect(
      stranger.getByRole('heading', { name: 'Не удалось подключить аккаунт' }),
    ).toBeVisible()
    await expect(stranger.getByText(/открыта не в том браузере/)).toBeVisible()
    await other.close()

    // The owner's flow was burnt too: the installation still waits for an account.
    await page.unroute('https://auth.test.example/**')
    await page.goto('/installed')
    await expect(
      page.locator('[data-plugin="team.tracker"]').getByText('Нужен аккаунт').first(),
    ).toBeVisible()
  })
})

test('a server added by URL: probe, sign in, review tools, consent, use', async ({ page }) => {
  await signIn(page)
  await fakeProvider(page)
  await page.goto('/store')
  await page.getByRole('link', { name: 'Добавить по URL' }).click()
  await page.getByRole('textbox', { name: 'Адрес сервера' }).fill('https://mcp.test.example/mcp')
  await page.getByRole('button', { name: 'Проверить сервер' }).click()

  await expect(page.getByRole('heading', { name: 'Сервер просит войти' })).toBeVisible()
  await page.getByRole('button', { name: 'Подключить аккаунт' }).click()

  await expect(page).toHaveURL(/\/store\/add-url\?installation=.*connection=ok/)
  await expect(page.getByRole('heading', { name: 'Сервер mcp.test.example' })).toBeVisible()
  await expect(page.getByText('list issues')).toBeVisible()
  await expect(page.getByText('create issue')).toBeVisible()
  await expect(page.getByText('Действие во внешнем сервисе').first()).toBeVisible()
  await page.getByRole('button', { name: 'Разрешить и добавить' }).click()

  await expect(page).toHaveURL(/\/installed$/)
  const row = page.locator('[data-plugin^="custom."]')
  await expect(row.getByText('Свой MCP-сервер')).toBeVisible()
  await expect(row.getByText('Работает')).toBeVisible()

  // Every tool of a server added by URL asks first, even the read-only looking ones.
  await page.goto('/chat')
  const field = page.getByRole('textbox', { name: 'Сообщение Мии' })
  await field.fill('Покажи список задач')
  await field.press('Enter')
  const approval = page.getByRole('region', { name: /Подтверждение/ })
  await expect(approval).toBeVisible()
  await approval.getByRole('button', { name: 'Одобрить' }).click()
  await expect(page.getByText('Вот что нашлось.')).toBeVisible()
  expect((await world(page)).calls.map(([name]) => name)).toContain('list_issues')
})

test('an update that asks for more shows the new permission and needs consent', async ({
  page,
}) => {
  await signIn(page)
  await fakeProvider(page)
  await page.goto('/store/plugin/team.updater')
  await page.getByRole('button', { name: 'Установить', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Установить', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Аккаунт подключён' })).toBeVisible()
  await page.goto('/installed')
  const row = page.locator('[data-plugin="team.updater"]')
  await expect(row).toBeVisible()

  await page.request.post('/__e2e/publish-updater-v2')
  await page.reload()
  await expect(row.getByText('Есть обновление 1.1.0')).toBeVisible()
  await row.getByRole('button', { name: 'Обновить' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByText('новое')).toBeVisible()
  await dialog.getByRole('button', { name: 'Обновить' }).click()
  await expect(row.getByText('Версия 1.1.0')).toBeVisible()
})
