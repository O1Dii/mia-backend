import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  Brain,
  KeyRound,
  LoaderCircle,
  MonitorSmartphone,
  Plug,
  ShieldCheck,
  Unplug,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, type ConnectedApp, type MemorySettings, type Retention } from '../lib/api'
import { useAuth } from '../lib/auth'
import { messageOf } from '../lib/http'
import { Link } from '../lib/router'
import { Dialog } from './Dialog'

type Counts = {
  facts: number | null
  people: number | null
  connections: number | null
  models: number | null
  plugins: number | null
}

const retentionText: Record<Retention, string> = {
  '30d': 'история разговоров хранится 30 дней',
  '1y': 'история разговоров хранится 1 год',
  forever: 'история разговоров хранится без ограничения',
  none: 'история разговоров не хранится',
}

function Row({
  icon,
  title,
  children,
  to,
  link,
}: {
  icon: ReactNode
  title: string
  children: ReactNode
  to?: string
  link?: string
}) {
  return (
    <li className="mia-glass mw-data-row">
      <span className="mw-data-row__icon" aria-hidden="true">
        {icon}
      </span>
      <div className="mw-data-row__body">
        <h3>{title}</h3>
        <p>{children}</p>
      </div>
      {to && (
        <Link to={to} className="mia-btn mia-btn--ghost">
          {link}
        </Link>
      )}
    </li>
  )
}

/** «Где ваши данные»: where each kind of data lives, who can see it and how to remove it. */
export function Privacy() {
  const auth = useAuth()
  const [counts, setCounts] = useState<Counts | null>(null)
  const [settings, setSettings] = useState<MemorySettings | null>(null)
  const [apps, setApps] = useState<ConnectedApp[] | null>(null)
  const [leaving, setLeaving] = useState<ConnectedApp | null>(null)

  const load = useCallback(async () => {
    const [facts, people, connections, models, installs, memory, connected] =
      await Promise.allSettled([
        api.facts('active'),
        api.people('active'),
        api.connections(),
        api.models(),
        api.installations(),
        api.memorySettings(),
        api.apps(),
      ])
    const count = <T,>(result: PromiseSettledResult<{ items: T[] }>) =>
      result.status === 'fulfilled' ? result.value.items.length : null
    setCounts({
      facts: count(facts),
      people: count(people),
      connections: count(connections),
      models: count(models),
      plugins: count(installs),
    })
    setSettings(memory.status === 'fulfilled' ? memory.value : null)
    setApps(connected.status === 'fulfilled' ? connected.value.items : [])
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  const n = (value: number | null) => (value === null ? '—' : String(value))

  return (
    <section className="mw-page" aria-labelledby="data-title">
      <header className="mw-page__head">
        <h1 id="data-title">Где ваши данные</h1>
        <p>
          Коротко и без юридических формулировок: что Мия хранит, что видит модель, что остаётся в
          браузере и как всё это удалить.
        </p>
      </header>
      {!counts && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      )}
      <h2 className="mw-section">У Мии на сервере</h2>
      <ul className="mw-stack" aria-label="Что хранится на сервере">
        <Row icon={<ShieldCheck size={20} />} title="Аккаунт">
          Вход через Google: хранятся адрес почты и имя ({auth.user.email}). Пароля у Мии нет.
        </Row>
        <Row icon={<Brain size={20} />} title="Память" to="/memory" link="Открыть память">
          Фактов: {n(counts?.facts ?? null)}, людей: {n(counts?.people ?? null)}. Каждая запись
          зашифрована вашим личным ключом; даже поиск по памяти идёт по обезличенному индексу.{' '}
          {settings ? `Сейчас ${retentionText[settings.history_retention]}.` : ''}
        </Row>
        <Row
          icon={<Plug size={20} />}
          title="Плагины и их аккаунты"
          to="/installed"
          link="Мои плагины"
        >
          Установлено: {n(counts?.plugins ?? null)}, подключённых аккаунтов:{' '}
          {n(counts?.connections ?? null)}. Токены входа лежат в зашифрованном хранилище и
          отзываются у сервиса при удалении плагина. Данные плагина (таймеры, заметки) удаляются
          вместе с ним.
        </Row>
        <Row icon={<KeyRound size={20} />} title="Ключи моделей" to="/models" link="Модели">
          Подключений: {n(counts?.models ?? null)}. Ключ шифруется на сервере и показывается только
          как последние четыре символа. Плагины ключа не видят.
        </Row>
      </ul>

      <h2 className="mw-section">Что видит модель</h2>
      <ul className="mw-stack" aria-label="Что видит модель">
        <Row icon={<Brain size={20} />} title="Только нужное для ответа">
          Ваше сообщение, немного релевантных фактов и имена людей из памяти. Телефоны, почты и
          длинные идентификаторы из памяти в запрос не попадают. Запросы идут к провайдеру, чей ключ
          вы подключили.
        </Row>
        <Row icon={<ShieldCheck size={20} />} title="Действия — только с вашего согласия">
          Всё, что что-то меняет во внешних сервисах, Мия сначала показывает на подтверждение.
          Плитки дашборда не могут запускать такие действия.
        </Row>
      </ul>

      <h2 className="mw-section">В этом браузере</h2>
      <ul className="mw-stack" aria-label="Что хранится в браузере">
        <Row icon={<MonitorSmartphone size={20} />} title="Сессия и настройки">
          Служебный cookie сессии (недоступен скриптам страницы) и настройки голоса. Плитки плагинов
          открываются в изолированной рамке: они не видят ни cookie, ни страницу, ни ваши данные,
          кроме того, что получают от самого плагина.
        </Row>
      </ul>

      <h2 className="mw-section">Подключённые приложения</h2>
      {apps === null ? (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      ) : apps.length === 0 ? (
        <p className="mw-muted">Сторонние приложения к вашей Мии не подключены.</p>
      ) : (
        <ul className="mw-stack" aria-label="Подключённые приложения">
          {apps.map((app) => (
            <li key={app.id} className="mia-glass mw-data-row" data-app={app.client_id}>
              <span className="mw-data-row__icon" aria-hidden="true">
                <Unplug size={20} />
              </span>
              <div className="mw-data-row__body">
                <h3>{app.client_name}</h3>
                <p>
                  Доступы: {app.scopes.length}. Траты за месяц: {app.month_spend_usd ?? '0'} из{' '}
                  {app.monthly_limit_usd} $.
                </p>
              </div>
              <button
                className="mia-btn mia-btn--ghost"
                aria-label={`Отключить приложение ${app.client_name}`}
                onClick={() => setLeaving(app)}
              >
                Отключить
              </button>
            </li>
          ))}
        </ul>
      )}

      <h2 className="mw-section">Забрать или удалить</h2>
      <div className="mia-glass mw-block">
        <p>
          Выгрузка памяти файлом, полное удаление памяти и срок хранения истории — на странице
          памяти. Удаление плагина стирает его данные и отзывает доступ. Выход из аккаунта закрывает
          сессию на сервере.
        </p>
        <div className="mw-actions">
          <Link to="/memory" className="mia-btn mia-btn--secondary">
            Выгрузить или удалить память
          </Link>
          <button className="mia-btn mia-btn--ghost" onClick={() => void auth.logout()}>
            Выйти
          </button>
        </div>
      </div>

      {leaving && (
        <Dialog
          title={`Отключить ${leaving.client_name}?`}
          description="Все токены приложения перестанут работать сразу."
          onClose={() => setLeaving(null)}
        >
          <div className="mw-dialog__actions">
            <button className="mia-btn mia-btn--secondary" onClick={() => setLeaving(null)}>
              Отмена
            </button>
            <button
              className="mia-btn mia-btn--danger"
              onClick={async () => {
                try {
                  await api.revokeApp(leaving.id)
                  toast.success('Приложение отключено')
                  setLeaving(null)
                  await load()
                } catch (e) {
                  toast.error(messageOf(e))
                }
              }}
            >
              Отключить
            </button>
          </div>
        </Dialog>
      )}
    </section>
  )
}
