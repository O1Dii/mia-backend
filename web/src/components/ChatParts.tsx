import { useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ExternalLink,
  LoaderCircle,
  ShieldAlert,
  X,
} from 'lucide-react'
import type { Approval, Display, Risk } from '../lib/api'
import { messageOf } from '../lib/http'
import { Markdown } from '../lib/markdown'
import type { ToolStep } from '../lib/chat'

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
  if (display.kind === 'app')
    return (
      <article className="mia-card mia-glass mw-display">
        <span className="mia-card__eyebrow">Мини-приложение</span>
        <div className="mia-card__body">
          <Markdown text={display.fallback_markdown ?? `Приложение ${display.plugin_id}`} />
        </div>
      </article>
    )
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
