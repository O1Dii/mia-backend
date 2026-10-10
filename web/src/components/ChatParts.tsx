import { useEffect, useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ExternalLink,
  Inbox as InboxIcon,
  LoaderCircle,
  ShieldAlert,
  Smartphone,
  X,
} from 'lucide-react'
import type { Approval, Display, FeedCard, Risk, ScreenAction } from '../lib/api'
import { messageOf } from '../lib/http'
import { Markdown } from '../lib/markdown'
import { Link } from '../lib/router'
import type { ToolStep } from '../lib/chat'
import { InlineApp } from '../plugins/InlineApp'

export const riskLabel: Record<Risk, string> = {
  read: 'Чтение',
  write_reversible: 'Обратимое изменение',
  write_external: 'Действие во внешнем сервисе',
}

const riskShort: Record<Risk, string> = {
  read: 'Чтение',
  write_reversible: 'Изменение',
  write_external: 'Внешнее действие',
}

export function toolName(qualified: string): string {
  const last = qualified.split('.').at(-1) ?? qualified
  return last.replace(/_/g, ' ')
}

export function DisplayView({ display, text }: { display: Display | null; text: string }) {
  if (!display || display.kind === 'text') return <Markdown text={text} />
  if (display.kind === 'card')
    return (
      <article className="mia-card mia-glass mw-display">
        {display.title && <h3 className="mia-card__title">{display.title}</h3>}
        <div className="mia-card__body">
          <Markdown text={display.markdown} />
        </div>
      </article>
    )
  if (display.kind === 'app') return <InlineApp display={display} />
  const safe = /^https:\/\//.test(display.url)
  return (
    <article className="mia-card mia-glass mw-display">
      <h3 className="mia-card__title">{display.title}</h3>
      {safe && (
        <div className="mia-card__actions">
          <a
            className="mia-btn mia-btn--secondary"
            href={display.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            <ExternalLink size={16} /> Открыть
          </a>
        </div>
      )}
    </article>
  )
}

const stepStatus: Record<ToolStep['status'], string> = {
  running: 'выполняется',
  succeeded: 'готово',
  failed: 'не удалось',
  denied: 'отклонено',
  expired: 'время вышло',
  uncertain: 'результат неизвестен',
  cancelled: 'отменено',
}

function argumentLines(args: Record<string, unknown>): [string, string][] {
  return Object.entries(args).map(([key, value]) => [
    key,
    typeof value === 'string' ? value : JSON.stringify(value),
  ])
}

const decidedBy: Record<string, string> = {
  web: 'в браузере',
  android: 'на телефоне',
  voice_trusted: 'голосом',
}

/** One approval as the journal needs it: from the live stream (`full`) or from the feed. */
type ApprovalView = {
  id: string
  status: Approval['status']
  summary: string
  tool: string
  channel?: string
  /** The whole approval; only the live stream has it, and only with it the user can decide here. */
  full?: Approval
}

function viewOfApproval(approval: Approval): ApprovalView {
  return {
    id: approval.id,
    status: approval.status,
    summary: approval.summary,
    tool: approval.tool,
    channel: approval.decision?.channel,
    full: approval,
  }
}

/** One chip of the journal: a tool call, its approval, or an approval whose call is not known. */
type Entry = { id: string; tool: string; step?: ToolStep; approval?: ApprovalView }

/** Steps and approvals of one answer merged by tool call, in call order. */
export function activityOf(steps: ToolStep[], approvals: Approval[]): Entry[] {
  const byCall = new Map(approvals.map((a) => [a.tool_call_id, a]))
  const used = new Set<string>()
  const entries: Entry[] = steps.map((step) => {
    const live = byCall.get(step.id)
    if (live) used.add(live.id)
    const fromFeed = step.approval
    return {
      id: step.id,
      tool: step.tool,
      step,
      approval: live
        ? viewOfApproval(live)
        : fromFeed
          ? {
              id: fromFeed.id,
              status: fromFeed.status,
              summary: fromFeed.summary,
              tool: step.tool,
              channel: fromFeed.channel,
            }
          : undefined,
    }
  })
  for (const approval of approvals)
    if (!used.has(approval.id))
      entries.push({ id: approval.id, tool: approval.tool, approval: viewOfApproval(approval) })
  return entries
}

function entryLabel(entry: Entry): string {
  if (entry.approval?.status === 'pending') return 'ждёт решения'
  if (entry.step) return stepStatus[entry.step.status]
  return statusText[entry.approval?.status ?? 'cancelled'].toLowerCase()
}

function entryTone(entry: Entry): string {
  if (entry.approval?.status === 'pending') return 'mia-chip--accent'
  const status = entry.step?.status
  if (status === 'succeeded') return 'mia-chip--success'
  if (status === 'running') return ''
  if (status) return 'mia-chip--warning'
  return entry.approval?.status === 'approved' ? 'mia-chip--success' : 'mia-chip--warning'
}

function EntryIcon({ entry }: { entry: Entry }) {
  if (entry.approval?.status === 'pending') return <ShieldAlert size={12} aria-hidden="true" />
  const status = entry.step?.status
  if (status === 'running') return <LoaderCircle size={12} className="spin" aria-hidden="true" />
  if (status === 'succeeded' || (!status && entry.approval?.status === 'approved'))
    return <Check size={12} aria-hidden="true" />
  return <AlertTriangle size={12} aria-hidden="true" />
}

/**
 * The call journal of one answer (M73, compacted by M75-R1): every tool call and every decided
 * approval is a small chip in one horizontal row; a tap opens what is behind it (why it failed,
 * the redacted arguments, who decided). Only an approval that waits for the user stays out as a
 * short bar with its buttons, so the conversation is never pushed off a phone screen.
 */
export function ActivityLog({
  steps,
  approvals,
  onDecide,
}: {
  steps: ToolStep[]
  approvals: Approval[]
  onDecide: (approval: Approval, decision: 'approve' | 'deny') => Promise<void>
}) {
  const [open, setOpen] = useState<string | null>(null)
  const entries = activityOf(steps, approvals)
  if (!entries.length) return null
  const waiting = entries.filter((e) => e.approval?.status === 'pending')
  const chips = entries.filter((e) => e.approval?.status !== 'pending' || e.step)
  const opened = chips.find((e) => e.id === open)
  const base = opened && `step-detail-${opened.id}`
  return (
    <div className="mw-activity">
      {chips.length > 0 && (
        <ul className="mw-steps mw-steps--row" aria-label="Действия Мии">
          {chips.map((entry) => (
            <li key={entry.id} className="mw-step" data-status={entry.step?.status ?? 'approval'}>
              <button
                type="button"
                className={`mia-chip mw-step__chip ${entryTone(entry)}`}
                aria-expanded={open === entry.id}
                aria-controls={`step-detail-${entry.id}`}
                aria-label={`${toolName(entry.tool)} · ${entryLabel(entry)}`}
                onClick={() => setOpen(open === entry.id ? null : entry.id)}
              >
                <EntryIcon entry={entry} />
                <span className="mw-step__name">{toolName(entry.tool)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {opened && (
        <div
          id={base}
          className="mw-step__body"
          role="group"
          aria-label={`Подробности: ${toolName(opened.tool)}`}
        >
          <EntryDetail entry={opened} />
        </div>
      )}
      {waiting.map((entry) =>
        entry.approval!.full ? (
          <ApprovalCard
            key={entry.approval!.id}
            approval={entry.approval!.full}
            onDecide={onDecide}
            compact
          />
        ) : (
          <p key={entry.approval!.id} className="mw-muted">
            Ждёт вашего решения: {entry.approval!.summary}.{' '}
            <Link to="/approvals" className="mw-link">
              Открыть подтверждения
            </Link>
          </p>
        ),
      )}
    </div>
  )
}

function EntryDetail({ entry }: { entry: Entry }) {
  const step = entry.step
  const failed = step && step.status !== 'succeeded' && step.status !== 'running'
  const reason = failed ? (step.reason ?? step.codeText) : undefined
  const args = step?.args && Object.keys(step.args).length ? argumentLines(step.args) : []
  const approval = entry.approval
  return (
    <>
      {reason && (
        <p className="mw-step__reason" data-testid="step-reason">
          <strong>Причина:</strong> {reason}
        </p>
      )}
      {approval && (
        <p className="mw-step__approval" data-testid="step-approval">
          <strong>Подтверждение:</strong> {statusText[approval.status]}
          {approval.channel && decidedBy[approval.channel]
            ? ` · ${decidedBy[approval.channel]}`
            : ''}
          {approval.summary ? ` — ${approval.summary}` : ''}
        </p>
      )}
      {args.length > 0 && (
        <dl className="mw-step__args" aria-label="Аргументы вызова">
          {args.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {!reason && !approval && args.length === 0 && (
        <p className="mw-muted">{step ? stepStatus[step.status] : 'Нет подробностей.'}</p>
      )}
    </>
  )
}

/** The result of a background job posted into the chat it was started from (M74). */
export function JobResultCard({
  card,
  text,
  display,
}: {
  card: FeedCard
  text: string
  display: Display | null
}) {
  const done = card.status === 'succeeded'
  return (
    <article
      className="mia-card mia-glass mw-display mw-jobcard"
      aria-label={`Результат задачи: ${card.title}`}
      data-status={card.status}
    >
      <p className="mw-jobcard__head">
        <span className={`mia-chip ${done ? 'mia-chip--success' : 'mia-chip--warning'}`}>
          {done ? <Check size={12} /> : <AlertTriangle size={12} />}
          {done ? 'Задача готова' : 'Задача не удалась'}
        </span>
      </p>
      <h3 className="mia-card__title">{card.title}</h3>
      <div className="mia-card__body">
        <DisplayView display={display} text={text} />
      </div>
      <div className="mia-card__actions">
        <Link to="/inbox" className="mia-btn mia-btn--secondary">
          <InboxIcon size={16} aria-hidden="true" /> Во входящих
        </Link>
      </div>
    </article>
  )
}

/** Only links a button may open: https with a host, or the Telegram app (tg://). */
export function openableUrl(url: string | undefined): string | null {
  if (!url || /[\s"'<>`\\]/.test(url)) return null
  if (/^tg:\/\/\S+$/.test(url)) return url
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'https:' && parsed.hostname && !parsed.username ? url : null
  } catch {
    return null
  }
}

function expiryOf(action: ScreenAction): number | null {
  const at = action.expires_at ? Date.parse(action.expires_at) : NaN
  return Number.isNaN(at) ? null : at
}

/** True once the action expired; re-renders by itself at the moment of expiry. */
function useExpired(action: ScreenAction): boolean {
  const at = expiryOf(action)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (at === null || at <= now) return
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(at - now, 2 ** 31 - 1))
    return () => window.clearTimeout(timer)
  }, [at, now])
  return action.expired === true || (at !== null && at <= now)
}

function ScreenActionButton({ action }: { action: ScreenAction }) {
  const expired = useExpired(action)
  const url = expired ? null : openableUrl(action.url)
  if (!url)
    return (
      <button
        type="button"
        className="mia-btn mia-btn--secondary"
        disabled
        data-expired={expired ? 'true' : undefined}
      >
        <ExternalLink size={16} aria-hidden="true" /> {action.label}
        {expired ? ' · ссылка устарела' : ''}
      </button>
    )
  return (
    <a className="mia-btn mia-btn--primary" href={url} target="_blank" rel="noopener noreferrer">
      <ExternalLink size={16} aria-hidden="true" /> {action.label}
    </a>
  )
}

/** Buttons from tool results (M41-R1), e.g. «Открыть Telegram» with a one-time bot link. */
export function ScreenActions({ actions }: { actions: ScreenAction[] }) {
  if (!actions.length) return null
  return (
    <div className="mw-steps" role="group" aria-label="Ссылки от Мии">
      {actions.map((action) => (
        <ScreenActionButton
          key={`${action.label}|${action.url ?? action.expires_at}`}
          action={action}
        />
      ))}
    </div>
  )
}

const statusText: Record<Approval['status'], string> = {
  pending: 'Ждёт вашего решения',
  approved: 'Одобрено',
  denied: 'Отклонено',
  expired: 'Время вышло',
  cancelled: 'Отменено',
}

export function ApprovalCard({
  approval,
  onDecide,
  compact = false,
}: {
  approval: Approval
  onDecide: (approval: Approval, decision: 'approve' | 'deny') => Promise<void>
  compact?: boolean
}) {
  const [busy, setBusy] = useState<'approve' | 'deny' | null>(null)
  const [error, setError] = useState('')
  async function decide(decision: 'approve' | 'deny') {
    setBusy(decision)
    setError('')
    try {
      await onDecide(approval, decision)
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setBusy(null)
    }
  }
  const pending = approval.status === 'pending'
  const elsewhere = pending && approval.device_consent ? approval.device_consent : null
  return (
    <section
      className={`mia-glass mw-approval ${compact ? 'mw-approval--compact' : ''}`}
      aria-label={`Подтверждение: ${approval.summary}`}
      data-status={approval.status}
    >
      <div className="mw-approval__head">
        <ShieldAlert size={20} aria-hidden="true" />
        <div>
          <p className="mw-approval__summary">{approval.summary}</p>
          <p className="mw-approval__meta">
            <span
              className={`mia-chip ${approval.risk === 'write_external' ? 'mia-chip--warning' : ''}`}
            >
              {(compact ? riskShort : riskLabel)[approval.risk]}
            </span>{' '}
            {!compact && <span className="mia-chip">{toolName(approval.tool)}</span>}{' '}
            <span
              className={`mia-chip ${pending ? 'mia-chip--accent' : approval.status === 'approved' ? 'mia-chip--success' : ''}`}
            >
              {statusText[approval.status]}
            </span>
          </p>
        </div>
      </div>
      {!compact && (
        <details className="mw-approval__args">
          <summary>
            Параметры <ChevronDown size={14} aria-hidden="true" />
          </summary>
          <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
        </details>
      )}
      {elsewhere && (
        <p className="mw-approval__elsewhere" role="status">
          <Smartphone size={16} aria-hidden="true" /> Подтвердите на телефоне
          {elsewhere.target_device_name ? ` (${elsewhere.target_device_name})` : ''}
        </p>
      )}
      {pending && !elsewhere && (
        <div className="mw-approval__actions">
          <button
            className="mia-btn mia-btn--primary"
            disabled={busy !== null}
            onClick={() => void decide('approve')}
          >
            {busy === 'approve' ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}{' '}
            Одобрить
          </button>
          <button
            className="mia-btn mia-btn--secondary"
            disabled={busy !== null}
            onClick={() => void decide('deny')}
          >
            {busy === 'deny' ? <LoaderCircle size={16} className="spin" /> : <X size={16} />}{' '}
            Отклонить
          </button>
        </div>
      )}
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
