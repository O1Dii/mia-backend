import { apiUrl } from './http'

export type RunEvent = {
  event_id: string
  run_id: string
  sequence: number
  cursor: string
  type: string
  at: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  payload: Record<string, any>
}

const TERMINAL = new Set(['run.completed', 'run.failed', 'run.cancelled'])
const MAX_RECONNECTS = 6

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms)
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer)
        resolve()
      },
      { once: true },
    )
  })
}

export function parseFrames(buffer: string): { events: RunEvent[]; rest: string } {
  const events: RunEvent[] = []
  let rest = buffer
  for (;;) {
    const end = rest.indexOf('\n\n')
    if (end < 0) break
    const block = rest.slice(0, end)
    rest = rest.slice(end + 2)
    const data = block
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n')
    if (!data) continue
    try {
      events.push(JSON.parse(data) as RunEvent)
    } catch {
      // a malformed frame is skipped; the cursor of the next one still resumes correctly
    }
  }
  return { events, rest }
}

/**
 * Follow a run's events (SSE over fetch so the session cookie and Last-Event-ID are under our
 * control). Reconnects with the last cursor after a dropped stream; resolves 'terminal' once the
 * run ended and 'lost' when the stream cannot be recovered (the caller then asks GET /v1/runs/{id}).
 */
export async function followRun(
  runId: string,
  onEvent: (event: RunEvent) => void,
  signal: AbortSignal,
): Promise<'terminal' | 'lost' | 'aborted'> {
  let cursor: string | undefined
  let failures = 0
  const decoder = new TextDecoder()
  while (!signal.aborted) {
    try {
      const response = await fetch(apiUrl(`/v1/runs/${encodeURIComponent(runId)}/events`), {
        headers: { Accept: 'text/event-stream', ...(cursor ? { 'Last-Event-ID': cursor } : {}) },
        credentials: 'same-origin',
        signal,
      })
      if (response.status === 401) window.dispatchEvent(new Event('mia:unauthorized'))
      if (!response.ok || !response.body) {
        if (response.status >= 400 && response.status < 500 && response.status !== 429)
          return 'lost'
        throw new Error('stream unavailable')
      }
      const reader = response.body.getReader()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parsed = parseFrames(buffer)
        buffer = parsed.rest
        for (const event of parsed.events) {
          cursor = event.cursor
          failures = 0
          onEvent(event)
          if (TERMINAL.has(event.type)) {
            void reader.cancel()
            return 'terminal'
          }
        }
      }
    } catch {
      if (signal.aborted) return 'aborted'
    }
    failures++
    if (failures > MAX_RECONNECTS) return 'lost'
    await pause(Math.min(400 * 2 ** failures, 8000), signal)
  }
  return 'aborted'
}
