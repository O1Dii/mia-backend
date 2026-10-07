import { useAuth } from '../lib/auth'
import { useState } from 'react'
import {
  ArrowUpRight,
  Check,
  Cpu,
  KeyRound,
  LoaderCircle,
  Plus,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, providerOptions, type Connection, type ProviderId } from '../lib/api'
import { Dialog } from './Dialog'

export function Models({
  connections,
  activeId,
  select,
  refresh,
  serverError,
}: {
  connections: Connection[]
  activeId: string
  select: (id: string) => Promise<void>
  refresh: () => Promise<void>
  serverError: string
}) {
  const auth = useAuth()
  const [adding, setAdding] = useState<ProviderId | null>(null)
  const [testing, setTesting] = useState('')
  async function remove(connection: Connection) {
    try {
      await api.remove(connection.id)
      await refresh()
      toast.success('Подключение и ключ удалены')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Не удалось удалить подключение')
    }
  }
  async function test(connection: Connection) {
    setTesting(connection.id)
    try {
      await api.chat(connection.id, [{ role: 'user', content: 'Reply with OK.' }], {
        use_tools: false,
      })
      toast.success(`${connection.label}: модель отвечает`)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Подключение не удалось')
    } finally {
      setTesting('')
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">НЕЗАВИСИМОСТЬ ПО УМОЛЧАНИЮ</span>
          <h1>
            Ваш интеллект.
            <br />
            <span>Ваш выбор.</span>
          </h1>
          <p>Подключите любимую модель. Мия позаботится об инструментах.</p>
        </div>
        <span className="heading-icon">
          <Cpu size={44} strokeWidth={1} />
        </span>
      </div>
      {serverError && (
        <div className="inline-notice error-notice" role="alert">
          <p>{serverError}</p>
          <button className="text-button" onClick={() => void refresh()}>
            Проверить снова
          </button>
        </div>
      )}
      <div className="section-heading">
        <h2>
          Мои подключения <span className="count-pill">{connections.length}</span>
        </h2>
        <span className="muted">Ключи доступны только вашей сессии</span>
      </div>
      {connections.length === 0 ? (
        <div className="empty-state small">
          <KeyRound size={30} />
          <h3>Начните с одной модели</h3>
          <p>Используйте свой API-ключ или локальную модель без ключа.</p>
        </div>
      ) : (
        <div className="connection-list">
          {connections.map((connection) => {
            const provider = providerOptions.find((p) => p.id === connection.provider)!
            return (
              <div className="connection-row" key={connection.id}>
                <span className={`provider-mark ${provider.color}`}>{provider.mark}</span>
                <div>
                  <h3>{connection.label}</h3>
                  <p>
                    {provider.name} · {connection.model}
                  </p>
                </div>
                <div className="connection-actions">
                  <button
                    className={`button ${activeId === connection.id ? 'active-model' : 'secondary'}`}
                    onClick={() => select(connection.id)}
                  >
                    {activeId === connection.id ? <Check size={15} /> : null}
                    {activeId === connection.id ? 'Выбрана' : 'Выбрать'}
                  </button>
                  <button
                    className="button secondary"
                    disabled={Boolean(testing)}
                    onClick={() => void test(connection)}
                  >
                    {testing === connection.id ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : (
                      <ArrowUpRight size={15} />
                    )}
                    Тест API
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`Удалить ${connection.label}`}
                    onClick={() => void remove(connection)}
                  >
                    <Trash2 size={17} />
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}
      <div className="section-heading provider-heading">
        <h2>Подключить провайдера</h2>
        <span className="muted">
          {auth.required ? 'Облачные модели с вашим ключом' : 'Облако или ваш компьютер'}
        </span>
      </div>
      <div className="providers-grid">
        {providerOptions
          .filter((p) => !auth.required || !p.local)
          .map((provider) => (
            <button
              className="provider-card"
              key={provider.id}
              onClick={() => setAdding(provider.id)}
            >
              <span className={`provider-mark ${provider.color}`}>{provider.mark}</span>
              <div>
                <h3>{provider.name}</h3>
                <span>{provider.local ? 'Локально · без облака' : 'Ваш API-ключ'}</span>
              </div>
              <Plus size={18} />
            </button>
          ))}
      </div>
      <div className="privacy-panel">
        <ShieldCheck size={24} />
        <div>
          <h3>Ваши ключи остаются вашими</h3>
          <p>
            Ключи живут в памяти сервера до 8 часов или его перезапуска. Мия не записывает их в
            браузерное хранилище. Тест API выполняет небольшой запрос к модели.
          </p>
        </div>
      </div>
      {adding && (
        <AddConnection
          providerId={adding}
          onClose={() => setAdding(null)}
          onAdded={async (id) => {
            await refresh()
            await select(id)
            setAdding(null)
          }}
        />
      )}
    </>
  )
}

function AddConnection({
  providerId,
  onClose,
  onAdded,
}: {
  providerId: ProviderId
  onClose: () => void
  onAdded: (id: string) => Promise<void>
}) {
  const provider = providerOptions.find((p) => p.id === providerId)!
  const [label, setLabel] = useState(provider.name)
  const [model, setModel] = useState(provider.model)
  const [key, setKey] = useState('')
  const [url, setUrl] = useState(provider.url ?? '')
  const [toolMode, setToolMode] = useState<'native' | 'json'>('native')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function save() {
    setBusy(true)
    setError('')
    try {
      const connection = await api.add({
        provider: providerId,
        label: label.trim(),
        model: model.trim(),
        api_key: key.trim(),
        tool_mode: toolMode,
        ...(provider.local ? { base_url: url.trim() } : {}),
      })
      setKey('')
      await onAdded(connection.id)
      toast.success('Подключение добавлено. Проверьте его кнопкой «Тест API».')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не удалось сохранить подключение.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog
      title={`Подключить ${provider.name}`}
      description={
        provider.local
          ? 'Модель должна быть запущена на том же компьютере, что и web API.'
          : 'Добавьте ключ своего аккаунта и точное имя доступной вам модели.'
      }
      onClose={() => {
        setKey('')
        onClose()
      }}
    >
      <form
        className="connection-form"
        onSubmit={(e) => {
          e.preventDefault()
          void save()
        }}
      >
        <label>
          Имя подключения
          <input
            aria-label="Имя подключения"
            required
            maxLength={60}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
        </label>
        <label>
          Модель
          <input
            aria-label="Модель"
            required
            pattern="[a-zA-Z0-9_./:@\-]+"
            maxLength={150}
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
        </label>
        <p className="field-note">
          Укажите точный ID из каталога провайдера или имя загруженной локальной модели. Пример
          можно изменить.
        </p>
        <label>
          Вызов действий плагинов
          <select
            aria-label="Режим инструментов"
            value={toolMode}
            onChange={(e) => setToolMode(e.target.value as 'native' | 'json')}
          >
            <option value="native">Native tools — GLM, GPT, Claude, Gemini и др.</option>
            <option value="json">JSON-совместимость — для моделей без tools</option>
          </select>
        </label>
        {provider.local && (
          <label>
            Адрес локального API
            <input
              aria-label="Адрес локального API"
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder={provider.url}
            />
          </label>
        )}
        <label>
          API-ключ {provider.local ? '(необязательно)' : ''}
          <input
            aria-label="API-ключ"
            type="password"
            autoComplete="off"
            spellCheck={false}
            required={!provider.local}
            maxLength={4096}
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={
              provider.local
                ? 'Оставьте пустым, если сервер без авторизации'
                : 'Вставьте ключ провайдера'
            }
          />
        </label>
        {error && (
          <p className="error-message" role="alert">
            {error}
          </p>
        )}
        <button
          className="button primary full-width"
          disabled={busy || !model.trim() || !label.trim() || (!provider.local && !key.trim())}
        >
          {busy ? <LoaderCircle className="spin" size={17} /> : <KeyRound size={17} />}
          {busy ? 'Сохраняем…' : 'Сохранить подключение'}
        </button>
      </form>
    </Dialog>
  )
}
