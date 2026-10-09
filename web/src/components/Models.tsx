import { useCallback, useEffect, useState } from 'react'
import { Check, Cpu, KeyRound, LoaderCircle, Plus, Trash2, Zap } from 'lucide-react'
import { toast } from 'sonner'
import { api, type ModelConnection, type ModelProvider } from '../lib/api'
import { errorText } from '../lib/errors'
import { messageOf } from '../lib/http'
import { providers, type ProviderInfo } from '../lib/providers'
import { Dialog } from './Dialog'

const statusText: Record<ModelConnection['status'], string> = {
  active: 'Работает',
  paused: 'Пауза (лимит провайдера)',
  needs_reauth: 'Нужен новый ключ',
}

export function Models() {
  const [items, setItems] = useState<ModelConnection[] | null>(null)
  const [error, setError] = useState('')
  const [adding, setAdding] = useState<ProviderInfo | null>(null)
  const [busy, setBusy] = useState('')

  const reload = useCallback(async () => {
    try {
      setItems((await api.models()).items)
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить подключения.'))
    }
  }, [])
  useEffect(() => {
    void reload()
  }, [reload])

  async function act(id: string, work: () => Promise<void>, failure: string) {
    setBusy(id)
    try {
      await work()
      await reload()
    } catch (e) {
      toast.error(messageOf(e, failure))
    } finally {
      setBusy('')
    }
  }

  return (
    <section className="mw-page" aria-labelledby="models-title">
      <header className="mw-page__head">
        <h1 id="models-title">Модели</h1>
        <p>
          Подключите свою модель. Ключ хранится на сервере зашифрованным и никогда не показывается
          снова — видны только последние 4 символа.
        </p>
      </header>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      <h2 className="mw-section">
        Мои подключения {items && <span className="mia-chip">{items.length}</span>}
      </h2>
      {items === null && !error && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      )}
      {items?.length === 0 && (
        <div className="mw-empty mia-glass">
          <KeyRound size={28} aria-hidden="true" />
          <h2>Начните с одной модели</h2>
          <p>Используйте свой API-ключ или локальную модель без ключа.</p>
        </div>
      )}
      <ul className="mw-stack">
        {items?.map((connection) => (
          <li key={connection.id} className="mia-glass mw-model" data-model={connection.label}>
            <Cpu size={22} aria-hidden="true" />
            <div className="mw-model__body">
              <h3>{connection.label}</h3>
              <p className="mw-muted">
                {providers.find((p) => p.id === connection.provider)?.name ?? connection.provider} ·{' '}
                {connection.model}
                {connection.key_last4 ? ` · ключ …${connection.key_last4}` : ''}
              </p>
              <p>
                {connection.is_primary && (
                  <span className="mia-chip mia-chip--accent">Основная</span>
                )}{' '}
                <span
                  className={`mia-chip ${connection.status === 'active' ? 'mia-chip--success' : 'mia-chip--warning'}`}
                >
                  {statusText[connection.status]}
                </span>{' '}
                {connection.capability === 'chat_only' && (
                  <span className="mia-chip">Только чат</span>
                )}{' '}
                {connection.last_test_ok === true && (
                  <span className="mia-chip mia-chip--success">Проверено</span>
                )}
                {connection.last_test_ok === false && (
                  <span className="mia-chip mia-chip--danger">
                    Проверка не прошла
                    {connection.last_test_error_code
                      ? `: ${errorText(connection.last_test_error_code)}`
                      : ''}
                  </span>
                )}
              </p>
            </div>
            <div className="mw-model__actions">
              {!connection.is_primary && (
                <button
                  className="mia-btn mia-btn--secondary"
                  disabled={busy === connection.id}
                  onClick={() =>
                    void act(
                      connection.id,
                      async () =>
                        void (await api.patchModel(connection.id, { make_primary: true })),
                      'Не удалось выбрать модель',
                    )
                  }
                >
                  <Check size={16} /> Сделать основной
                </button>
              )}
              {connection.status === 'paused' && (
                <button
                  className="mia-btn mia-btn--secondary"
                  disabled={busy === connection.id}
                  onClick={() =>
                    void act(
                      connection.id,
                      async () => void (await api.resumeModel(connection.id)),
                      'Не удалось возобновить',
                    )
                  }
                >
                  Возобновить
                </button>
              )}
              <button
                className="mia-btn mia-btn--secondary"
                disabled={busy === connection.id}
                onClick={() =>
                  void act(
                    connection.id,
                    async () => {
                      const result = await api.testModel(connection.id)
                      if (result.ok)
                        toast.success(
                          `${connection.label}: модель отвечает (${result.latency_ms} мс)`,
                        )
                      else
                        toast.error(
                          `${connection.label}: ${errorText(result.error_code ?? undefined, 'проверка не прошла')}`,
                        )
                    },
                    'Не удалось проверить подключение',
                  )
                }
              >
                {busy === connection.id ? (
                  <LoaderCircle size={16} className="spin" />
                ) : (
                  <Zap size={16} />
                )}{' '}
                Проверить
              </button>
              <button
                className="mia-btn mia-btn--ghost"
                aria-label={`Удалить подключение ${connection.label}`}
                disabled={busy === connection.id}
                onClick={() =>
                  void act(
                    connection.id,
                    async () => void (await api.removeModel(connection.id)),
                    'Не удалось удалить',
                  )
                }
              >
                <Trash2 size={16} /> Удалить
              </button>
            </div>
          </li>
        ))}
      </ul>

      <h2 className="mw-section">Добавить модель</h2>
      <ul className="mw-grid mw-grid--small">
        {providers.map((provider) => (
          <li key={provider.id}>
            <button className="mia-glass mw-provider" onClick={() => setAdding(provider)}>
              <Plus size={18} aria-hidden="true" />
              <strong>{provider.name}</strong>
              <small>{provider.local ? 'Без ключа' : 'API-ключ'}</small>
            </button>
          </li>
        ))}
      </ul>
      {adding && (
        <AddModel
          provider={adding}
          first={(items?.length ?? 0) === 0}
          onClose={() => setAdding(null)}
          onSaved={async () => {
            setAdding(null)
            toast.success('Подключение сохранено')
            await reload()
          }}
        />
      )}
    </section>
  )
}

function AddModel({
  provider,
  first,
  onClose,
  onSaved,
}: {
  provider: ProviderInfo
  first: boolean
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const [label, setLabel] = useState(provider.name)
  const [model, setModel] = useState(provider.model)
  const [key, setKey] = useState('')
  const [url, setUrl] = useState(provider.url ?? '')
  const [tools, setTools] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function save() {
    setBusy(true)
    setError('')
    try {
      await api.addModel({
        provider: provider.id as ModelProvider,
        label: label.trim(),
        model: model.trim(),
        api_key: provider.local ? undefined : key.trim(),
        base_url: provider.local ? url.trim() : undefined,
        capability: tools ? 'tools' : 'chat_only',
        make_primary: first ? true : undefined,
      })
      await onSaved()
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }
  return (
    <Dialog
      title={`Подключить ${provider.name}`}
      description={
        provider.local
          ? 'Локальная модель не требует ключа.'
          : 'Ключ шифруется на сервере и не показывается снова.'
      }
      onClose={busy ? () => undefined : onClose}
    >
      <form
        className="mw-form"
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <label>
          <span>Название</span>
          <input
            value={label}
            maxLength={80}
            onChange={(event) => setLabel(event.target.value)}
            required
          />
        </label>
        <label>
          <span>ID модели</span>
          <input
            value={model}
            maxLength={160}
            onChange={(event) => setModel(event.target.value)}
            required
          />
        </label>
        {provider.local ? (
          <label>
            <span>Адрес сервера</span>
            <input value={url} onChange={(event) => setUrl(event.target.value)} required />
          </label>
        ) : (
          <label>
            <span>API-ключ</span>
            <input
              type="password"
              autoComplete="off"
              value={key}
              maxLength={512}
              onChange={(event) => setKey(event.target.value)}
              required
            />
          </label>
        )}
        <label className="mw-switch">
          <input
            type="checkbox"
            checked={tools}
            onChange={(event) => setTools(event.target.checked)}
          />
          <span>Модель умеет вызывать инструменты</span>
        </label>
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
        <div className="mw-dialog__actions">
          <button
            type="button"
            className="mia-btn mia-btn--secondary"
            disabled={busy}
            onClick={onClose}
          >
            Отмена
          </button>
          <button
            className="mia-btn mia-btn--primary"
            disabled={busy || !label.trim() || !model.trim()}
          >
            {busy && <LoaderCircle size={16} className="spin" />} Сохранить подключение
          </button>
        </div>
      </form>
    </Dialog>
  )
}
