import { useCallback, useEffect, useRef, useState } from 'react'
import { Bell, LoaderCircle } from 'lucide-react'
import { toast } from 'sonner'
import {
  api,
  type NotificationChannel,
  type NotificationEventType,
  type NotificationSettings,
  type NotificationSettingsPatch,
} from '../lib/api'
import { messageOf } from '../lib/http'

/** Why a channel cannot reach the user now, and what to do (NotificationChannel.detail). */
const hints: Record<string, string> = {
  push_not_configured: 'Push пока не настроен на сервере.',
  no_push_token: 'Откройте приложение Мия на телефоне и войдите — тогда сюда придут push.',
  not_linked:
    'Скажите Мие в чате «подключи Telegram-бота». Бот спросит, присылать ли сюда уведомления.',
  not_configured: 'Канал пока не подключён на сервере.',
  unavailable: 'Канал сейчас недоступен.',
}

/** What the screen shows right after a click, before the server answers. */
function applied(
  settings: NotificationSettings,
  patch: NotificationSettingsPatch,
): NotificationSettings {
  const matrix = { ...settings.matrix }
  for (const [kind, cells] of Object.entries(patch.matrix ?? {}))
    matrix[kind as NotificationEventType] = { ...matrix[kind as NotificationEventType], ...cells }
  return {
    ...settings,
    channels: settings.channels.map((channel) =>
      channel.name in (patch.channels ?? {})
        ? { ...channel, enabled: patch.channels![channel.name], decided: true }
        : channel,
    ),
    matrix,
  }
}

function hintOf(channel: NotificationChannel): string {
  return (channel.detail && hints[channel.detail]) || 'Канал не подключён.'
}

/**
 * «Уведомления» (M77): where Mia's notifications arrive. The inbox always gets everything; every
 * other channel has a switch, and the table decides which kinds of events go to which channel.
 * A newly connected channel (the Telegram bot) asks the question itself, so its switch starts off.
 */
export function Notifications() {
  const [settings, setSettings] = useState<NotificationSettings | null>(null)
  const [error, setError] = useState('')
  const latest = useRef(0)

  const load = useCallback(async () => {
    try {
      setSettings(await api.notificationSettings())
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить настройки уведомлений.'))
    }
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  // Changes are merges, so quick clicks can overlap; only the answer to the last one is shown.
  async function change(patch: NotificationSettingsPatch) {
    const mine = ++latest.current
    setSettings((current) => (current ? applied(current, patch) : current))
    try {
      const saved = await api.updateNotificationSettings(patch)
      if (mine === latest.current) setSettings(saved)
    } catch (e) {
      toast.error(messageOf(e))
      if (mine === latest.current) await load()
    }
  }

  function toggleChannel(channel: NotificationChannel, enabled: boolean) {
    void change({ channels: { [channel.name]: enabled } })
  }

  function toggleCell(kind: NotificationEventType, channel: NotificationChannel, value: boolean) {
    void change({ matrix: { [kind]: { [channel.name]: value } } })
  }

  return (
    <section className="mw-page" aria-labelledby="notifications-title">
      <header className="mw-page__head">
        <h1 id="notifications-title">Уведомления</h1>
        <p>
          Решайте сами, куда Мия присылает таймеры, результаты задач и вопросы. «Входящие» получают
          всё всегда; ниже — остальные каналы.
        </p>
      </header>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      {!settings && !error && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      )}
      {settings && (
        <>
          <h2 className="mw-section">Куда присылать</h2>
          <ul className="mw-stack" aria-label="Каналы уведомлений">
            <li className="mia-glass mw-data-row" data-channel="inbox">
              <span className="mw-data-row__icon" aria-hidden="true">
                <Bell size={20} />
              </span>
              <div className="mw-data-row__body">
                <h3>Входящие</h3>
                <p>Все уведомления сохраняются здесь, это нельзя отключить.</p>
              </div>
              <span className="mia-chip mia-chip--success">Всегда</span>
            </li>
            {settings.channels.map((channel) => {
              const connected = channel.status === 'connected'
              return (
                <li
                  key={channel.name}
                  className="mia-glass mw-data-row"
                  data-channel={channel.name}
                >
                  <span className="mw-data-row__icon" aria-hidden="true">
                    <Bell size={20} />
                  </span>
                  <div className="mw-data-row__body">
                    <h3>{channel.label}</h3>
                    <p>
                      <span className={`mia-chip ${connected ? 'mia-chip--success' : ''}`}>
                        {connected ? 'Подключён' : 'Не подключён'}
                      </span>{' '}
                      {!connected && <span className="mw-muted">{hintOf(channel)}</span>}
                      {connected && !channel.decided && !channel.enabled && (
                        <span className="mw-muted">Сюда пока ничего не приходит.</span>
                      )}
                    </p>
                  </div>
                  <label className="mw-switch">
                    <input
                      type="checkbox"
                      checked={channel.enabled}
                      aria-label={`Присылать в канал «${channel.label}»`}
                      onChange={(event) => toggleChannel(channel, event.target.checked)}
                    />
                    <span>{channel.enabled ? 'Включён' : 'Выключен'}</span>
                  </label>
                </li>
              )
            })}
          </ul>

          <h2 className="mw-section">Что и куда</h2>
          <p className="mw-muted" id="matrix-hint">
            Отметьте, какие уведомления идут в какой канал. Выключенный канал ничего не получает.
          </p>
          <div
            className="mw-matrix mia-glass"
            role="region"
            aria-label="Таблица уведомлений"
            tabIndex={0}
          >
            <table aria-describedby="matrix-hint">
              <thead>
                <tr>
                  <th scope="col">Что случилось</th>
                  <th scope="col">Входящие</th>
                  {settings.channels.map((channel) => (
                    <th scope="col" key={channel.name}>
                      {channel.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {settings.event_types.map((kind) => (
                  <tr key={kind.id} data-event={kind.id}>
                    <th scope="row">{kind.label}</th>
                    <td>
                      <input
                        type="checkbox"
                        checked
                        disabled
                        aria-label={`${kind.label} → Входящие (всегда)`}
                      />
                    </td>
                    {settings.channels.map((channel) => (
                      <td key={channel.name}>
                        <input
                          type="checkbox"
                          checked={settings.matrix[kind.id]?.[channel.name] ?? true}
                          disabled={!channel.enabled}
                          aria-label={`${kind.label} → ${channel.label}`}
                          onChange={(event) => toggleCell(kind.id, channel, event.target.checked)}
                        />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mw-muted">
            Если задача или расписание заказаны с конкретным каналом, он важнее этих настроек.
          </p>
        </>
      )}
    </section>
  )
}
