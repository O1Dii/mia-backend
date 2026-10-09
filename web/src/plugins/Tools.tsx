import { useState } from 'react'
import {
  Bell,
  Check,
  ExternalLink,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Trash2,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, type Connection, type PluginState } from '../lib/legacyApi'
import { useWorkspace } from '../lib/workspace'
import type { Plugin } from './catalog'
import { formatDuration, unlockAudio } from './scheduler'
import { PluginFrame } from './ViewBridge'

export function Tool({
  plugin,
  now,
  active,
  openModels,
  compact = false,
}: {
  plugin: Plugin
  now: number
  active?: Connection
  openModels: () => void
  compact?: boolean
}) {
  if (plugin.id === 'mia.clock') return <WorldClock now={now} />
  if (plugin.id === 'mia.notes') return <Notes />
  if (plugin.id === 'mia.alarm') return <Alarm now={now} />
  if (plugin.id === 'mia.timer' || plugin.id === 'mia.focus')
    return <Countdown plugin={plugin} now={now} />
  if (plugin.id === 'mia.digest')
    return <Digest active={active} openModels={openModels} compact={compact} />
  if (plugin.id === 'mia.web') return <InternetTool />
  if (plugin.manifest?.view.type === 'html') return <PluginFrame plugin={plugin} />
  return <CommunityTool plugin={plugin} active={active} openModels={openModels} />
}

function useCommand() {
  const { act } = useWorkspace()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const run = async (name: string, args: Record<string, unknown> = {}) => {
    setBusy(true)
    setError('')
    try {
      return await act(name, args)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Действие не выполнено')
      return null
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, run }
}

function WorldClock({ now }: { now: number }) {
  const { snapshot } = useWorkspace()
  const { run, busy } = useCommand()
  const zones = snapshot?.states['mia.clock']?.zones ?? [
    'Europe/Minsk',
    'Europe/London',
    'Asia/Tokyo',
  ]
  const [zone, setZone] = useState('America/New_York')
  const options = [
    'Europe/Minsk',
    'Europe/London',
    'Europe/Berlin',
    'Europe/Paris',
    'Asia/Tokyo',
    'Asia/Dubai',
    'Asia/Shanghai',
    'America/New_York',
    'America/Los_Angeles',
    'Australia/Sydney',
    'UTC',
  ]
  return (
    <div className="tool-body">
      <div className="world-clocks">
        {zones.map((item) => (
          <div className="world-clock" key={item}>
            <span>{item.replaceAll('_', ' ')}</span>
            <strong>
              {new Intl.DateTimeFormat('ru', {
                timeZone: item,
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
              }).format(now)}
            </strong>
            <small>
              {new Intl.DateTimeFormat('ru', {
                timeZone: item,
                weekday: 'short',
                day: 'numeric',
                month: 'long',
              }).format(now)}
            </small>
            <button
              className="icon-button"
              disabled={busy}
              aria-label={`Удалить ${item}`}
              onClick={() => void run('mia_clock__remove_zone', { timezone: item })}
            >
              <Trash2 size={15} />
            </button>
          </div>
        ))}
      </div>
      <div className="inline-form">
        <select aria-label="Часовой пояс" value={zone} onChange={(e) => setZone(e.target.value)}>
          {options.map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
        <button
          className="button secondary"
          disabled={busy || zones.includes(zone)}
          onClick={() => void run('mia_clock__add_zone', { timezone: zone })}
        >
          <Plus size={16} /> Добавить
        </button>
      </div>
    </div>
  )
}

function Notes() {
  const { snapshot } = useWorkspace()
  const notes = snapshot?.states['mia.notes']?.notes ?? []
  const { run, busy } = useCommand()
  const [text, setText] = useState('')
  return (
    <div className="tool-body">
      <form
        className="note-form"
        onSubmit={async (e) => {
          e.preventDefault()
          if (await run('mia_notes__add', { text: text.trim() })) setText('')
        }}
      >
        <textarea
          aria-label="Новая заметка"
          placeholder="Идея, задача или список покупок…"
          maxLength={5000}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="button primary" disabled={busy || !text.trim()}>
          <Plus size={16} /> Сохранить мысль
        </button>
      </form>
      <div className="notes-list">
        {notes.length === 0 && (
          <p className="empty-copy">Напишите здесь или попросите Мию сохранить мысль в чате.</p>
        )}
        {notes.map((note) => (
          <div className={`note-row ${note.done ? 'done' : ''}`} key={note.id}>
            <button
              className="note-check"
              aria-label={note.done ? 'Вернуть задачу' : 'Выполнить задачу'}
              disabled={busy}
              onClick={() => void run('mia_notes__toggle', { id: note.id })}
            >
              {note.done && <Check size={13} />}
            </button>
            <p>{note.text}</p>
            <button
              className="icon-button"
              aria-label="Удалить заметку"
              disabled={busy}
              onClick={() => void run('mia_notes__delete', { id: note.id })}
            >
              <Trash2 size={16} />
            </button>
          </div>
        ))}
      </div>
      <p className="field-note">Общее состояние для чата, дашборда и подключённых приложений.</p>
    </div>
  )
}

function Countdown({ plugin, now }: { plugin: Plugin; now: number }) {
  const { snapshot } = useWorkspace()
  const timer = snapshot?.states[plugin.id] ?? {}
  const { run, busy } = useCommand()
  const focus = plugin.id === 'mia.focus'
  const [minutes, setMinutes] = useState(focus ? 25 : 5)
  const deadline = timer.deadline
  const running = Boolean(deadline && deadline > now)
  const paused = timer.status === 'paused' && (timer.remaining ?? 0) > 0
  const remaining = deadline
    ? Math.max(0, deadline - now)
    : paused
      ? timer.remaining!
      : minutes * 60000
  const prefix = plugin.id.replace('.', '_')
  const setPreset = async (value: number) => {
    setMinutes(value)
    if (timer.deadline || paused) await run(`${prefix}__cancel`)
  }
  return (
    <div className="tool-body timer-tool">
      <span className="eyebrow">
        {timer.label ?? (focus ? 'ПРОСТРАНСТВО ДЛЯ ВАЖНОГО' : 'ВРЕМЯ ДЛЯ СЕБЯ')}
      </span>
      <div className={`countdown-display ${running ? 'running' : ''}`}>
        {formatDuration(remaining)}
      </div>
      <div className="timer-presets">
        {(focus ? [25, 5, 50] : [1, 5, 10, 30]).map((value) => (
          <button
            className={`chip ${minutes === value ? 'selected' : ''}`}
            key={value}
            disabled={busy}
            onClick={() => void setPreset(value)}
          >
            {value} мин{focus && value === 5 ? ' · перерыв' : ''}
          </button>
        ))}
      </div>
      {!focus && (
        <label className="duration-field">
          Своё время, мин
          <input
            aria-label="Длительность в минутах"
            type="number"
            min={0.02}
            max={180}
            step="any"
            value={minutes}
            onChange={(e) => setMinutes(Number(e.target.value))}
          />
        </label>
      )}
      <div className="timer-actions">
        <button
          className="button primary"
          disabled={busy || !Number.isFinite(minutes) || minutes <= 0 || minutes > 180}
          onClick={() => {
            unlockAudio()
            void run(
              `${prefix}__${running ? 'pause' : paused ? 'resume' : 'start'}`,
              running || paused
                ? {}
                : { seconds: Math.max(1, Math.round(minutes * 60)), label: plugin.name },
            )
          }}
        >
          {busy ? (
            <LoaderCircle className="spin" size={17} />
          ) : running ? (
            <Pause size={17} />
          ) : (
            <Play size={17} />
          )}
          {running ? 'Пауза' : paused ? 'Продолжить' : 'Начать'}
        </button>
        <button
          className="button secondary"
          disabled={busy}
          onClick={() => void run(`${prefix}__cancel`)}
        >
          <RotateCcw size={16} /> Сбросить
        </button>
      </div>
      <p className="field-note">
        Можно сказать в чате: «
        {focus ? 'Начни фокус-сессию на 25 минут' : 'Поставь таймер на 5 минут'}». Для звука
        оставьте вкладку открытой.
      </p>
    </div>
  )
}

function Alarm({ now }: { now: number }) {
  const { snapshot } = useWorkspace()
  const alarm = snapshot?.states['mia.alarm']
  const { run, busy } = useCommand()
  const [time, setTime] = useState('07:30')
  const [label, setLabel] = useState('Время для нового дня')
  return (
    <div className="tool-body alarm-tool">
      <label>
        Когда начать?
        <input
          aria-label="Время будильника"
          className="time-input"
          type="time"
          required
          value={time}
          onChange={(e) => setTime(e.target.value)}
        />
      </label>
      <label>
        О чём напомнить
        <input
          aria-label="Название будильника"
          value={label}
          maxLength={80}
          onChange={(e) => setLabel(e.target.value)}
        />
      </label>
      <button
        className="button primary"
        disabled={!time || busy}
        onClick={async () => {
          const [hour, minute] = time.split(':').map(Number)
          const date = new Date()
          date.setHours(hour, minute, 0, 0)
          if (date.getTime() <= Date.now()) date.setDate(date.getDate() + 1)
          unlockAudio()
          if (
            await run('mia_alarm__set', {
              at: date.toISOString(),
              label: label.trim() || 'Будильник',
            })
          )
            toast.success('Будильник установлен')
        }}
      >
        <Bell size={16} /> Установить будильник
      </button>
      {alarm?.deadline && alarm.deadline > now && (
        <div className="alarm-summary">
          <span>
            <strong>{alarm.label}</strong>
            <small>
              {new Date(alarm.deadline).toLocaleString('ru')} · через{' '}
              {formatDuration(alarm.deadline - now)}
            </small>
          </span>
          <button
            className="icon-button"
            aria-label="Отменить будильник"
            onClick={() => void run('mia_alarm__cancel')}
          >
            <Trash2 size={17} />
          </button>
        </div>
      )}
      <p className="field-note">
        Одно напоминание по времени устройства. Можно настроить из чата. Вкладка должна оставаться
        открытой.
      </p>
    </div>
  )
}

export function Sources({ items }: { items: PluginState['items'] }) {
  return (
    <ol className="news-sources">
      {items?.map((item, i) => (
        <li key={`${item.url}-${i}`}>
          <a
            href={item.url.startsWith('https://') ? item.url : undefined}
            target="_blank"
            rel="noopener noreferrer"
          >
            <span>
              [{i + 1}] {item.title}
            </span>
            <ExternalLink size={13} />
          </a>
          <small>
            {item.source}
            {item.published_at ? ` · ${new Date(item.published_at).toLocaleString('ru')}` : ''}
          </small>
        </li>
      ))}
    </ol>
  )
}

function Digest({
  active,
  openModels,
  compact,
}: {
  active?: Connection
  openModels: () => void
  compact: boolean
}) {
  const { snapshot, accept } = useWorkspace()
  const state = snapshot?.states['mia.digest'] ?? {}
  const [topics, setTopics] = useState(state.topics ?? 'Локальные модели и искусственный интеллект')
  const [interval, setInterval] = useState(state.interval_minutes ?? 0)
  const [settings, setSettings] = useState(!state.summary)
  const { run, busy, error } = useCommand()
  return (
    <div className="tool-body digest-tool">
      {!active && (
        <div className="inline-notice">
          <p>Подключите модель — Интернет соберёт источники, модель подготовит сводку.</p>
          <button className="text-button" onClick={openModels}>
            Подключить модель →
          </button>
        </div>
      )}
      <div className="digest-toolbar">
        <span className="eyebrow">
          {state.updated_at
            ? `ОБНОВЛЕНО ${new Date(state.updated_at).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })}`
            : 'НОВОСТИ ЗА 48 ЧАСОВ'}
        </span>
        <button className="text-button" onClick={() => setSettings(!settings)}>
          {settings ? 'Скрыть настройки' : 'Темы и расписание'}
        </button>
      </div>
      {settings && (
        <div className="digest-settings">
          <label>
            Темы новостей
            <input
              aria-label="Темы новостей"
              maxLength={200}
              value={topics}
              onChange={(e) => setTopics(e.target.value)}
              placeholder="Например: локальные модели, робототехника"
            />
          </label>
          <label>
            Автообновление
            <select
              aria-label="Автообновление сводки"
              value={interval}
              onChange={(e) => setInterval(Number(e.target.value))}
            >
              <option value={0}>Вручную</option>
              <option value={30}>Каждые 30 минут</option>
              <option value={60}>Каждый час</option>
              <option value={180}>Каждые 3 часа</option>
            </select>
          </label>
          <p className="field-note">
            Автообновление работает, пока локальный сервер запущен и модель подключена. Каждый
            запуск обращается к выбранной модели и может расходовать её баланс.
          </p>
          <button
            className="button secondary"
            onClick={async () => {
              try {
                accept(await api.digestSettings(topics, interval))
                toast.success('Темы и расписание сохранены')
              } catch (e) {
                toast.error(e instanceof Error ? e.message : 'Ошибка сохранения')
              }
            }}
          >
            Сохранить настройки
          </button>
        </div>
      )}
      <button
        className="button primary"
        disabled={!active || busy || topics.trim().length < 2}
        onClick={() => void run('mia_digest__refresh', { topics: topics.trim() })}
      >
        {busy ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}
        {busy ? 'Ищем новости и собираем сводку…' : 'Собрать свежие новости'}
      </button>
      {(error || state.error) && (
        <p className="error-message" role="alert">
          {error || state.error}
        </p>
      )}
      {state.summary ? (
        <>
          <div className="ai-result">
            <span className="eyebrow">
              {state.topics} · {state.model}
            </span>
            <p>{state.summary}</p>
          </div>
          <details className="source-details" open={!compact}>
            <summary>Источники · {state.items?.length ?? 0}</summary>
            <Sources items={state.items} />
          </details>
          <p className="field-note">
            Сводка по заголовкам и описаниям найденных публикаций; полные статьи не прочитаны.
          </p>
        </>
      ) : (
        <div className="digest-empty">
          <Sparkles size={25} />
          <p>
            Свежие материалы по вашим темам.
            <br />
            Без копирования статей вручную.
          </p>
        </div>
      )}
    </div>
  )
}

function InternetTool() {
  const { snapshot } = useWorkspace()
  const state = snapshot?.states['mia.web']
  const { run, busy, error } = useCommand()
  const [query, setQuery] = useState('')
  const [content, setContent] = useState('')
  return (
    <div className="tool-body">
      <p className="field-note">
        Теперь ваша модель может искать и читать интернет через инструменты. Источники не получают
        ваш API-ключ.
      </p>
      <label>
        Поисковый запрос или HTTPS-ссылка
        <input
          aria-label="Запрос в интернет"
          maxLength={2048}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      <button
        className="button primary"
        disabled={busy || query.trim().length < 2}
        onClick={async () => {
          const isURL = query.startsWith('https://')
          const result = await run(
            `mia_web__${isURL ? 'read' : 'search'}`,
            isURL ? { url: query } : { query },
          )
          if (result?.text) setContent(String(result.text))
        }}
      >
        {busy ? <LoaderCircle className="spin" size={16} /> : <ExternalLink size={16} />} Найти или
        прочитать
      </button>
      {error && (
        <p className="error-message" role="alert">
          {error}
        </p>
      )}
      {content && (
        <div className="ai-result">
          <p>{content}</p>
        </div>
      )}
      <Sources items={state?.items} />
    </div>
  )
}

export async function toolName(pluginId: string, action: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(pluginId))
  const hash = Array.from(new Uint8Array(digest))
    .map((n) => n.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 10)
  return `app_${hash}__${action}`
}

function CommunityTool({
  plugin,
  active,
  openModels,
}: {
  plugin: Plugin
  active?: Connection
  openModels: () => void
}) {
  const { snapshot, refresh } = useWorkspace()
  const state = snapshot?.states[plugin.id] ?? {}
  const { run, busy } = useCommand()
  const [input, setInput] = useState('')
  const [answer, setAnswer] = useState('')
  const [asking, setAsking] = useState(false)
  const manifest = plugin.manifest
  if (!manifest)
    return (
      <p className="field-note">Переустановите плагин, чтобы подключить его skill к runtime.</p>
    )
  return (
    <div className="tool-body community-tool">
      <div className="community-fields">
        {manifest.view.fields.map((field) => (
          <div key={field}>
            <small>{field}</small>
            <p>{String(state[field] ?? 'Пока пусто — задайте значение через Мию')}</p>
          </div>
        ))}
      </div>
      {manifest.actions.map((action) => (
        <ActionForm
          key={action.name}
          pluginId={plugin.id}
          action={action}
          disabled={busy}
          onRun={run}
        />
      ))}
      <div className="plugin-ai-command">
        <label>
          Попросить выбранную модель
          <textarea
            aria-label="Запрос модели в плагине"
            rows={2}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            maxLength={8000}
            placeholder={`Например: измени ${plugin.name.toLowerCase()}…`}
          />
        </label>
        <button
          className="button secondary"
          disabled={asking || !input.trim()}
          onClick={async () => {
            if (!active) {
              openModels()
              return
            }
            setAsking(true)
            try {
              const result = await api.chat(active.id, [{ role: 'user', content: input }], {
                plugin_id: plugin.id,
              })
              setAnswer(result.text)
              await refresh()
            } catch (e) {
              toast.error(e instanceof Error ? e.message : 'Модель недоступна')
            } finally {
              setAsking(false)
            }
          }}
        >
          {asking ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}
          {active ? `Спросить ${active.label}` : 'Подключить модель'}
        </button>
        {answer && (
          <div className="ai-result">
            <p>{answer}</p>
          </div>
        )}
      </div>
      {manifest.downloads.length > 0 && (
        <div className="app-downloads">
          {manifest.downloads.map((d) => (
            <a
              key={d.url}
              className="button secondary"
              href={d.url}
              target="_blank"
              rel="noopener noreferrer"
            >
              {d.label} · {d.platform}
              <ExternalLink size={14} />
            </a>
          ))}
          <p className="field-note">Внешние ссылки автора. Проверяйте источник загрузки.</p>
        </div>
      )}
    </div>
  )
}

function ActionForm({
  pluginId,
  action,
  disabled,
  onRun,
}: {
  pluginId: string
  action: NonNullable<Plugin['manifest']>['actions'][number]
  disabled: boolean
  onRun: (name: string, args: Record<string, unknown>) => Promise<Record<string, unknown> | null>
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  return (
    <form
      className="plugin-action-form"
      onSubmit={async (e) => {
        e.preventDefault()
        const args = Object.fromEntries(
          Object.entries(action.inputs).map(([key, spec]) => [
            key,
            spec.type === 'integer'
              ? Number(values[key] ?? 0)
              : spec.type === 'boolean'
                ? values[key] === 'true'
                : (values[key] ?? ''),
          ]),
        )
        await onRun(await toolName(pluginId, action.name), args)
      }}
    >
      {Object.entries(action.inputs).map(([key, spec]) => (
        <label key={key}>
          {spec.description || key}
          {spec.type === 'boolean' ? (
            <select
              value={values[key] ?? 'false'}
              onChange={(e) => setValues({ ...values, [key]: e.target.value })}
            >
              <option value="false">Нет</option>
              <option value="true">Да</option>
            </select>
          ) : (
            <textarea
              aria-label={key === 'input' ? 'Текст для AI-плагина' : key}
              rows={2}
              maxLength={spec.maxLength}
              value={values[key] ?? ''}
              onChange={(e) => setValues({ ...values, [key]: e.target.value })}
            />
          )}
        </label>
      ))}
      <button className="button primary" disabled={disabled}>
        {disabled ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />}{' '}
        {action.effect === 'model.prompt' ? 'Запустить рецепт' : action.name}
      </button>
    </form>
  )
}
