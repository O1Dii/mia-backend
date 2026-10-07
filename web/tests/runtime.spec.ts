import { test, expect, type Page } from '@playwright/test'
import { createServer, type Server } from 'node:http'
import { readFile } from 'node:fs/promises'

let model: Server
let baseUrl = ''
const received: {
  model: string
  messages: { role: string; content: string }[]
  tools?: unknown[]
}[] = []
test.beforeAll(async () => {
  model = createServer(async (req, res) => {
    let data = ''
    for await (const chunk of req) data += chunk
    const payload = JSON.parse(data)
    received.push(payload)
    const afterTool = payload.messages.at(-1)?.role === 'tool'
    const message =
      payload.tools?.length && !afterTool
        ? {
            role: 'assistant',
            content: null,
            reasoning_content: 'Preserve this continuation',
            tool_calls: [
              {
                id: 'timer-call',
                type: 'function',
                function: {
                  name: 'mia_timer__start',
                  arguments: JSON.stringify({ seconds: 300, label: 'Чай через пять минут' }),
                },
              },
            ],
          }
        : {
            role: 'assistant',
            content: payload.tools?.length
              ? 'Таймер запущен на пять минут.'
              : 'Начните с презентации, затем сделайте перерыв.',
          }
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ choices: [{ message }] }))
  })
  await new Promise<void>((resolve) => model.listen(0, '127.0.0.1', resolve))
  const address = model.address()
  if (!address || typeof address === 'string') throw new Error('Mock failed')
  baseUrl = `http://127.0.0.1:${address.port}/v1`
})
test.afterAll(async () => {
  await new Promise<void>((resolve, reject) => model.close((e) => (e ? reject(e) : resolve())))
})

async function setup(page: Page, plugins: string[] = ['mia.timer']) {
  await page.goto('/')
  await page.waitForFunction(async () => {
    const r = await fetch('/api/web/workspace')
    return (await r.json()).subject_id
  })
  await page.evaluate(
    async ({ baseUrl, plugins }) => {
      const post = async (path: string, body: unknown) => {
        const r = await fetch('/api/web' + path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Mia-Web': '1' },
          body: JSON.stringify(body),
        })
        if (!r.ok) throw new Error(await r.text())
        return r.json()
      }
      const connection = await post('/connections', {
        provider: 'local',
        label: 'Test GLM adapter',
        model: 'selected-glm',
        base_url: baseUrl,
        api_key: '',
      })
      await post('/connections/select', { connection_id: connection.id })
      for (const plugin_id of plugins) await post('/plugins/install', { plugin_id })
    },
    { baseUrl, plugins },
  )
  await page.reload()
}

test('chat runs the selected model, updates dashboard, and preserves widget layout', async ({
  page,
}) => {
  await setup(page, ['mia.timer', 'mia.notes'])
  await page.getByRole('button', { name: 'Диалог с Мией', exact: true }).click()
  await page.getByLabel('Сообщение Мии').fill('Поставь таймер на 5 минут')
  await page.getByRole('button', { name: 'Отправить сообщение' }).click()
  await expect(page.getByText('Таймер запущен на пять минут.', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Дашборд', exact: true }).click()
  const timer = page.locator('[data-plugin="mia.timer"]')
  await expect(timer.locator('.countdown-display')).toHaveText(/0[45]:\d{2}/)
  await expect(timer.getByText('Чай через пять минут')).toBeVisible()
  expect(received.at(-1)?.model).toBe('selected-glm')
  expect(received.at(-1)?.messages.some((m) => m.role === 'tool')).toBe(true)
  await page.getByRole('button', { name: 'Настроить дашборд' }).click()
  await page.getByLabel('Размер Таймер').selectOption('wide')
  await page.getByLabel('Опустить Таймер').click()
  await page.reload()
  await expect(page.locator('.dashboard-tile').last()).toHaveAttribute('data-plugin', 'mia.timer')
  await expect(timer).toHaveClass(/tile-wide/)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('downloadable app pairs in another page, uses current model and shares timer with web', async ({
  page,
  context,
}) => {
  await setup(page)
  await page.getByRole('button', { name: 'Приложения и доступ', exact: true }).click()
  const download = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Скачать приложение + SDK' }).click()
  expect((await download).suggestedFilename()).toBe('mia-timer-app.zip')
  const app = await context.newPage()
  await app.goto('http://127.0.0.1:8001/api/web/sdk/timer.html')
  await app.getByRole('button', { name: 'Подключиться к Мии' }).click()
  await expect(app.locator('#code')).toHaveText(/^[A-F0-9]{8}$/)
  await page.getByLabel('Код приложения').fill((await app.locator('#code').textContent())!)
  await page.getByRole('button', { name: 'Проверить код' }).click()
  await page.getByRole('button', { name: 'Разрешить подключение' }).click()
  await expect(app.getByLabel('Запрос модели')).toBeVisible()
  await app.getByLabel('Запрос модели').fill('Поставь таймер на 5 минут')
  await app.getByRole('button', { name: 'Отправить', exact: true }).click()
  await expect(app.locator('#answer')).toContainText('Таймер запущен')
  await page.getByRole('button', { name: 'Дашборд', exact: true }).click()
  await expect(page.locator('.countdown-display')).toHaveText(/0[45]:\d{2}/)
  await app.getByRole('button', { name: 'Отменить таймер' }).click()
  await expect(page.getByLabel('Активные таймеры')).not.toBeVisible()
  await page.getByRole('button', { name: 'Приложения и доступ', exact: true }).click()
  await page.getByLabel('Отозвать доступ mia.timer').click()
  await expect(app.getByRole('alert')).toContainText('отозвано')
})

test('v1 recipe runs through server prompt action with chosen model', async ({ page }) => {
  await setup(page, [])
  await page.getByRole('button', { name: 'Для авторов', exact: true }).click()
  await page.getByRole('button', { name: 'Рецепт v1' }).click()
  await page.getByRole('button', { name: 'Добавить рецепт', exact: true }).click()
  await page.getByRole('button', { name: 'Добавить плагин', exact: true }).click()
  await page.getByRole('button', { name: 'Дашборд', exact: true }).click()
  await page.getByLabel('Текст для AI-плагина').fill('Подготовить презентацию')
  await page.getByRole('button', { name: 'Запустить рецепт', exact: true }).click()
  await expect(
    page.getByText('Начните с презентации, затем сделайте перерыв.', { exact: true }),
  ).toBeVisible()
  expect(received.at(-1)?.messages[0].content).toContain('Подготовить презентацию')
  expect(received.at(-1)?.messages[0].content).not.toContain('{{input}}')
})

test('author HTML view has a functioning scoped bridge and no ambient storage', async ({
  page,
}) => {
  await setup(page, [])
  const manifest = JSON.parse(
    await readFile('../contracts/plugins/v2/examples/focus-board.json', 'utf8'),
  )
  manifest.view = {
    type: 'html',
    fields: [],
    html: `<h2>Авторский виджет</h2><button id="save">Сохранить через SDK</button><p id="state"></p><p id="isolation"></p><script>
    try {localStorage.setItem('escape','1');document.querySelector('#isolation').textContent='unsafe'} catch {document.querySelector('#isolation').textContent='Изолирован'}
    document.querySelector('#save').onclick=async()=>{await mia.invoke('set_focus',{task:'Сделать web'});const state=await mia.state();document.querySelector('#state').textContent=state.task}
  </script>`,
  }
  await page.getByRole('button', { name: 'Для авторов', exact: true }).click()
  await page.getByLabel('JSON-рецепт плагина').fill(JSON.stringify(manifest))
  await page.getByRole('button', { name: 'Добавить рецепт', exact: true }).click()
  await page.getByRole('button', { name: 'Добавить плагин', exact: true }).click()
  await page.getByRole('button', { name: 'Дашборд', exact: true }).click()
  const frame = page.frameLocator('iframe')
  await expect(frame.getByText('Изолирован', { exact: true })).toBeVisible()
  await frame.getByRole('button', { name: 'Сохранить через SDK' }).click()
  await expect(frame.locator('#state')).toHaveText('Сделать web')
})
