import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  ArrowLeft,
  ChevronRight,
  Cloud,
  HardDrive,
  CircleHelp,
  History,
  LoaderCircle,
  Monitor,
  Pencil,
  Smartphone,
  Unplug,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  api,
  type CloudRow,
  type DeviceAccessLogEntry,
  type DeviceCapabilityState,
  type DeviceDetail,
  type DeviceKind,
  type DeviceList,
  type DeviceSummary,
} from '../lib/api'
import { when } from '../lib/format'
import { messageOf } from '../lib/http'
import { Link, navigate } from '../lib/router'
import { Dialog } from './Dialog'

const REFRESH_MS = 30_000

const kindLabel: Record<string, string> = {
  phone: 'Телефон',
  browser: 'Браузер',
  node: 'Узел Mia',
}

function KindIcon({ kind, size = 20 }: { kind: DeviceKind; size?: number }) {
  if (kind === 'phone') return <Smartphone size={size} aria-hidden="true" />
  if (kind === 'browser') return <Monitor size={size} aria-hidden="true" />
  if (kind === 'node') return <HardDrive size={size} aria-hidden="true" />
  return <CircleHelp size={size} aria-hidden="true" />
}

/** «5 мин назад» measured against the server clock, so a wrong computer clock does not lie. */
export function ago(value: string | null, serverTime?: string): string {
  if (!value) return 'давно'
  const now = serverTime ? Date.parse(serverTime) : Date.now()
  const minutes = Math.max(0, Math.floor((now - Date.parse(value)) / 60_000))
  if (minutes < 1) return 'только что'
  if (minutes < 60) return `${minutes} мин назад`
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)} ч назад`
  return when(value)
}

function presenceText(device: DeviceSummary, serverTime?: string): string {
  const status = device.presence.status
  if (status === 'online') return 'онлайн'
  if (status === 'recent') return `был(а) ${ago(device.last_seen_at, serverTime)}`
  if (status === 'no_push') return 'нет push'
  if (status === 'offline') return 'офлайн'
  return 'состояние неизвестно'
}

function presenceTone(status: string): string {
  if (status === 'online') return 'mia-chip--success'
  if (status === 'no_push') return 'mia-chip--warning'
  return ''
}

function PresenceChip({ device, serverTime }: { device: DeviceSummary; serverTime?: string }) {
  return (
    <span
      className={`mia-chip ${presenceTone(device.presence.status)}`}
      data-presence={device.presence.status}
    >
      {presenceText(device, serverTime)}
    </span>
  )
}

/** Why a phone shows «нет push» and what to do about it. */
function pushHint(device: DeviceSummary): string | null {
  if (device.kind !== 'phone' || device.presence.status !== 'no_push') return null
  if (device.push === 'not_configured')
    return 'Push не настроен на сервере: телефон получает запросы, только пока Мия на нём открыта.'
  return 'Откройте Мию на телефоне и разрешите уведомления — иначе просьбы с других устройств до него не дойдут.'
}

function platformLine(device: DeviceSummary): string {
  const { os, model, app_version: version, client } = device.platform
  if (device.kind === 'browser') {
    const signedIn = `вход ${when(device.connected_at)}`
    return device.custom_name && client ? `${client} · ${signedIn}` : signedIn
  }
  const parts = [model, os, version ? `Мия ${version}` : null].filter(Boolean)
  return parts.length ? parts.join(' · ') : (kindLabel[device.kind] ?? 'Устройство')
}

function useDevices() {
  const [list, setList] = useState<DeviceList | null>(null)
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    try {
      setList(await api.devices())
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить устройства.'))
    }
  }, [])
  useEffect(() => {
    void load()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [load])
  return { list, error, load }
}

/** «Устройства» (M84): which devices the account has, what can run on each, and their state. */
export function Devices({ deviceId }: { deviceId?: string }) {
  if (deviceId) return <DeviceCard key={deviceId} deviceId={deviceId} />
  return <DeviceListScreen />
}

function DeviceListScreen() {
  const { list, error } = useDevices()
  return (
    <section className="mw-page" aria-labelledby="devices-title">
      <header className="mw-page__head">
        <h1 id="devices-title">Устройства</h1>
        <p>
          Мия работает в облаке. Телефон или компьютер участвует, только когда вы просите сделать
          что-то на нём, — и с вашим разрешением на самом устройстве.
        </p>
      </header>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      {!list && !error && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      )}
      {list && (
        <>
          <ul className="mw-stack" aria-label="Устройства аккаунта">
            {list.items.map((device) => (
              <li key={device.id}>
                <DeviceRow device={device} serverTime={list.server_time} />
              </li>
            ))}
          </ul>
          <CloudBlock cloud={list.cloud} />
        </>
      )}
    </section>
  )
}

function DeviceRow({ device, serverTime }: { device: DeviceSummary; serverTime: string }) {
  const hint = pushHint(device)
  return (
    <Link
      to={`/devices/${encodeURIComponent(device.id)}`}
      className="mia-glass mw-data-row mw-more-link mw-device-row"
      data-device={device.id}
      data-kind={device.kind}
    >
      <span className="mw-data-row__icon">
        <KindIcon kind={device.kind} />
      </span>
      <span className="mw-data-row__body">
        <span className="mw-device-row__title">
          <strong>{device.name}</strong>
          {device.current && <span className="mia-chip mia-chip--accent">Это устройство</span>}
          <PresenceChip device={device} serverTime={serverTime} />
        </span>
        <span className="mw-muted">{platformLine(device)}</span>
        {device.capabilities_total !== undefined && (
          <span className="mw-muted">
            Возможности: готово {device.capabilities_ready ?? 0} из {device.capabilities_total}
          </span>
        )}
        {hint && <span className="mw-muted mw-device-row__hint">{hint}</span>}
      </span>
      <ChevronRight size={18} aria-hidden="true" />
    </Link>
  )
}

function CloudBlock({ cloud }: { cloud: CloudRow }) {
  return (
    <section
      className="mia-glass mw-block mw-cloud"
      data-device="cloud"
      aria-labelledby="cloud-title"
    >
      <div className="mw-device-row__title">
        <Cloud size={20} aria-hidden="true" />
        <h2 id="cloud-title">{cloud.label}</h2>
        <span className="mia-chip mia-chip--success">онлайн</span>
      </div>
      <p className="mw-muted">{cloud.summary}</p>
      {cloud.plugins.length > 0 && (
        <details>
          <summary>Плагины в облаке: {cloud.plugins.length}</summary>
          <ul className="mw-list mw-where__list">
            {cloud.plugins.map((plugin) => (
              <li key={plugin.plugin_id} data-plugin={plugin.plugin_id}>
                <Cloud size={14} aria-hidden="true" />
                <strong>{plugin.name}</strong>
                <small>{plugin.cloud}</small>
                {plugin.device && <small>На телефоне по просьбе: {plugin.device}</small>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

// ------------------------------------------------------------------------------ the card

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mw-device-facts__row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

const permissionText: Record<string, string> = {
  granted: 'разрешение Android есть',
  denied: 'нет разрешения Android',
  not_required: 'разрешение Android не нужно',
  unknown: 'разрешение Android неизвестно',
}

function capabilityState(item: DeviceCapabilityState): { text: string; tone: string } {
  if (!item.enabled) return { text: 'выключено в Мии', tone: '' }
  if (item.ready) return { text: 'готово', tone: 'mia-chip--success' }
  if (item.os_permission === 'denied')
    return { text: 'нужно разрешение Android', tone: 'mia-chip--warning' }
  return { text: 'неизвестно', tone: '' }
}

function CapabilityItem({ item }: { item: DeviceCapabilityState }) {
  const state = capabilityState(item)
  return (
    <li className="mia-glass mw-block mw-capability" data-capability={item.capability}>
      <div className="mw-device-row__title">
        <strong>{item.label}</strong>
        <span className={`mia-chip ${state.tone}`} data-state={state.text}>
          {state.text}
        </span>
      </div>
      <ul className="mw-list">
        <li>{permissionText[item.os_permission] ?? permissionText.unknown}</li>
        <li>
          {item.remote === 'deny'
            ? 'Другим устройствам запрещено просить это'
            : 'Другие устройства могут попросить — телефон спросит вас'}
        </li>
        <li className="mw-muted">Мия увидит: {item.model_sees}.</li>
      </ul>
      {item.plugins.length > 0 ? (
        <p className="mw-muted">
          Используют:{' '}
          {item.plugins.map((plugin, index) => (
            <span key={plugin.plugin_id} data-plugin={plugin.plugin_id}>
              {index > 0 && ', '}
              <Link to={`/store/plugin/${encodeURIComponent(plugin.plugin_id)}`}>
                {plugin.name}
              </Link>
              {plugin.optional ? ' (по желанию)' : ' (нужно для работы)'}
            </span>
          ))}
        </p>
      ) : (
        <p className="mw-muted">Установленные плагины эту возможность не используют.</p>
      )}
    </li>
  )
}

const decisionText: Record<string, string> = {
  pending: 'ждёт ответа',
  approved: 'разрешено',
  denied: 'отклонено',
  expired: 'время вышло',
  allowed: 'по постоянному разрешению',
  blocked: 'выключено на телефоне',
  offline: 'телефон был недоступен',
}

function AccessLog({ deviceId }: { deviceId: string }) {
  const [items, setItems] = useState<DeviceAccessLogEntry[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    api
      .deviceAccessLog(deviceId)
      .then((page) => setItems(page.items))
      .catch((e: unknown) => setError(messageOf(e, 'Не удалось загрузить журнал.')))
  }, [deviceId])
  if (error)
    return (
      <p className="mw-error" role="alert">
        {error}
      </p>
    )
  if (!items)
    return (
      <p className="mw-muted" role="status">
        <LoaderCircle size={16} className="spin" /> Загружаем журнал…
      </p>
    )
  if (items.length === 0)
    return <p className="mw-muted">Другие устройства ещё ничего не просили.</p>
  return (
    <ul className="mw-list mw-access-log" aria-label="Журнал доступа">
      {items.map((entry) => (
        <li key={entry.id} data-decision={entry.decision}>
          <strong>{entry.capability_label}</strong>
          <span className="mia-chip">{decisionText[entry.decision] ?? entry.decision}</span>
          <small>
            {entry.requester.label} · {when(entry.created_at)}
          </small>
        </li>
      ))}
    </ul>
  )
}

function DeviceCard({ deviceId }: { deviceId: string }) {
  const [device, setDevice] = useState<DeviceDetail | null>(null)
  const [error, setError] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [showLog, setShowLog] = useState(false)

  const load = useCallback(async () => {
    try {
      setDevice(await api.device(deviceId))
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить устройство.'))
    }
  }, [deviceId])
  useEffect(() => {
    void load()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [load])

  async function revoke(targetId: string, grantId: string) {
    try {
      await api.revokeDeviceGrant(targetId, grantId)
      toast.success('Разрешение отозвано')
    } catch (e) {
      toast.error(messageOf(e))
    }
    await load()
  }

  const back = (
    <Link to="/devices" className="mw-back">
      <ArrowLeft size={16} aria-hidden="true" /> Устройства
    </Link>
  )
  if (error && !device)
    return (
      <section className="mw-page">
        {back}
        <p className="mw-error" role="alert">
          {error}
        </p>
      </section>
    )
  if (!device)
    return (
      <section className="mw-page">
        {back}
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      </section>
    )

  const isPhone = device.kind === 'phone'
  const hint = pushHint(device)
  return (
    <section
      className="mw-page mw-device-card"
      aria-labelledby="device-title"
      data-kind={device.kind}
    >
      {back}
      <header className="mw-page__head">
        <div className="mw-device-row__title">
          <span className="mw-data-row__icon">
            <KindIcon kind={device.kind} />
          </span>
          <h1 id="device-title">{device.name}</h1>
        </div>
        <div className="mw-chips">
          <span className="mia-chip">{kindLabel[device.kind] ?? 'Устройство'}</span>
          {device.current && <span className="mia-chip mia-chip--accent">Это устройство</span>}
          <PresenceChip device={device} />
        </div>
        {hint && <p className="mw-muted">{hint}</p>}
      </header>

      <dl className="mia-glass mw-block mw-device-facts">
        {device.kind === 'browser' ? (
          <Fact label="Браузер">{device.platform.client ?? '—'}</Fact>
        ) : (
          <>
            <Fact label="Модель">{device.platform.model ?? '—'}</Fact>
            <Fact label="Система">{device.platform.os ?? '—'}</Fact>
            <Fact label="Версия Мии">{device.platform.app_version ?? '—'}</Fact>
          </>
        )}
        <Fact label="Подключено">{when(device.connected_at)}</Fact>
        <Fact label="Последний контакт">
          {device.presence.status === 'online' ? 'сейчас' : when(device.last_seen_at)}
        </Fact>
        {isPhone && (
          <Fact label="Push">
            {device.push === 'registered'
              ? 'подключён'
              : device.push === 'not_configured'
                ? 'не настроен на сервере'
                : 'нет'}
          </Fact>
        )}
        {isPhone && device.notifications && (
          <Fact label="Уведомления">
            {device.notifications === 'granted'
              ? 'разрешены'
              : device.notifications === 'denied'
                ? 'запрещены в Android'
                : 'неизвестно'}
          </Fact>
        )}
      </dl>

      <div className="mw-actions">
        {device.actions.rename && (
          <button className="mia-btn mia-btn--secondary" onClick={() => setRenaming(true)}>
            <Pencil size={16} aria-hidden="true" /> Переименовать
          </button>
        )}
        {device.actions.disconnect ? (
          <button className="mia-btn mia-btn--danger" onClick={() => setDisconnecting(true)}>
            <Unplug size={16} aria-hidden="true" /> Отключить устройство
          </button>
        ) : (
          <span className="mw-muted">
            Это устройство, с которого вы сейчас работаете. Чтобы отключить его, выйдите из
            аккаунта.
          </span>
        )}
      </div>

      {isPhone ? (
        <>
          <h2 className="mw-section">Что может выполняться на этом телефоне</h2>
          <p className="mw-muted">
            Только по вашей просьбе. Включить, выключить и запретить другим устройствам можно в
            приложении Мия на телефоне: «Настройки» → «Доступ с других устройств».
          </p>
          <ul className="mw-stack" aria-label="Возможности телефона">
            {device.capabilities.map((item) => (
              <CapabilityItem key={item.capability} item={item} />
            ))}
          </ul>

          <h2 className="mw-section">Постоянные разрешения</h2>
          {device.grants.length === 0 ? (
            <p className="mw-muted">
              Нет: каждый запрос с другого устройства телефон подтверждает отдельно.
            </p>
          ) : (
            <ul className="mw-stack" aria-label="Постоянные разрешения">
              {device.grants.map((grant) => (
                <li key={grant.id} className="mia-glass mw-data-row" data-grant={grant.id}>
                  <span className="mw-data-row__body">
                    <strong>
                      {grant.requester.label}: {grant.capability_label}
                    </strong>
                    <span className="mw-muted">
                      до {when(grant.expires_at)}
                      {grant.last_used_at ? ` · использовано ${when(grant.last_used_at)}` : ''}
                    </span>
                  </span>
                  <button
                    className="mia-btn mia-btn--secondary"
                    onClick={() => void revoke(device.id, grant.id)}
                  >
                    Отозвать
                  </button>
                </li>
              ))}
            </ul>
          )}

          <h2 className="mw-section">Журнал доступа</h2>
          {device.links.access_log && !showLog && (
            <button className="mia-btn mia-btn--secondary" onClick={() => setShowLog(true)}>
              <History size={16} aria-hidden="true" /> Показать журнал доступа
            </button>
          )}
          {showLog && <AccessLog deviceId={device.id} />}
        </>
      ) : (
        <>
          <h2 className="mw-section">Что выполняется на этом устройстве</h2>
          <p className="mw-muted">
            {device.kind === 'browser'
              ? 'Ничего: браузер показывает Мию и может попросить телефон о действии — телефон спросит вас.'
              : 'Возможности этого устройства появятся здесь, когда оно их сообщит.'}
          </p>
        </>
      )}

      {device.received_grants.length > 0 && (
        <>
          <h2 className="mw-section">Разрешено этому устройству</h2>
          <ul className="mw-stack" aria-label="Разрешено этому устройству">
            {device.received_grants.map((grant) => (
              <li key={grant.id} className="mia-glass mw-data-row" data-grant={grant.id}>
                <span className="mw-data-row__body">
                  <strong>
                    {grant.target_device_name}: {grant.capability_label}
                  </strong>
                  <span className="mw-muted">до {when(grant.expires_at)}</span>
                </span>
                <button
                  className="mia-btn mia-btn--secondary"
                  onClick={() => void revoke(grant.target_device_id, grant.id)}
                >
                  Отозвать
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {renaming && (
        <RenameDialog
          device={device}
          close={() => setRenaming(false)}
          saved={(next) => {
            setDevice(next)
            setRenaming(false)
          }}
        />
      )}
      {disconnecting && <DisconnectDialog device={device} close={() => setDisconnecting(false)} />}
    </section>
  )
}

function RenameDialog({
  device,
  close,
  saved,
}: {
  device: DeviceDetail
  close: () => void
  saved: (device: DeviceDetail) => void
}) {
  const [name, setName] = useState(device.name)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function save(value: string | null) {
    setBusy(true)
    setError('')
    try {
      saved(await api.renameDevice(device.id, value))
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }

  return (
    <Dialog
      title="Переименовать устройство"
      description="Имя видно только вам: в этом списке и в просьбах на телефоне."
      onClose={() => !busy && close()}
    >
      <form
        className="mw-form"
        onSubmit={(event) => {
          event.preventDefault()
          void save(name.trim() || null)
        }}
      >
        <label>
          <span>Имя</span>
          <input
            value={name}
            maxLength={64}
            autoComplete="off"
            aria-label="Имя устройства"
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
        <div className="mw-dialog__actions">
          {device.custom_name && (
            <button
              type="button"
              className="mia-btn mia-btn--secondary"
              disabled={busy}
              onClick={() => void save(null)}
            >
              Вернуть «{device.default_name}»
            </button>
          )}
          <button type="submit" className="mia-btn mia-btn--primary" disabled={busy}>
            {busy && <LoaderCircle size={16} className="spin" />} Сохранить
          </button>
        </div>
      </form>
    </Dialog>
  )
}

function DisconnectDialog({ device, close }: { device: DeviceDetail; close: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function disconnect() {
    setBusy(true)
    setError('')
    try {
      await api.disconnectDevice(device.id)
      toast.success(`«${device.name}» отключено`)
      navigate('/devices')
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }

  const phone = device.kind === 'phone'
  return (
    <Dialog
      title={`Отключить «${device.name}»?`}
      description={
        phone
          ? 'Мия на этом телефоне выйдет из аккаунта.'
          : 'В этом браузере Мия выйдет из аккаунта.'
      }
      onClose={() => !busy && close()}
    >
      <ul className="mw-danger-list">
        {phone ? (
          <>
            <li>приложение и его страницы выйдут из аккаунта — войти можно снова;</li>
            <li>push на этот телефон больше не придут;</li>
            <li>постоянные разрешения этого телефона и разрешения для него будут отозваны;</li>
            <li>Мия перестанет предлагать действия на этом телефоне.</li>
          </>
        ) : (
          <>
            <li>браузер выйдет из аккаунта — войти можно снова;</li>
            <li>разрешения, которые телефоны дали этому браузеру, будут отозваны.</li>
          </>
        )}
      </ul>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      <div className="mw-dialog__actions">
        <button className="mia-btn mia-btn--secondary" onClick={close} disabled={busy}>
          Отмена
        </button>
        <button
          className="mia-btn mia-btn--danger"
          onClick={() => void disconnect()}
          disabled={busy}
        >
          {busy && <LoaderCircle size={16} className="spin" />} Отключить
        </button>
      </div>
    </Dialog>
  )
}
