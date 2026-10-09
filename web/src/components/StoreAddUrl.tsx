import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Link2, LoaderCircle } from 'lucide-react'
import { toast } from 'sonner'
import { api, type Discovery, type Installation } from '../lib/api'
import { ApiError, messageOf } from '../lib/http'
import { errorText } from '../lib/errors'
import { Link, navigate } from '../lib/router'
import { riskLabel } from './ChatParts'
import { connectAccount } from './StoreShared'

/** Add an MCP server by URL: probe, sign in if it asks, review the tools, consent. */
export function StoreAddUrl({ search }: { search: URLSearchParams }) {
  const resume = search.get('installation')
  const [url, setUrl] = useState('')
  const [installation, setInstallation] = useState<Installation | null>(null)
  const [discovery, setDiscovery] = useState<Discovery | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(
    search.get('connection') === 'error'
      ? errorText(search.get('code') ?? 'connection_oauth_failed')
      : '',
  )

  const inspect = useCallback(async (id: string) => {
    setBusy(true)
    setError('')
    try {
      const [current, found] = await Promise.all([api.installation(id), api.discovery(id)])
      setInstallation(current)
      setDiscovery(found)
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    if (resume) void inspect(resume)
  }, [resume, inspect])

  async function probe() {
    setBusy(true)
    setError('')
    try {
      const created = await api.createCustom(url.trim())
      navigate(`/store/add-url?installation=${created.id}`, true)
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 422
          ? 'Нужен адрес MCP-сервера с https:// без данных входа в адресе.'
          : messageOf(e),
      )
      setBusy(false)
    }
  }

  async function connect() {
    if (!installation) return
    setBusy(true)
    try {
      await connectAccount(installation.id, `/store/add-url?installation=${installation.id}`)
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }

  async function allow() {
    if (!installation || !discovery) return
    setBusy(true)
    setError('')
    try {
      await api.consentCustom(installation, discovery)
      toast.success('Сервер добавлен')
      navigate('/installed')
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }

  async function cancel() {
    if (installation && installation.status !== 'active')
      await api.uninstall(installation.id).catch(() => undefined)
    navigate('/store')
  }

  return (
    <section className="mw-page" aria-labelledby="add-title">
      <Link to="/store" className="mw-back">
        <ArrowLeft size={16} /> Магазин
      </Link>
      <header className="mw-page__head">
        <h1 id="add-title">Добавить MCP-сервер по URL</h1>
        <p>
          Подключите свой сервер инструментов. Мия проверит его, покажет все инструменты и спросит
          разрешение. Каждое действие такого сервера потребует вашего подтверждения.
        </p>
      </header>

      {!resume && (
        <form
          className="mia-glass mw-block mw-form"
          onSubmit={(event) => {
            event.preventDefault()
            void probe()
          }}
        >
          <label>
            <span>Адрес сервера</span>
            <input
              type="url"
              inputMode="url"
              placeholder="https://mcp.example.com/mcp"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              required
            />
          </label>
          <div className="mw-actions">
            <button
              className="mia-btn mia-btn--primary"
              disabled={busy || !url.startsWith('https://')}
            >
              {busy ? <LoaderCircle size={16} className="spin" /> : <Link2 size={16} />} Проверить
              сервер
            </button>
          </div>
        </form>
      )}

      {resume && busy && !discovery && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Проверяем сервер…
        </p>
      )}

      {discovery?.state === 'needs_connection' && (
        <div className="mia-glass mw-block" aria-live="polite">
          <h2>Сервер просит войти</h2>
          <p>
            <strong>{discovery.host}</strong> требует авторизации. Войдите в аккаунт на его странице
            — Мия получит только токен доступа, пароль ей не передаётся.
          </p>
          <div className="mw-actions">
            <button
              className="mia-btn mia-btn--primary"
              disabled={busy}
              onClick={() => void connect()}
            >
              <Link2 size={16} /> Подключить аккаунт
            </button>
            <button
              className="mia-btn mia-btn--ghost"
              disabled={busy}
              onClick={() => void cancel()}
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {discovery?.state === 'ready' && (
        <div className="mia-glass mw-block" aria-live="polite">
          <h2>Сервер {discovery.host}</h2>
          <h3>Инструменты ({discovery.tools.length})</h3>
          <ul className="mw-list">
            {discovery.tools.map((tool) => (
              <li key={tool.name}>
                <strong>{tool.name.replace(/_/g, ' ')}</strong>
                <span className="mia-chip mia-chip--warning">{riskLabel[tool.risk]}</span>
                {tool.description && <small>{tool.description}</small>}
              </li>
            ))}
          </ul>
          {discovery.skipped_tools.length > 0 && (
            <p className="mw-muted">
              Пропущены инструменты с неподходящими именами: {discovery.skipped_tools.join(', ')}.
            </p>
          )}
          <h3>Разрешения</h3>
          <ul className="mw-list">
            {discovery.grants.map((grant) => (
              <li key={grant.id}>{grant.text}</li>
            ))}
          </ul>
          <div className="mw-actions">
            <button
              className="mia-btn mia-btn--primary"
              disabled={busy}
              onClick={() => void allow()}
            >
              {busy && <LoaderCircle size={16} className="spin" />} Разрешить и добавить
            </button>
            <button
              className="mia-btn mia-btn--ghost"
              disabled={busy}
              onClick={() => void cancel()}
            >
              Отмена
            </button>
          </div>
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
