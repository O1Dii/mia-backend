import { useCallback, useEffect, useRef, useState } from 'react'
import { AlarmClock, Bell, Hourglass, LoaderCircle, Timer, X } from 'lucide-react'
import { api, type NowItem } from '../lib/api'
import { useChat } from '../lib/chat'
import { messageOf } from '../lib/http'
import { Link } from '../lib/router'

const REFRESH_MS = 30_000
/** While a background task runs its end is awaited more closely. */
const REFRESH_JOBS_MS = 4_000

type Snapshot = {
  items: NowItem[]
  /** Server clock minus browser clock when the page arrived: countdowns follow the server. */
  skew: number
}

/** `m:ss`, or `h:mm:ss` from an hour up; never negative. */
export function countdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

function leftOf(item: NowItem, now: number, skew: number): number | null {
  if (item.kind === 'job') return null
  if (item.state === 'paused') return item.remaining_ms
  const deadline = item.deadline_at ? Date.parse(item.deadline_at) : NaN
  return Number.isNaN(deadline) ? item.remaining_ms : deadline - (now + skew)
}

const kindName: Record<NowItem['kind'], string> = {
  timer: 'Таймер',
  alarm: 'Будильник',
  focus: 'Фокус',
  job: 'Фоновая задача',
}
const jobState: Record<string, string> = {
  queued: 'в очереди',
  running: 'выполняется',
  waiting_approval: 'ждёт подтверждения',
}
const clock = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' })

function KindIcon({ kind }: { kind: NowItem['kind'] }) {
  if (kind === 'alarm') return <AlarmClock size={18} aria-hidden="true" />
  if (kind === 'focus') return <Hourglass size={18} aria-hidden="true" />
  if (kind === 'job') return <LoaderCircle size={18} className="spin" aria-hidden="true" />
  return <Timer size={18} aria-hidden="true" />
}

/**
 * «Сейчас» (M75): active timers, alarms, focus sessions and background tasks of the account.
 * Countdowns tick in the browser from `deadline_at`; the list is re-read on focus, after a run
 * and every ~30 s, and a finished task also brings its result card into the open chat.
 */
export function NowStrip() {
  const chat = useChat()
  const { activity, activeId, syncTail, refreshChats } = chat
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const jobs = useRef(new Set<string>())
  const expired = useRef(new Set<string>())

  const refresh = useCallback(async () => {
    try {
      const page = await api.now()
      const ids = new Set(page.items.filter((i) => i.kind === 'job').map((i) => i.id))
      const finished = [...jobs.current].some((id) => !ids.has(id))
      jobs.current = ids
      expired.current = new Set()
      setSnapshot({ items: page.items, skew: Date.parse(page.server_time) - Date.now() })
      if (finished) {
        void syncTail()
        void refreshChats()
      }
    } catch {
      // The strip is a convenience: a failed read keeps what is shown.
    }
  }, [refreshChats, syncTail])

  useEffect(() => {
    void refresh()
  }, [refresh, activity, activeId])

  // A short task may start and end between two reads: its card is looked for after every run.
  useEffect(() => {
    if (!activity) return
    void syncTail()
    const timer = window.setTimeout(() => void syncTail(), 6000)
    return () => window.clearTimeout(timer)
  }, [activity, syncTail])

  const hasJobs = snapshot?.items.some((i) => i.kind === 'job') ?? false
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    const timer = window.setInterval(onVisible, hasJobs ? REFRESH_JOBS_MS : REFRESH_MS)
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
    }
  }, [refresh, hasJobs])

  const ticking = snapshot?.items.some((i) => i.kind !== 'job' && i.state !== 'paused') ?? false
  useEffect(() => {
    if (!ticking) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    setNow(Date.now())
    return () => window.clearInterval(timer)
  }, [ticking])

  const skew = snapshot?.skew ?? 0
  const items = (snapshot?.items ?? []).filter((item) => {
    const left = leftOf(item, now, skew)
    return left === null || left > 0
  })

  // A timer that ran out disappears at once; the server is asked once more to be sure.
  useEffect(() => {
    if (!snapshot) return
    const gone = snapshot.items.filter((i) => {
      const left = leftOf(i, now, skew)
      return left !== null && left <= 0 && !expired.current.has(i.id)
    })
    if (!gone.length) return
    for (const item of gone) expired.current.add(item.id)
    window.setTimeout(() => void refresh(), 1500)
  }, [now, snapshot, skew, refresh])

  async function cancel(item: NowItem) {
    setBusy(item.id)
    setError('')
    try {
      if (item.cancel.type === 'job') await api.cancelJob(item.cancel.job_id)
      else {
        const result = await api.tileCall(
          item.cancel.installation_id,
          item.cancel.tile_id,
          item.cancel.name,
          item.cancel.arguments,
        )
        if (result.is_error) throw new Error('tile call failed')
      }
      setSnapshot((s) => (s ? { ...s, items: s.items.filter((i) => i.id !== item.id) } : s))
      await refresh()
    } catch (e) {
      setError(messageOf(e, 'Не удалось отменить. Попробуйте ещё раз.'))
    } finally {
      setBusy(null)
    }
  }

  if (!items.length && !error) return null
  return (
    <section className="mw-now" aria-label="Сейчас">
      <h2 className="mw-now__title">
        <Bell size={14} aria-hidden="true" /> Сейчас
      </h2>
      <ul className="mw-now__list">
        {items.map((item) => {
          const left = leftOf(item, now, skew)
          const name = item.label?.trim() || kindName[item.kind]
          const elsewhere = item.kind === 'job' && item.conversation_id
          return (
            <li key={`${item.kind}-${item.id}`} className="mw-now__item" data-kind={item.kind}>
              <KindIcon kind={item.kind} />
              <div className="mw-now__text">
                <span className="mw-now__label" title={name}>
                  {name}
                </span>
                <span className="mw-now__meta">
                  {left !== null ? (
                    <>
                      <span role="timer" className="mw-now__time" data-state={item.state}>
                        {countdown(left)}
                      </span>
                      {item.state === 'paused' && ' · пауза'}
                      {item.kind === 'alarm' &&
                        item.deadline_at &&
                        ` · в ${clock.format(new Date(item.deadline_at))}`}
                      {item.kind !== 'alarm' &&
                        item.label?.trim() &&
                        ` · ${kindName[item.kind].toLowerCase()}`}
                    </>
                  ) : (
                    <>
                      {kindName.job.toLowerCase()} · {jobState[item.state] ?? item.state}
                      {elsewhere && item.conversation_id !== activeId && (
                        <>
                          {' · '}
                          <Link to={`/chat/${item.conversation_id}`} className="mw-link">
                            чат
                          </Link>
                        </>
                      )}
                    </>
                  )}
                </span>
              </div>
              <button
                type="button"
                className="mia-icon-btn mw-now__cancel"
                aria-label={`Отменить: ${name}`}
                disabled={busy === item.id}
                onClick={() => void cancel(item)}
              >
                {busy === item.id ? <LoaderCircle size={16} className="spin" /> : <X size={16} />}
              </button>
            </li>
          )
        })}
      </ul>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
