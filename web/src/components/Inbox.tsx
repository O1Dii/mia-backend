import { useCallback, useEffect, useState } from 'react'
import {
  CalendarClock,
  Check,
  CirclePause,
  CirclePlay,
  Inbox as InboxIcon,
  ListChecks,
  LoaderCircle,
  Play,
  Plus,
  Trash2,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, type InboxItem, type Job, type JobStatus, type Schedule } from '../lib/api'
import { errorText } from '../lib/errors'
import { when } from '../lib/format'
import { messageOf } from '../lib/http'
import { Link } from '../lib/router'
import { DisplayView } from './ChatParts'
import { Dialog } from './Dialog'

type Tab = 'inbox' | 'jobs' | 'schedules'

const POLL_MS = 8000

const jobStatus: Record<JobStatus, { text: string; className: string }> = {
  queued: { text: 'В очереди', className: '' },
  running: { text: 'Выполняется', className: 'mia-chip--accent' },
  waiting_approval: { text: 'Ждёт подтверждения', className: 'mia-chip--warning' },
  succeeded: { text: 'Готово', className: 'mia-chip--success' },
  failed: { text: 'Не удалось', className: 'mia-chip--danger' },
  cancelled: { text: 'Отменена', className: '' },
}
const activeJob: JobStatus[] = ['queued', 'running', 'waiting_approval']

const days: Record<string, string> = {
  MO: 'пн',
  TU: 'вт',
  WE: 'ср',
  TH: 'чт',
  FR: 'пт',
  SA: 'сб',
  SU: 'вс',
}

/** «Каждый день в 09:00» from an RFC 5545 rule; unknown shapes fall back to the rule itself. */
export function describeRule(rule: string): string {
  const line = rule.split('\n').find((l) => l.startsWith('RRULE:')) ?? rule
  const parts = Object.fromEntries(
    line
      .replace(/^RRULE:/, '')
      .split(';')
      .map((p) => p.split('=') as [string, string]),
  )
  const interval = Number(parts.INTERVAL ?? 1)
  const hour = parts.BYHOUR !== undefined ? String(parts.BYHOUR).padStart(2, '0') : null
  const minute = String(parts.BYMINUTE ?? '0').padStart(2, '0')
  const at = hour ? ` в ${hour}:${minute}` : ''
  switch (parts.FREQ) {
    case 'MINUTELY':
      return interval === 1 ? 'Каждую минуту' : `Каждые ${interval} мин`
    case 'HOURLY':
      return interval === 1 ? 'Каждый час' : `Каждые ${interval} ч`
    case 'DAILY':
      return (interval === 1 ? 'Каждый день' : `Каждые ${interval} дн.`) + at
    case 'WEEKLY': {
      const list = parts.BYDAY
        ? ` (${String(parts.BYDAY)
            .split(',')
            .map((d) => days[d.replace(/[^A-Z]/g, '')] ?? d)
            .join(', ')})`
        : ''
      return (interval === 1 ? 'Каждую неделю' : `Раз в ${interval} нед.`) + list + at
    }
    case 'MONTHLY':
      return 'Каждый месяц' + at
    case 'YEARLY':
      return 'Каждый год' + at
    default:
      return rule
  }
}

/** Unread inbox items for the navigation badge; refreshes when the inbox screen changes it. */
export function useUnreadInbox(): number {
  const [count, setCount] = useState(0)
  useEffect(() => {
    let alive = true
    const refresh = () =>
      void api.inbox(true).then(
        (page) => alive && setCount(page.items.length),
        () => undefined,
      )
    refresh()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh()
    }, 30_000)
    window.addEventListener('mia:inbox', refresh)
    return () => {
      alive = false
      window.clearInterval(timer)
      window.removeEventListener('mia:inbox', refresh)
    }
  }, [])
  return count
}

/** Inbox and background work: what Mia did while you were away, tasks and their schedules. */
export function Inbox() {
  const [tab, setTab] = useState<Tab>('inbox')
  const [inbox, setInbox] = useState<InboxItem[] | null>(null)
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [schedules, setSchedules] = useState<Schedule[] | null>(null)
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState('')

  const reload = useCallback(async () => {
    try {
      const [items, jobList, scheduleList] = await Promise.all([
        api.inbox(),
        api.jobs(),
        api.schedules(),
      ])
      setInbox(items.items)
      window.dispatchEvent(new Event('mia:inbox'))
      setJobs(jobList.items)
      setSchedules(scheduleList.items)
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить данные.'))
    }
  }, [])
  useEffect(() => {
    void reload()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void reload()
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [reload])

  async function act(id: string, action: () => Promise<unknown>, done: string) {
    setBusy(id)
    try {
      await action()
      toast.success(done)
      await reload()
    } catch (e) {
      toast.error(messageOf(e))
    } finally {
      setBusy('')
    }
  }

  const unread = inbox?.filter((item) => !item.read_at).length ?? 0
  const activeJobs = jobs?.filter((job) => activeJob.includes(job.status)).length ?? 0
  const tabs: { id: Tab; label: string; count: number }[] = [
    { id: 'inbox', label: 'Входящие', count: unread },
    { id: 'jobs', label: 'Задачи', count: activeJobs },
    { id: 'schedules', label: 'Расписания', count: 0 },
  ]
  const shownInbox = (inbox ?? []).filter((item) => !unreadOnly || !item.read_at)

  return (
    <section className="mw-page" aria-labelledby="inbox-title">
      <header className="mw-page__head">
        <h1 id="inbox-title">Входящие и задачи</h1>
        <p>Что Мия сделала в фоне, пока вас не было: ответы на задачи, напоминания и расписания.</p>
      </header>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      <div className="mw-tabs" role="tablist" aria-label="Разделы">
        {tabs.map((item) => (
          <button
            key={item.id}
            role="tab"
            id={`inbox-tab-${item.id}`}
            aria-selected={tab === item.id}
            aria-controls="inbox-panel"
            className={`mw-tab ${tab === item.id ? 'is-current' : ''}`}
            onClick={() => setTab(item.id)}
          >
            {item.label}
            {item.count > 0 && (
              <b className="mw-tab__count" aria-label={`: ${item.count}`}>
                {item.count}
              </b>
            )}
          </button>
        ))}
      </div>
      <div
        id="inbox-panel"
        role="tabpanel"
        aria-labelledby={`inbox-tab-${tab}`}
        className="mw-stack"
      >
        {inbox === null && !error && (
          <p className="mw-muted" role="status">
            <LoaderCircle size={16} className="spin" /> Загружаем…
          </p>
        )}

        {inbox && tab === 'inbox' && (
          <>
            <label className="mw-switch">
              <input
                type="checkbox"
                checked={unreadOnly}
                onChange={(event) => setUnreadOnly(event.target.checked)}
              />
              <span>Только непрочитанные</span>
            </label>
            {shownInbox.length === 0 ? (
              <div className="mw-empty mia-glass">
                <InboxIcon size={28} aria-hidden="true" />
                <p>
                  {unreadOnly
                    ? 'Непрочитанного нет.'
                    : 'Пока пусто. Результаты фоновых задач и напоминания появятся здесь.'}
                </p>
              </div>
            ) : (
              <ul className="mw-stack">
                {shownInbox.map((item) => (
                  <li
                    key={item.id}
                    className={`mia-glass mw-record ${item.read_at ? '' : 'is-unread'}`}
                    data-record="inbox"
                    data-read={item.read_at ? 'true' : 'false'}
                  >
                    <div className="mw-record__body">
                      <h2 className="mw-record__title">{item.title}</h2>
                      <p className="mw-muted">
                        {when(item.created_at)} ·{' '}
                        {
                          {
                            job: 'Задача',
                            schedule: 'Расписание',
                            plugin: 'Плагин',
                            system: 'Мия',
                          }[item.source.type]
                        }
                        {item.read_at ? '' : ' · новое'}
                      </p>
                      <DisplayView display={item.response.display} text={item.response.speech} />
                    </div>
                    {!item.read_at && (
                      <div className="mw-record__actions">
                        <button
                          className="mia-btn mia-btn--secondary"
                          aria-label={`Прочитано: ${item.title}`}
                          disabled={busy === item.id}
                          onClick={() => void act(item.id, () => api.markRead(item.id), 'Отмечено')}
                        >
                          <Check size={16} aria-hidden="true" /> Прочитано
                        </button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}

        {jobs && tab === 'jobs' && (
          <>
            <div className="mw-actions">
              <button className="mia-btn mia-btn--secondary" onClick={() => setCreating(true)}>
                <Plus size={16} aria-hidden="true" /> Новая задача
              </button>
            </div>
            {jobs.length === 0 ? (
              <div className="mw-empty mia-glass">
                <ListChecks size={28} aria-hidden="true" />
                <p>
                  Фоновых задач пока нет. Попросите Мию «проверь это и напиши мне позже» или
                  создайте задачу сами.
                </p>
              </div>
            ) : (
              <ul className="mw-stack">
                {jobs.map((job) => {
                  const status = jobStatus[job.status]
                  return (
                    <li
                      key={job.id}
                      className="mia-glass mw-record"
                      data-record="job"
                      data-status={job.status}
                    >
                      <div className="mw-record__body">
                        <p className="mw-record__text">{job.goal}</p>
                        <div className="mw-chips">
                          <span className={`mia-chip ${status.className}`}>{status.text}</span>
                          <span className="mw-muted">{when(job.created_at)}</span>
                        </div>
                        {job.status === 'waiting_approval' && (
                          <Link to="/approvals" className="mw-link">
                            Открыть подтверждения
                          </Link>
                        )}
                        {job.error && (
                          <p className="mw-error">{errorText(job.error.code, job.error.message)}</p>
                        )}
                        {job.result && (
                          <DisplayView display={job.result.display} text={job.result.speech} />
                        )}
                      </div>
                      {activeJob.includes(job.status) && (
                        <div className="mw-record__actions">
                          <button
                            className="mia-btn mia-btn--ghost"
                            aria-label={`Отменить задачу: ${job.goal}`}
                            disabled={busy === job.id}
                            onClick={() =>
                              void act(job.id, () => api.cancelJob(job.id), 'Отмена запрошена')
                            }
                          >
                            <X size={16} aria-hidden="true" /> Отменить
                          </button>
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </>
        )}

        {schedules &&
          tab === 'schedules' &&
          (schedules.length === 0 ? (
            <div className="mw-empty mia-glass">
              <CalendarClock size={28} aria-hidden="true" />
              <p>
                Расписаний нет. Скажите Мие «каждое утро в 9 присылай мне сводку», и оно появится
                здесь.
              </p>
            </div>
          ) : (
            <ul className="mw-stack">
              {schedules.map((schedule) => {
                const paused = schedule.status === 'paused'
                return (
                  <li
                    key={schedule.id}
                    className="mia-glass mw-record"
                    data-record="schedule"
                    data-status={schedule.status}
                  >
                    <div className="mw-record__body">
                      <p className="mw-record__text">{schedule.goal}</p>
                      <div className="mw-chips">
                        <span className="mia-chip">{describeRule(schedule.rule)}</span>
                        <span
                          className={`mia-chip ${paused ? 'mia-chip--warning' : 'mia-chip--success'}`}
                        >
                          {paused ? 'На паузе' : 'Работает'}
                        </span>
                        {schedule.kind === 'reminder' && (
                          <span className="mia-chip">Напоминание</span>
                        )}
                      </div>
                      <p className="mw-muted">
                        {paused ? 'Пауза' : `Следующий запуск: ${when(schedule.next_run_at)}`} ·
                        последний: {when(schedule.last_run_at)} · {schedule.timezone}
                      </p>
                    </div>
                    <div className="mw-record__actions">
                      <button
                        className="mia-btn mia-btn--primary"
                        aria-label={`Запустить сейчас: ${schedule.goal}`}
                        disabled={busy === schedule.id}
                        onClick={() =>
                          void act(
                            schedule.id,
                            () => api.runSchedule(schedule.id),
                            'Запущено. Результат придёт во входящие.',
                          ).then(() => setTab('jobs'))
                        }
                      >
                        <Play size={16} aria-hidden="true" /> Запустить сейчас
                      </button>
                      <button
                        className="mia-btn mia-btn--secondary"
                        aria-label={`${paused ? 'Возобновить' : 'Поставить на паузу'}: ${schedule.goal}`}
                        disabled={busy === schedule.id}
                        onClick={() =>
                          void act(
                            schedule.id,
                            () =>
                              api.patchSchedule(schedule.id, {
                                status: paused ? 'active' : 'paused',
                              }),
                            paused ? 'Расписание возобновлено' : 'Расписание на паузе',
                          )
                        }
                      >
                        {paused ? (
                          <CirclePlay size={16} aria-hidden="true" />
                        ) : (
                          <CirclePause size={16} aria-hidden="true" />
                        )}{' '}
                        {paused ? 'Возобновить' : 'Пауза'}
                      </button>
                      <button
                        className="mia-btn mia-btn--ghost"
                        aria-label={`Удалить расписание: ${schedule.goal}`}
                        disabled={busy === schedule.id}
                        onClick={() =>
                          void act(
                            schedule.id,
                            () => api.deleteSchedule(schedule.id),
                            'Расписание удалено',
                          )
                        }
                      >
                        <Trash2 size={16} aria-hidden="true" /> Удалить
                      </button>
                    </div>
                  </li>
                )
              })}
            </ul>
          ))}
      </div>
      {creating && (
        <NewJob
          close={() => setCreating(false)}
          onCreated={async () => {
            setCreating(false)
            toast.success('Задача поставлена в очередь')
            await reload()
          }}
        />
      )}
    </section>
  )
}

function NewJob({ close, onCreated }: { close: () => void; onCreated: () => Promise<void> }) {
  const [goal, setGoal] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit() {
    setBusy(true)
    setError('')
    try {
      await api.createJob(goal.trim())
      await onCreated()
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }
  return (
    <Dialog
      title="Новая фоновая задача"
      description="Мия выполнит её в фоне и пришлёт результат во входящие."
      onClose={close}
    >
      <form
        className="mw-form"
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        <label>
          <span>Что сделать</span>
          <textarea
            value={goal}
            rows={4}
            maxLength={4000}
            required
            onChange={(event) => setGoal(event.target.value)}
          />
        </label>
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
        <div className="mw-dialog__actions">
          <button type="button" className="mia-btn mia-btn--secondary" onClick={close}>
            Отмена
          </button>
          <button className="mia-btn mia-btn--primary" disabled={busy || !goal.trim()}>
            {busy && <LoaderCircle size={16} className="spin" />} Поставить в очередь
          </button>
        </div>
      </form>
    </Dialog>
  )
}
