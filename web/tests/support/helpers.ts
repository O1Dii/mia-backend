import { expect, type Page } from '@playwright/test'

let counter = 0
/** A subject nobody used before: every test gets its own account and workspace. */
export function newSubject(prefix = 'user'): string {
  counter += 1
  return `${prefix}-${Date.now().toString(36)}-${counter}`
}

const key = () => crypto.randomUUID()

/** Sign in the way the browser does after Google: nonce cookie, then the ID-token exchange. */
export async function signIn(page: Page, subject = newSubject()): Promise<string> {
  const nonce = await page.request.post('/v1/auth/web/nonce', {
    headers: { 'Idempotency-Key': key() },
  })
  expect(nonce.status()).toBe(201)
  const login = await page.request.post('/v1/auth/web/google', {
    headers: { 'Idempotency-Key': key() },
    data: { credential: `google:${subject}` },
  })
  expect(login.status()).toBe(200)
  return subject
}

/**
 * The fake provider: a browser opening https://auth.test.example/authorize is answered with the
 * redirect the real provider would send after the user pressed "Allow".
 */
export async function fakeProvider(page: Page) {
  await page.route('https://auth.test.example/**', async (route) => {
    const url = new URL(route.request().url())
    const answer = await page.request.get(`/__e2e/authorize${url.search}`)
    const { location } = (await answer.json()) as { location: string }
    await route.fulfill({ status: 302, headers: { location } })
  })
}

/** Install the Tracker plugin through the store and connect its account (the fake provider). */
export async function installTracker(page: Page) {
  await fakeProvider(page)
  await page.goto('/store/plugin/team.tracker')
  await page.getByRole('button', { name: 'Установить', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Установить', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Аккаунт подключён' })).toBeVisible()
}

export type World = {
  calls: [string, Record<string, unknown>][]
  big_calls: [string, Record<string, unknown>][]
  revocations: unknown[]
  registrations: number
  model_calls: number
}
export async function world(page: Page): Promise<World> {
  return (await (await page.request.get('/__e2e/world')).json()) as World
}

/** Browser speech APIs replaced by scripted fakes; tests drive them through window.__speech. */
export async function fakeSpeech(page: Page) {
  await page.addInitScript(() => {
    type Handler = ((event: unknown) => void) | null
    const state = {
      recognition: null as null | FakeRecognition,
      starts: 0,
      spoken: [] as string[],
      cancelled: 0,
      speaking: false,
    }
    class FakeRecognition {
      lang = ''
      continuous = false
      interimResults = false
      onresult: Handler = null
      onerror: Handler = null
      onend: (() => void) | null = null
      active = false
      start() {
        this.active = true
        state.starts += 1
        state.recognition = this
      }
      stop() {
        this.finish()
      }
      abort() {
        this.active = false
        if (state.recognition === this) state.recognition = null
      }
      emit(transcript: string, isFinal: boolean) {
        this.onresult?.({ results: { length: 1, 0: { isFinal, 0: { transcript } } } })
      }
      finish() {
        if (!this.active) return
        this.active = false
        this.onend?.()
      }
    }
    class FakeUtterance {
      lang = ''
      voice: unknown = null
      onend: (() => void) | null = null
      onerror: ((event: { error: string }) => void) | null = null
      constructor(public text: string) {}
    }
    const queue: FakeUtterance[] = []
    const synthesis = {
      speak(utterance: FakeUtterance) {
        state.spoken.push(utterance.text)
        state.speaking = true
        queue.push(utterance)
      },
      cancel() {
        if (queue.length) state.cancelled += 1
        queue.length = 0
        state.speaking = false
      },
      getVoices: () => [],
    }
    Object.assign(window, {
      SpeechRecognition: FakeRecognition,
      webkitSpeechRecognition: FakeRecognition,
      SpeechSynthesisUtterance: FakeUtterance,
      __speech: {
        state,
        say: (text: string, isFinal = false) => state.recognition?.emit(text, isFinal),
        finish: () => state.recognition?.finish(),
        // The last queued utterance ends: the browser finished reading.
        endSpeech: () => {
          const last = queue.at(-1)
          queue.length = 0
          state.speaking = false
          last?.onend?.()
        },
      },
    })
    Object.defineProperty(window, 'speechSynthesis', { value: synthesis, configurable: true })
  })
}

export async function noRecognition(page: Page) {
  await page.addInitScript(() => {
    Object.assign(window, { SpeechRecognition: undefined, webkitSpeechRecognition: undefined })
  })
}

declare global {
  interface Window {
    __speech: {
      state: { starts: number; spoken: string[]; cancelled: number; speaking: boolean }
      say: (text: string, isFinal?: boolean) => void
      finish: () => void
      endSpeech: () => void
    }
  }
}

/** Empty when the page does not scroll sideways; otherwise the elements that stick out. */
export async function horizontalOverflow(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const width = document.documentElement.clientWidth
    if (document.documentElement.scrollWidth <= width) return []
    return [...document.querySelectorAll('body *')]
      .filter((el) => el.getBoundingClientRect().right > width + 1)
      .slice(0, 8)
      .map((el) => `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 60)}`)
  })
}

/** Install a builtin plugin (no account needed) through the store's consent dialog. */
export async function installBuiltin(page: Page, pluginId: string) {
  await page.goto(`/store/plugin/${pluginId}`)
  await page.getByRole('button', { name: 'Установить', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Установить', exact: true }).click()
  await expect(page.getByText(/^Установлено · /)).toBeVisible()
}

/** Add a tile of an installed plugin from the dashboard's dialog. */
export async function addTile(page: Page, title: string, size: string) {
  await page.goto('/dashboard')
  await page.getByRole('button', { name: 'Добавить плитку' }).first().click()
  await page
    .getByRole('dialog')
    .getByRole('button', { name: new RegExp(`Добавить «${title}», .*`) })
    .filter({ hasText: size })
    .click()
  await expect(page.getByRole('heading', { name: title, level: 2 })).toBeVisible()
}

export function tileFrame(page: Page, title: string) {
  return page.frameLocator(`iframe[title="Плитка: ${title}"]`)
}

/** Call a tile's tool the way its iframe does (through the host endpoint), from the test. */
export async function callTile(
  page: Page,
  installationId: string,
  tileId: string,
  name: string,
  args: Record<string, unknown> = {},
) {
  const response = await page.request.post(
    `/v1/installations/${installationId}/tiles/${tileId}/tool-calls`,
    { headers: { 'Idempotency-Key': crypto.randomUUID() }, data: { name, arguments: args } },
  )
  return { status: response.status(), body: (await response.json()) as Record<string, any> }
}

export async function installationId(page: Page, pluginId: string): Promise<string> {
  const response = await page.request.get('/v1/installations')
  const items = ((await response.json()) as { items: { id: string; plugin_id: string }[] }).items
  const found = items.find((i) => i.plugin_id === pluginId)
  if (!found) throw new Error(`${pluginId} is not installed`)
  return found.id
}
