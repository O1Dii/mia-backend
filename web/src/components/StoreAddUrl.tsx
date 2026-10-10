import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowLeft, Link2, LoaderCircle } from 'lucide-react'
import { toast } from 'sonner'
import { api, type Discovery, type Installation, type Risk } from '../lib/api'
import { ApiError, messageOf } from '../lib/http'
import { errorText } from '../lib/errors'
import { Link, navigate } from '../lib/router'
import { riskLabel } from './ChatParts'
import { connectAccount } from './StoreShared'

/** Tools of a server this large start without destructive ones (mirrors the API default). */
const LARGE_SERVER = 24

/** Add an MCP server by URL: probe, sign in if it asks, pick the tools, consent. */
export function StoreAddUrl({ search }: { search: URLSearchParams }) {
  const resume = search.get('installation')
  const [url, setUrl] = useState('')
  const [installation, setInstallation] = useState<Installation | null>(null)
  const [discovery, setDiscovery] = useState<Discovery | null>(null)
  const [busy, setBusy] = useState(false)
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  /** Risks the user changed from what the server's MCP hints (or an earlier choice) proposed. */
  const [edited, setEdited] = useState<Record<string, Risk>>({})
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

  const usable = useMemo(() => {
    const skipped = new Set(discovery?.skipped_tools ?? [])
    return (discovery?.tools ?? []).filter((tool) => !skipped.has(tool.name))
  }, [discovery])
  const recommended = useCallback(
    () => new Set(usable.filter((tool) => tool.selected ?? true).map((tool) => tool.name)),
    [usable],
  )
  useEffect(() => {
    setChosen(recommended())
    setEdited({})
  }, [recommended])
  const riskOf = (tool: { name: string; risk: Risk }): Risk => edited[tool.name] ?? tool.risk
  const riskGrants = discovery?.risk_grants ?? []
  const shownGrants = [
    ...riskGrants.filter((grant) =>
      usable.some((tool) => chosen.has(tool.name) && `tools.${riskOf(tool)}` === grant.id),
    ),
    ...(discovery?.grants ?? []).filter(
      (grant) => !grant.id.startsWith('tools.') || riskGrants.length === 0,
    ),
  ]
  const reads = usable.filter((tool) => chosen.has(tool.name) && riskOf(tool) === 'read').length
  const limit = discovery?.tool_limit ?? usable.length
  const over = chosen.size - limit
  const editing = installation?.status === 'active'

  function toggle(name: string, on: boolean) {
    setChosen((current) => {
      const next = new Set(current)
      if (on) next.add(name)
      else next.delete(name)
      return next
    })
  }

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
      const tools = usable.filter((tool) => chosen.has(tool.name)).map((tool) => tool.name)
      const changed = Object.fromEntries(
        Object.entries(edited).filter(
          ([name, risk]) =>
            tools.includes(name) && usable.find((tool) => tool.name === name)?.risk !== risk,
        ),
      )
      await api.consentCustom(installation, discovery, tools, changed)
      toast.success(editing ? 'Инструменты сохранены' : 'Сервер добавлен')
      navigate('/installed')
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }

  async function cancel() {
    if (editing) {
      navigate('/installed')
      return
    }
    if (installation) await api.uninstall(installation.id).catch(() => undefined)
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
          Подключите свой сервер инструментов. Мия проверит его, покажет инструменты — отметьте
          нужные — и спросит разрешение. Чтение сервер выполняет без вопросов, а действия во внешнем
          сервисе потребуют вашего подтверждения.
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
          <h3>Инструменты ({usable.length})</h3>
          <p className="mw-muted">
            Уровень риска Мия берёт из пометок самого сервера (только чтение, не разрушает данные);
            без пометки действие считается внешним и ждёт вашего подтверждения. Его можно изменить у
            каждого инструмента. Чтение (выбрано: {reads}) выполняется без подтверждения.
          </p>
          <p className="mw-muted" role="status" aria-live="polite">
            Выбрано {chosen.size} из {usable.length}
            {limit < usable.length ? `, можно не больше ${limit}` : ''}.
          </p>
          {limit < usable.length && (
            <p className="mw-muted">
              Модель получает за раз ограниченное число инструментов всех плагинов вместе, поэтому у
              этого сервера можно включить не больше {limit}. Выбор можно изменить позже в «Моих
              плагинах».
            </p>
          )}
          {usable.length > LARGE_SERVER && (
            <p className="mw-muted">
              Удаление и архивирование не отмечены — отметьте их, если они нужны.
            </p>
          )}
          <div className="mw-actions">
            <button
              type="button"
              className="mia-btn mia-btn--ghost"
              disabled={busy}
              onClick={() => setChosen(recommended())}
            >
              Рекомендуемые
            </button>
            <button
              type="button"
              className="mia-btn mia-btn--ghost"
              disabled={busy || chosen.size === 0}
              onClick={() => setChosen(new Set())}
            >
              Снять все
            </button>
          </div>
          <ul className="mw-list" aria-label="Инструменты сервера">
            {usable.map((tool) => (
              <li key={tool.name}>
                <label className="mw-switch">
                  <input
                    type="checkbox"
                    checked={chosen.has(tool.name)}
                    disabled={busy}
                    onChange={(event) => toggle(tool.name, event.target.checked)}
                  />
                  <strong>{tool.name.replace(/_/g, ' ')}</strong>
                </label>
                <select
                  className="mw-risk"
                  aria-label={`Риск: ${tool.name.replace(/_/g, ' ')}`}
                  value={riskOf(tool)}
                  disabled={busy}
                  onChange={(event) =>
                    setEdited((current) => ({
                      ...current,
                      [tool.name]: event.target.value as Risk,
                    }))
                  }
                >
                  {(['read', 'write_reversible', 'write_external'] as const).map((risk) => (
                    <option key={risk} value={risk}>
                      {riskLabel[risk]}
                    </option>
                  ))}
                </select>
                {tool.risk_source === 'annotation' && edited[tool.name] === undefined && (
                  <span className="mia-chip">по пометке сервера</span>
                )}
                {(edited[tool.name] !== undefined || tool.risk_source === 'saved') && (
                  <span className="mia-chip">изменено вами</span>
                )}
                {tool.description && <small>{tool.description}</small>}
              </li>
            ))}
          </ul>
          {discovery.skipped_tools.length > 0 && (
            <p className="mw-muted">
              Пропущены инструменты с неподходящими именами: {discovery.skipped_tools.join(', ')}.
            </p>
          )}
          {limit === 0 && (
            <p className="mw-error" role="alert">
              Места для инструментов не осталось: другие плагины уже занимают всё, что модель
              получает за раз. Удалите плагин или уменьшите выбор у другого сервера.
            </p>
          )}
          {limit > 0 && over > 0 && (
            <p className="mw-error" role="alert">
              Выбрано больше, чем можно: снимите отметку ещё с {over}.
            </p>
          )}
          <h3>Разрешения</h3>
          <ul className="mw-list">
            {shownGrants.map((grant) => (
              <li key={grant.id}>{grant.text}</li>
            ))}
          </ul>
          <div className="mw-actions">
            <button
              className="mia-btn mia-btn--primary"
              disabled={busy || chosen.size === 0 || over > 0}
              onClick={() => void allow()}
            >
              {busy && <LoaderCircle size={16} className="spin" />}{' '}
              {editing ? 'Сохранить выбор' : 'Разрешить и добавить'}
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
