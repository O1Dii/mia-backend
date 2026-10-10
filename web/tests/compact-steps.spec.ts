import { expect, test, type Page } from '@playwright/test'
import {
  fakeSpeech,
  horizontalOverflow,
  installTracker,
  newSubject,
  signIn,
} from './support/helpers'

// M75-R1: the call journal and approvals are compact (a row of chips, a short bar for a pending
// approval), steps come from FeedItem.steps after a reload, the automatic reading equals the
// «Озвучить» button, and a long task result opens in full in the inbox.
const field = (page: Page) => page.getByRole('textbox', { name: 'Сообщение Мии' })
const sizes = [
  { name: '390', width: 390, height: 844 },
  { name: '1440', width: 1440, height: 900 },
]

async function say(page: Page, text: string) {
  await field(page).fill(text)
  await field(page).press('Enter')
}

for (const size of sizes) {
  test.describe(`compact journal at ${size.name} px`, () => {
    test.use({ viewport: { width: size.width, height: size.height } })

    test('a pending approval is a short bar, a decided one a chip, the conversation stays in view', async ({
      page,
    }, info) => {
      await signIn(page)
      await installTracker(page)
      await page.goto('/chat')
      await expect(page.getByRole('heading', { name: 'С чего начнём?' })).toBeVisible()

      for (const [index, title] of ['Первая', 'Вторая', 'Третья'].entries()) {
        await say(page, `Создай задачу ${title}`)
        const approval = page.getByRole('region', { name: new RegExp(`Подтверждение: .*${title}`) })
        await expect(approval).toBeVisible()
        // Short: the arguments are folded, the whole bar is a few lines.
        const box = (await approval.boundingBox())!
        expect(box.height, `approval bar at ${size.name}px`).toBeLessThan(190)
        // The arguments are not in the bar: they are behind the call's chip.
        await expect(approval.locator('details')).toHaveCount(0)
        await expect(approval.getByRole('button', { name: 'Одобрить' })).toBeVisible()
        // The conversation is still on the screen: the question, and the composer below it.
        await expect(page.locator('[data-role="user"]').last()).toBeInViewport()
        await expect(field(page)).toBeInViewport()
        await expect(approval).toBeInViewport()
        expect(await horizontalOverflow(page)).toEqual([])
        if (index === 0)
          await page.screenshot({ path: info.outputPath(`compact-approval-${size.name}.png`) })

        if (index === 0) {
          // The parameters to decide on are one tap away, on the call's chip.
          await page.getByRole('button', { name: 'create issue · ждёт решения' }).click()
          await expect(page.getByRole('definition')).toHaveText('Первая')
          await page.getByRole('button', { name: 'create issue · ждёт решения' }).click()
        }
        await approval.getByRole('button', { name: 'Одобрить' }).click()
        await expect(page.getByText('Готово, задача создана.').last()).toBeVisible()
        // Decided: the bar is gone, a chip remains in the journal of that answer.
        await expect(approval).toHaveCount(0)
        const chips = page.getByRole('list', { name: 'Действия Мии' }).last()
        await expect(chips.locator('.mw-step')).toHaveCount(1)
        await expect(chips.getByRole('button', { name: 'create issue · готово' })).toBeVisible()
      }

      // Three answers, three rows of chips; each journal is a single short line.
      const journals = page.locator('.mw-activity')
      await expect(journals).toHaveCount(3)
      for (const box of await journals.all())
        expect((await box.boundingBox())!.height).toBeLessThan(60)
      await expect(page.locator('[data-role="user"]').last()).toBeInViewport()
      const row = page.getByRole('list', { name: 'Действия Мии' }).last()
      expect(await row.evaluate((el) => getComputedStyle(el).flexWrap)).toBe('nowrap')

      // A tap on the chip opens the details; the decision is told there.
      const chip = row.getByRole('button', { name: 'create issue · готово' })
      await expect(chip).toHaveAttribute('aria-expanded', 'false')
      await expect(page.getByTestId('step-approval')).toHaveCount(0)
      await chip.click()
      await expect(chip).toHaveAttribute('aria-expanded', 'true')
      await expect(page.getByTestId('step-approval')).toContainText('Одобрено')
      await expect(page.getByTestId('step-approval')).toContainText('Третья')
      await page.screenshot({ path: info.outputPath(`compact-open-${size.name}.png`) })
      await chip.click()
      await expect(page.getByTestId('step-approval')).toHaveCount(0)
    })

    test('steps and decided approvals come from the feed after a reload (also on another device)', async ({
      page,
    }, info) => {
      await signIn(page)
      await page.goto('/chat')
      await say(page, 'Привет!')
      await expect(page.locator('.mw-chat [data-role="assistant"]')).toHaveCount(1)
      await expect(page.getByText('тестовый ответ').first()).toBeVisible()

      // What M74-R1 adds to the answer's feed entry (the contract of FeedItem.steps).
      await page.route(/\/v1\/feed(\?|$)/, async (route) => {
        const response = await route.fetch()
        const body = (await response.json()) as { items: Record<string, unknown>[] }
        for (const item of body.items)
          if (item.role === 'assistant')
            item.steps = [
              {
                tool_call_id: 's1',
                tool: 'builtin.internet.search',
                risk: 'read',
                executor: 'server',
                status: 'succeeded',
                arguments: { query: 'курс доллара' },
              },
              {
                tool_call_id: 's2',
                tool: 'builtin.news.refresh',
                risk: 'read',
                executor: 'server',
                status: 'failed',
                error_code: 'tool_execution_failed',
                error_detail: 'Поиск новостей по запросу «ИИ» не удался: источник недоступен.',
              },
              {
                tool_call_id: 's3',
                tool: 'team.tracker.create_issue',
                risk: 'write_external',
                executor: 'server',
                status: 'succeeded',
                arguments: { title: 'Молоко' },
                approval: {
                  id: 'a-3',
                  status: 'approved',
                  summary: 'Создать задачу «Молоко»',
                  decided_at: '2026-10-11T10:00:00Z',
                  channel: 'android',
                  device_id: 'd-1',
                },
              },
              {
                tool_call_id: 's4',
                tool: 'builtin.timer.start',
                risk: 'write_reversible',
                executor: 'device',
                status: 'denied',
                error_code: 'approval_denied',
                approval: {
                  id: 'a-4',
                  status: 'denied',
                  summary: 'Поставить таймер на 5 минут',
                  channel: 'android',
                },
              },
            ]
        await route.fulfill({ response, json: body })
      })
      await page.reload()

      const row = page.getByRole('list', { name: 'Действия Мии' })
      await expect(row.locator('.mw-step')).toHaveCount(4)
      // Collapsed: no reason, arguments or approval text on the screen.
      await expect(page.getByTestId('step-reason')).toHaveCount(0)
      await expect(page.getByTestId('step-approval')).toHaveCount(0)
      await expect(page.getByText('курс доллара')).toHaveCount(0)
      expect(await horizontalOverflow(page)).toEqual([])
      await page.screenshot({ path: info.outputPath(`feed-steps-${size.name}.png`) })

      await row.getByRole('button', { name: 'refresh · не удалось' }).click()
      await expect(page.getByTestId('step-reason')).toContainText('источник недоступен')
      await row.getByRole('button', { name: 'search · готово' }).click()
      await expect(page.getByText('курс доллара')).toBeVisible()
      // An approval decided on the phone shows as decided here.
      await row.getByRole('button', { name: 'create issue · готово' }).click()
      await expect(page.getByTestId('step-approval')).toHaveText(
        'Подтверждение: Одобрено · на телефоне — Создать задачу «Молоко»',
      )
      await row.getByRole('button', { name: 'start · отклонено' }).click()
      await expect(page.getByTestId('step-approval')).toContainText('Отклонено · на телефоне')
      await expect(page.getByTestId('step-reason')).toContainText('Вы отклонили действие.')
    })
  })
}

test.describe('read-aloud: the automatic reading is the same as the button', () => {
  test.beforeEach(async ({ page }) => {
    await fakeSpeech(page)
    await signIn(page, newSubject('speech'))
    await page.goto('/chat')
  })

  test('the whole answer is read, not its short line, by both ways', async ({ page }) => {
    await page.getByRole('button', { name: 'Голосовой ввод' }).click()
    await page.evaluate(() => window.__speech.say('расскажи, несколько абзацев', true))
    await page.evaluate(() => window.__speech.finish())
    await expect(field(page)).toHaveValue('расскажи, несколько абзацев')
    await field(page).press('Enter')
    await expect(page.getByText('Третий абзац: и вот итог.')).toBeVisible()

    const spoken = () => page.evaluate(() => window.__speech.state.spoken.slice())
    await expect.poll(async () => (await spoken()).length).toBeGreaterThan(0)
    const automatic = (await spoken()).join(' ')
    expect(automatic).toContain('Первый абзац')
    expect(automatic).toContain('Второй абзац')
    expect(automatic).toContain('Третий абзац')
    expect(automatic).not.toContain('Коротко')

    await page.evaluate(() => window.__speech.endSpeech())
    const before = (await spoken()).length
    await page.getByRole('button', { name: 'Озвучить ответ' }).click()
    await expect.poll(async () => (await spoken()).length).toBeGreaterThan(before)
    const button = (await spoken()).slice(before).join(' ')
    expect(button).toBe(automatic)

    // After a reload the answer comes from the feed: the button still reads the same.
    await page.evaluate(() => window.__speech.endSpeech())
    await page.reload()
    await expect(page.getByText('Третий абзац: и вот итог.')).toBeVisible()
    const mark = (await spoken()).length
    await page.getByRole('button', { name: 'Озвучить ответ' }).click()
    await expect.poll(async () => (await spoken()).length).toBeGreaterThan(mark)
    expect((await spoken()).slice(mark).join(' ')).toBe(automatic)
  })
})

for (const size of sizes) {
  test.describe(`long task result in the inbox at ${size.name} px`, () => {
    test.use({ viewport: { width: size.width, height: size.height } })

    test('a long report opens in full, in the inbox and in the task list', async ({
      page,
    }, info) => {
      test.setTimeout(120_000)
      await signIn(page)
      await page.goto('/chat')
      await say(page, 'Сделай длинный отчёт в фоне')
      await expect(page.locator('[data-card="job_result"]')).toBeVisible({ timeout: 60_000 })

      await page.goto('/inbox')
      const record = page.locator('[data-record="inbox"]', { hasText: 'длинный отчёт' }).first()
      await expect(record).toBeVisible()
      const text = record.locator('.mw-fold__text')
      await expect(record.locator('.mw-fold')).toHaveAttribute('data-long', 'true')
      const folded = (await text.boundingBox())!.height
      expect(folded).toBeLessThan(220)
      expect(await horizontalOverflow(page)).toEqual([])
      await page.screenshot({ path: info.outputPath(`inbox-folded-${size.name}.png`) })

      await record.getByRole('button', { name: /^Показать полностью/ }).click()
      await expect(record.locator('.mw-fold')).toHaveAttribute('data-open', 'true')
      expect((await text.boundingBox())!.height).toBeGreaterThan(folded * 3)
      await expect(record.getByText('строка')).toHaveCount(40)
      await page.screenshot({ path: info.outputPath(`inbox-open-${size.name}.png`) })
      await record.getByRole('button', { name: /^Свернуть/ }).click()
      expect((await text.boundingBox())!.height).toBeLessThan(220)

      await page.getByRole('tab', { name: /^Задачи/ }).click()
      const job = page.locator('[data-record="job"]', { hasText: 'длинный отчёт' }).first()
      await job.getByRole('button', { name: /^Показать полностью/ }).click()
      await expect(job.getByText('строка')).toHaveCount(40)
    })
  })
}
