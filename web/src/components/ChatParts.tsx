import { useEffect, useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ExternalLink,
  Inbox as InboxIcon,
  LoaderCircle,
  ShieldAlert,
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

/**
 * The call journal (M73): every step is a chip; a step that did not succeed shows why, open; the
 * redacted arguments of any step fold under «Аргументы».
 */
export function ToolSteps({ steps }: { steps: ToolStep[] }) {
  if (!steps.length) return null
  return (
    <ul className="mw-steps" aria-label="Действия Мии">
      {steps.map((step) => {
        const failed = step.status !== 'succeeded' && step.status !== 'running'
        const reason = failed ? (step.reason ?? step.codeText) : undefined
        const args = step.args && Object.keys(step.args).length ? argumentLines(step.args) : []
        const detailed = Boolean(reason) || args.length > 0
        const chip = (
          <>
            {step.status === 'running' ? (
              <LoaderCircle size={12} className="spin" />
            ) : step.status === 'succeeded' ? (
              <Check size={12} />
            ) : (
              <AlertTriangle size={12} />
            )}
            {toolName(step.tool)} · {stepStatus[step.status]}
          </>
        )
        const tone = `mia-chip ${step.status === 'succeeded' ? 'mia-chip--success' : step.status === 'running' ? '' : 'mia-chip--warning'}`
        return (
          <li key={step.id} className="mw-step" data-status={step.status}>
            {detailed ? (
              <details className="mw-step__details" open={Boolean(reason)}>
                <summary className={tone}>
                  {chip} <ChevronDown size={12} aria-hidden="true" />
                </summary>
                <div className="mw-step__body">
                  {reason && (
                    <p className="mw-step__reason" data-testid="step-reason">
                      <strong>Причина:</strong> {reason}
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
                </div>
              </details>
            ) : (
              <span className={tone}>{chip}</span>
            )}
          </li>
        )
      })}
    </ul>
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
              {riskLabel[approval.risk]}
            </span>{' '}
            <span className="mia-chip">{toolName(approval.tool)}</span>{' '}
            <span
              className={`mia-chip ${pending ? 'mia-chip--accent' : approval.status === 'approved' ? 'mia-chip--success' : ''}`}
            >
              {statusText[approval.status]}
            </span>
          </p>
        </div>
      </div>
      <details className="mw-approval__args">
        <summary>
          Параметры <ChevronDown size={14} aria-hidden="true" />
        </summary>
        <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
      </details>
      {pending && (
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
