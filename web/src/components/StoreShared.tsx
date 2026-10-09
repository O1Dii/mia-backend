import { useCallback, useEffect, useState } from 'react'
import { Blocks, Check, Star } from 'lucide-react'
import {
  api,
  type CatalogEntry,
  type Connection,
  type Consent,
  type Installation,
} from '../lib/api'
import { messageOf } from '../lib/http'
import { PluginArt } from './PluginArt'
import { riskLabel } from './ChatParts'

export const categoryTitles: Record<string, string> = {
  work: 'Работа',
  productivity: 'Продуктивность',
  communication: 'Общение',
  knowledge: 'Знания',
  home: 'Дом',
  utilities: 'Повседневное',
  fun: 'Развлечения',
}

/** Everything the store screens need to know about what is installed and connected. */
export function useStoreState() {
  const [installations, setInstallations] = useState<Installation[] | null>(null)
  const [connections, setConnections] = useState<Connection[]>([])
  const [catalog, setCatalog] = useState<Record<string, CatalogEntry>>({})
  const [error, setError] = useState('')
  const reload = useCallback(async () => {
    try {
      const [inst, conns, plugins] = await Promise.all([
        api.installations(),
        api.connections(),
        api.plugins(),
      ])
      setInstallations(inst.items)
      setConnections(conns.items)
      setCatalog(Object.fromEntries(plugins.items.map((p) => [p.plugin_id, p])))
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить плагины.'))
    }
  }, [])
  useEffect(() => {
    void reload()
  }, [reload])
  return { installations, connections, catalog, error, reload }
}

/**
 * Open the provider's consent page; the callback brings the browser back to `returnTo`.
 * A `mia_token` plugin connects at once (Mia signs its requests), so there is no consent page.
 */
export async function connectAccount(installationId: string, returnTo: string): Promise<void> {
  const started = await api.startConnection(installationId, returnTo)
  if (!started.authorization_url) {
    // `returnTo` is an app path; the page itself may live under a prefix (/mia, M62-T1).
    const base = import.meta.env.BASE_URL.replace(/\/$/, '')
    location.assign(`${base}${returnTo}${returnTo.includes('?') ? '&' : '?'}connection=ok`)
    return
  }
  location.assign(started.authorization_url)
}

export function PluginMark({
  entry,
  size = 'card',
}: {
  entry?: CatalogEntry
  size?: 'card' | 'row' | 'hero'
}) {
  if (entry?.art && size !== 'row') return <PluginArt kind={entry.art} compact={size === 'card'} />
  const initial = (entry?.name ?? '?').slice(0, 1).toUpperCase()
  return (
    <div className={`mw-mark mw-mark--${size}`} aria-hidden="true">
      {entry ? <span>{initial}</span> : <Blocks size={28} />}
    </div>
  )
}

export function Stars({ average, count }: { average: number | null; count: number }) {
  if (!count || average === null) return <span className="mw-muted">Нет оценок</span>
  return (
    <span className="mw-stars" aria-label={`Оценка ${average.toFixed(1)} из 5, отзывов: ${count}`}>
      <Star size={14} aria-hidden="true" /> {average.toFixed(1)}{' '}
      <span className="mw-muted">({count})</span>
    </span>
  )
}

export function ConsentSummary({ consent, added }: { consent: Consent; added?: string[] }) {
  const extras = [
    consent.network.length > 0 ? `Сетевые адреса: ${consent.network.join(', ')}` : '',
    consent.llm.via_host
      ? `Использует вашу модель${consent.llm.monthly_budget_usd ? ` (до $${consent.llm.monthly_budget_usd} в месяц)` : ''}`
      : '',
    consent.memory.length > 0 ? `Память: ${consent.memory.join(', ')}` : '',
    consent.notify ? 'Может присылать уведомления' : '',
    consent.schedules ? 'Может создавать расписания' : '',
  ].filter(Boolean)
  return (
    <div className="mw-consent">
      <h3>Что сможет Мия</h3>
      <ul className="mw-list">
        {consent.tools.map((tool) => (
          <li key={tool.name}>
            <strong>{tool.name.replace(/_/g, ' ')}</strong>
            <span
              className={`mia-chip ${tool.risk === 'write_external' ? 'mia-chip--warning' : ''}`}
            >
              {riskLabel[tool.risk]}
            </span>
            {tool.description && <small>{tool.description}</small>}
          </li>
        ))}
        {!consent.tools.length && <li className="mw-muted">Плагин не добавляет инструментов.</li>}
      </ul>
      <h3>Разрешения</h3>
      <ul className="mw-list mw-list--checks">
        {consent.grants.map((grant) => (
          <li key={grant.id}>
            <Check size={14} aria-hidden="true" />
            <span>{grant.text}</span>
            {added?.includes(grant.id) && <span className="mia-chip mia-chip--accent">новое</span>}
          </li>
        ))}
        {extras.map((text) => (
          <li key={text}>
            <Check size={14} aria-hidden="true" />
            <span>{text}</span>
          </li>
        ))}
      </ul>
      {consent.requires_connection && (
        <p className="mw-notice">
          После установки нужно войти в аккаунт сервиса. Пароль Мия не видит.
        </p>
      )}
    </div>
  )
}
