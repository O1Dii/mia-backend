import { readFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'
import { horizontalOverflow, signIn } from './support/helpers'

async function openMemory(page: Page, tab?: string) {
  await page.goto('/memory')
  await expect(page.getByRole('heading', { name: 'Что Мия знает', level: 1 })).toBeVisible()
  if (tab) await page.getByRole('tab', { name: new RegExp(`^${tab}`) }).click()
}

async function importText(page: Page, text: string) {
  await page.getByRole('tab', { name: /^Данные и приватность/ }).click()
  await page.getByRole('button', { name: 'Импортировать' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Содержимое').fill(text)
  await dialog.getByRole('button', { name: 'Отправить на проверку' }).click()
  await expect(page.getByRole('tab', { name: /^Предложено/, selected: true })).toBeVisible()
}

const api = (page: Page) => ({
  facts: async (status = 'active') =>
    (
      (await (await page.request.get(`/v1/memory/facts?status=${status}`)).json()) as {
        items: { text: string }[]
      }
    ).items.map((f) => f.text),
})

test.describe('memory page: what Mia knows', () => {
  test('imported and suggested facts wait for consent: confirm, fix, reject', async ({ page }) => {
    await signIn(page)
    await openMemory(page)
    await expect(page.getByText('Пока ничего не запомнено')).toBeVisible()
    await importText(page, '- Люблю зелёный чай\n- Живу в Минске\n- Не люблю кофе')

    // Nothing is remembered yet: the three lines are only proposals.
    await expect(page.locator('[data-record="fact-suggested"]')).toHaveCount(3)
    expect(await api(page).facts()).toEqual([])

    await page.getByRole('button', { name: 'Запомнить: Люблю зелёный чай' }).click()
    await expect(page.locator('[data-record="fact-suggested"]')).toHaveCount(2)
    expect(await api(page).facts()).toEqual(['Люблю зелёный чай'])

    await page.getByRole('button', { name: 'Изменить предложение: Живу в Минске' }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('Текст').fill('Живу в Бресте')
    await dialog.getByRole('button', { name: 'Сохранить' }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    await expect(page.locator('[data-record="fact-suggested"]')).toHaveCount(1)

    await page.getByRole('button', { name: 'Отклонить: Не люблю кофе' }).click()
    await expect(page.getByText('Новых предложений нет')).toBeVisible()
    expect((await api(page).facts()).sort()).toEqual(['Живу в Бресте', 'Люблю зелёный чай'])
    expect(await api(page).facts('suggested')).toEqual([])

    await page.getByRole('tab', { name: /^Факты/ }).click()
    await expect(page.locator('[data-record="fact"]')).toHaveCount(2)
  })

  test('a fact is added, edited and deleted for good', async ({ page }) => {
    await signIn(page)
    await openMemory(page, 'Факты')
    await page.getByRole('button', { name: 'Добавить факт' }).click()
    let dialog = page.getByRole('dialog')
    await dialog.getByLabel('Текст').fill('Работаю в студии звука')
    await dialog.getByLabel('Раздел').selectOption('work')
    await dialog.getByRole('button', { name: 'Сохранить' }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    const row = page.locator('[data-record="fact"]', { hasText: 'Работаю в студии звука' })
    await expect(row.locator('.mia-chip', { hasText: 'Работа' })).toBeVisible()

    await row.getByRole('button', { name: /^Изменить:/ }).click()
    dialog = page.getByRole('dialog')
    await dialog.getByLabel('Текст').fill('Работаю звукорежиссёром')
    await dialog.getByRole('button', { name: 'Сохранить' }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    await expect(page.getByText('Работаю звукорежиссёром')).toBeVisible()
    expect(await api(page).facts()).toEqual(['Работаю звукорежиссёром'])

    await page.getByRole('button', { name: 'Удалить: Работаю звукорежиссёром' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Удалить насовсем' }).click()
    await expect(page.locator('[data-record="fact"]')).toHaveCount(0)
    await page.reload()
    await expect(page.getByText('Пока ничего не запомнено')).toBeVisible()
    expect(await api(page).facts()).toEqual([])
  })

  test('export downloads the memory as a ZIP archive and as JSON', async ({ page }) => {
    await signIn(page)
    await openMemory(page, 'Факты')
    for (const text of ['Люблю джаз', 'Бегаю по утрам']) {
      await page.getByRole('button', { name: 'Добавить факт' }).click()
      await page.getByRole('dialog').getByLabel('Текст').fill(text)
      await page.getByRole('dialog').getByRole('button', { name: 'Сохранить' }).click()
      await expect(page.getByRole('dialog')).toBeHidden()
      await expect(page.getByText(text)).toBeVisible()
    }
    await page.getByRole('tab', { name: /^Данные и приватность/ }).click()

    const archive = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Скачать архив (ZIP)' }).click()
    const zip = await archive
    expect(zip.suggestedFilename()).toBe('mia-memory.zip')
    const bytes = await readFile(await zip.path())
    expect(bytes.subarray(0, 2).toString('latin1')).toBe('PK')
    expect(bytes.length).toBeGreaterThan(100)

    const jsonFile = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Скачать JSON' }).click()
    const download = await jsonFile
    expect(download.suggestedFilename()).toBe('mia-memory.json')
    const exported = JSON.parse(await readFile(await download.path(), 'utf-8')) as {
      format: string
      facts: { text: string }[]
    }
    expect(exported.format).toBe('mia.memory.v1')
    expect(exported.facts.map((f) => f.text).sort()).toEqual(['Бегаю по утрам', 'Люблю джаз'])

    // The export is in the access log without its content.
    await page.reload()
    await page.getByRole('tab', { name: /^Данные и приватность/ }).click()
    await expect(page.locator('.mw-log li').first()).toBeVisible()
    await expect(page.locator('.mw-log')).not.toContainText('Люблю джаз')
  })

  test('people: add, edit and delete', async ({ page }) => {
    await signIn(page)
    await openMemory(page, 'Люди')
    await page.getByRole('button', { name: 'Добавить человека' }).click()
    let dialog = page.getByRole('dialog')
    await dialog.getByLabel('Имя').fill('Яна')
    await dialog.getByLabel(/Кто это/).fill('жена')
    await dialog.getByRole('button', { name: 'Сохранить' }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    const row = page.locator('[data-record="person"]', { hasText: 'Яна' })
    await expect(row).toContainText('жена')

    await row.getByRole('button', { name: 'Изменить: Яна' }).click()
    dialog = page.getByRole('dialog')
    await dialog.getByLabel(/Другие имена/).fill('Янка, Яночка')
    await dialog.getByRole('button', { name: 'Сохранить' }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    await expect(row.getByText('Янка')).toBeVisible()

    await row.getByRole('button', { name: 'Удалить: Яна' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Удалить насовсем' }).click()
    await expect(page.locator('[data-record="person"]')).toHaveCount(0)
  })

  test('what Mia proposes in a chat shows up for consent, a person included', async ({ page }) => {
    await signIn(page)
    await page.request.post('/v1/model-connections', {
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      data: { provider: 'deepinfra', label: 'GLM', model: 'm', api_key: 'sk-test-abcd1234' },
    })
    await page.goto('/chat')
    await expect(page.getByRole('heading', { name: 'С чего начнём?' })).toBeVisible()
    const field = page.getByRole('textbox', { name: 'Сообщение Мии' })
    await field.fill('Запомни, что Яна — моя жена')
    await field.press('Enter')
    // The chat list previews the answer too: the conversation's bubble is the one that counts.
    await expect(
      page.getByRole('region', { name: 'Чат с Мией' }).getByText(/Предложила запомнить/),
    ).toBeVisible()

    await page.getByRole('link', { name: /^Память/ }).click()
    // The proposal is waiting; the tab opens on it.
    await expect(page.getByRole('tab', { name: /^Предложено/, selected: true })).toBeVisible()
    await expect(page.locator('[data-record="fact-suggested"]')).toContainText('Яна — моя жена')
    expect(await api(page).facts()).toEqual([])

    await page.getByRole('button', { name: /^Запомнить: Яна/ }).click()
    await expect.poll(() => api(page).facts()).toEqual(['Яна — моя жена'])
    await page.getByRole('tab', { name: /^Люди/ }).click()
    await expect(page.locator('[data-record="person"]')).toContainText('Яна')
  })

  test('settings persist: auto-save sections and how long history is kept', async ({ page }) => {
    await signIn(page)
    await openMemory(page, 'Данные и приватность')
    // Health and finance never save by themselves.
    await expect(page.getByRole('checkbox', { name: 'Здоровье' })).toBeDisabled()
    await expect(page.getByRole('checkbox', { name: 'Финансы' })).toBeDisabled()
    const interests = page.getByRole('checkbox', { name: 'Интересы' })
    await interests.check()
    await expect(interests).toBeChecked()
    await page.getByLabel('Как долго хранить историю разговоров').selectOption('30d')
    await expect(page.getByText('Срок хранения сохранён').first()).toBeVisible()

    await page.reload()
    await page.getByRole('tab', { name: /^Данные и приватность/ }).click()
    await expect(page.getByRole('checkbox', { name: 'Интересы' })).toBeChecked()
    await expect(page.getByLabel('Как долго хранить историю разговоров')).toHaveValue('30d')
  })

  test('forget everything erases facts and people, and memory is private per user', async ({
    page,
    browser,
  }) => {
    await signIn(page)
    await openMemory(page, 'Факты')
    await page.getByRole('button', { name: 'Добавить факт' }).click()
    await page.getByRole('dialog').getByLabel('Текст').fill('Секретный факт')
    await page.getByRole('dialog').getByRole('button', { name: 'Сохранить' }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    await expect(page.getByText('Секретный факт')).toBeVisible()

    const other = await browser.newContext({ baseURL: page.url().split('/').slice(0, 3).join('/') })
    const stranger = await other.newPage()
    await signIn(stranger)
    expect(await api(stranger).facts()).toEqual([])
    await other.close()

    await page.getByRole('tab', { name: /^Данные и приватность/ }).click()
    await page.getByRole('button', { name: 'Удалить всю память' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Удалить всё' }).click()
    await expect(page.getByText('Память очищена').first()).toBeVisible()
    await page.getByRole('tab', { name: /^Факты/ }).click()
    await expect(page.getByText('Пока ничего не запомнено')).toBeVisible()
    expect(await api(page).facts()).toEqual([])
  })

  test('the memory page and the data page fit a phone', async ({ page }, info) => {
    await signIn(page)
    await page.setViewportSize({ width: 390, height: 844 })
    await openMemory(page, 'Факты')
    await page.getByRole('button', { name: 'Добавить факт' }).click()
    await page
      .getByRole('dialog')
      .getByLabel('Текст')
      .fill('Очень длинный факт про то, как я люблю гулять по набережной по вечерам')
    await page.getByRole('dialog').getByRole('button', { name: 'Сохранить' }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    await expect(page.locator('[data-record="fact"]')).toHaveCount(1)
    expect(await horizontalOverflow(page)).toEqual([])
    await page.screenshot({ path: info.outputPath('screens/memory-mobile.png'), fullPage: true })
    await page.goto('/data')
    await expect(page.getByRole('heading', { name: 'Где ваши данные', level: 1 })).toBeVisible()
    await expect(page.getByText(/Фактов: 1/)).toBeVisible()
    expect(await horizontalOverflow(page)).toEqual([])
    await page.screenshot({ path: info.outputPath('screens/data-mobile.png'), fullPage: true })
  })
})
