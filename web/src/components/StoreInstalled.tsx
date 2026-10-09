import { useEffect, useState } from 'react'
import { LoaderCircle, Plug, PlugZap, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, type CatalogDetail, type Installation, type UpdateInfo } from '../lib/api'
import { messageOf } from '../lib/http'
import { Link } from '../lib/router'
import { Dialog } from './Dialog'
import { ConsentDialog } from './StoreConsent'
import { PluginMark, connectAccount, useStoreState } from './StoreShared'

const statusChip: Record<Installation['status'], { text: string; className: string }> = {
  active: { text: 'Работает', className: 'mia-chip--success' },
  needs_connection: { text: 'Нужен аккаунт', className: 'mia-chip--warning' },
  needs_consent: { text: 'Ждёт разрешений', className: 'mia-chip--warning' },
  disabled: { text: 'Выключен', className: '' },
}

export function StoreInstalled() {
  const state = useStoreState()
  const [updates, setUpdates] = useState<Record<string, UpdateInfo>>({})
  const [removing, setRemoving] = useState<Installation | null>(null)
  const [updating, setUpdating] = useState<Installation | null>(null)
  const [busy, setBusy] = useState('')

  useEffect(() => {
    const catalogInstalls = state.installations?.filter((i) => i.source === 'catalog') ?? []
    let active = true
    void Promise.all(catalogInstalls.map((i) => api.updateInfo(i.id).catch(() => null))).then(
      (infos) => {
        if (!active) return
        setUpdates(
          Object.fromEntries(
            infos
              .filter((info): info is UpdateInfo => Boolean(info?.update_available))
              .map((info) => [info.installation_id, info]),
          ),
        )
      },
    )
    return () => {
      active = false
    }
  }, [state.installations])

  async function connect(installation: Installation) {
    setBusy(installation.id)
    try {
      await connectAccount(
        installation.id,
        `/store/connected?plugin=${encodeURIComponent(installation.plugin_id)}`,
      )
    } catch (e) {
      toast.error(messageOf(e))
      setBusy('')
    }
  }
  async function disconnect(connectionId: string) {
    setBusy(connectionId)
    try {
      await api.disconnect(connectionId)
      toast.success('Аккаунт отключён')
      await state.reload()
    } catch (e) {
      toast.error(messageOf(e))
    } finally {
      setBusy('')
    }
  }
  async function remove(installation: Installation) {
    setBusy(installation.id)
    try {
      await api.uninstall(installation.id)
      setRemoving(null)
      toast.success('Плагин удалён')
      await state.reload()
    } catch (e) {
      toast.error(messageOf(e))
    } finally {
      setBusy('')
    }
  }

  return (
    <section className="mw-page" aria-labelledby="installed-title">
      <header className="mw-page__head">
        <h1 id="installed-title">Мои плагины</h1>
        <p>Установленные плагины, подключённые аккаунты и обновления.</p>
      </header>
      {state.error && (
        <p className="mw-error" role="alert">
          {state.error}
        </p>
      )}
      {state.installations === null && !state.error && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      )}
      {state.installations?.length === 0 && (
        <div className="mw-empty mia-glass">
          <Plug size={28} aria-hidden="true" />
          <h2>Пока ничего не установлено</h2>
          <p>В магазине есть таймер, заметки, новости и другие плагины.</p>
          <Link to="/store" className="mia-btn mia-btn--primary">
            Открыть магазин
          </Link>
        </div>
      )}
      <ul className="mw-stack">
        {state.installations?.map((installation) => {
          const entry = state.catalog[installation.plugin_id]
          const connection = state.connections.find((c) => c.installation_id === installation.id)
          const update = updates[installation.id]
          const chip = statusChip[installation.status]
          const name =
            entry?.name ??
            (installation.custom_url
              ? new URL(installation.custom_url).host
              : installation.plugin_id)
          return (
            <li
              key={installation.id}
              className="mia-glass mw-installed"
              data-plugin={installation.plugin_id}
            >
              <PluginMark entry={entry} size="row" />
              <div className="mw-installed__body">
                <h2>
                  {entry ? (
                    <Link to={`/store/plugin/${encodeURIComponent(installation.plugin_id)}`}>
                      {name}
                    </Link>
                  ) : (
                    name
                  )}
                </h2>
                <p className="mw-muted">
                  {installation.source === 'custom'
                    ? 'Свой MCP-сервер'
                    : `Версия ${installation.version}`}
                  {connection?.account_label ? ` · аккаунт: ${connection.account_label}` : ''}
                </p>
                <p>
                  <span className={`mia-chip ${chip.className}`}>{chip.text}</span>{' '}
                  {connection && connection.status !== 'connected' && (
                    <span className="mia-chip mia-chip--warning">
                      Подключение:{' '}
                      {connection.status === 'pending' ? 'не завершено' : 'нужно обновить'}
                    </span>
                  )}
                  {update && (
                    <span className="mia-chip mia-chip--accent">
                      Есть обновление {update.latest_version}
                    </span>
                  )}
                </p>
              </div>
              <div className="mw-installed__actions">
                {installation.status === 'needs_consent' && installation.source === 'custom' && (
                  <Link
                    to={`/store/add-url?installation=${installation.id}`}
                    className="mia-btn mia-btn--primary"
                  >
                    Продолжить настройку
                  </Link>
                )}
                {installation.source !== 'legacy_web' &&
                  (installation.status === 'needs_connection' ||
                    (connection && connection.status !== 'connected')) && (
                    <button
                      className="mia-btn mia-btn--primary"
                      disabled={busy === installation.id}
                      onClick={() => void connect(installation)}
                    >
                      <PlugZap size={16} />{' '}
                      {connection ? 'Подключить заново' : 'Подключить аккаунт'}
                    </button>
                  )}
                {update && (
                  <button
                    className="mia-btn mia-btn--secondary"
                    onClick={() => setUpdating(installation)}
                  >
                    Обновить
                  </button>
                )}
                {connection?.status === 'connected' && (
                  <button
                    className="mia-btn mia-btn--ghost"
                    disabled={busy === connection.id}
                    onClick={() => void disconnect(connection.id)}
                  >
                    Отключить аккаунт
                  </button>
                )}
                <button
                  className="mia-btn mia-btn--ghost"
                  aria-label={`Удалить ${name}`}
                  disabled={busy === installation.id}
                  onClick={() => setRemoving(installation)}
                >
                  <Trash2 size={16} /> Удалить
                </button>
              </div>
            </li>
          )
        })}
      </ul>
      {removing && (
        <Dialog
          title="Удалить плагин?"
          description="Данные плагина и подключённый аккаунт будут удалены. Токены доступа отзываются на стороне сервиса."
          onClose={() => setRemoving(null)}
        >
          <div className="mw-dialog__actions">
            <button className="mia-btn mia-btn--secondary" onClick={() => setRemoving(null)}>
              Отмена
            </button>
            <button
              className="mia-btn mia-btn--danger"
              disabled={busy === removing.id}
              onClick={() => void remove(removing)}
            >
              {busy === removing.id && <LoaderCircle size={16} className="spin" />} Удалить
            </button>
          </div>
        </Dialog>
      )}
      {updating && updates[updating.id]?.consent && (
        <UpdateDialog
          installation={updating}
          info={updates[updating.id]}
          name={state.catalog[updating.plugin_id]?.name ?? updating.plugin_id}
          onClose={() => setUpdating(null)}
          onDone={async () => {
            setUpdating(null)
            toast.success('Плагин обновлён')
            await state.reload()
          }}
        />
      )}
    </section>
  )
}

function UpdateDialog({
  installation,
  info,
  name,
  onClose,
  onDone,
}: {
  installation: Installation
  info: UpdateInfo
  name: string
  onClose: () => void
  onDone: () => Promise<void>
}) {
  const consent = info.consent as NonNullable<CatalogDetail['consent']>
  return (
    <ConsentDialog
      title={`Обновить «${name}»`}
      description={`Версия ${info.installed_version} → ${info.latest_version}. Новые разрешения отмечены.`}
      consent={consent}
      added={info.added_grants}
      action="Обновить"
      onConfirm={async () => {
        await api.upgrade(installation, consent)
        await onDone()
      }}
      onClose={onClose}
    />
  )
}
