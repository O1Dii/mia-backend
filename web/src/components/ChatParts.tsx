import { useEffect, useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ExternalLink,
  LoaderCircle,
  ShieldAlert,
  X,
} from 'lucide-react'
import type { Approval, Display, Risk, ScreenAction } from '../lib/api'
import { messageOf } from '../lib/http'
import { Markdown } from '../lib/markdown'
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

export function ToolSteps({ steps }: { steps: ToolStep[] }) {
  if (!steps.length) return null
  return (
    <ul className="mw-steps" aria-label="Действия Мии">
      {steps.map((step) => (
        <li
          key={step.id}
          className={`mia-chip ${step.status === 'succeeded' ? 'mia-chip--success' : step.status === 'running' ? '' : 'mia-chip--warning'}`}
          data-status={step.status}
        >
          {step.status === 'running' ? (
            <LoaderCircle size={12} className="spin" />
          ) : step.status === 'succeeded' ? (
            <Check size={12} />
          ) : (
            <AlertTriangle size={12} />
          )}
          {toolName(step.tool)} · {stepStatus[step.status]}
        </li>
      ))}
    </ul>
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
