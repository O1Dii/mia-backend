import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  Brain,
  Check,
  Download,
  LoaderCircle,
  Pencil,
  Plus,
  Trash2,
  Upload,
  UserRound,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  api,
  type Fact,
  type MemoryAccess,
  type MemoryCategory,
  type MemorySettings,
  type Person,
  type Preference,
  type Retention,
} from '../lib/api'
import { downloadFile, when } from '../lib/format'
import { apiUrl, messageOf } from '../lib/http'
import { Link } from '../lib/router'
import { Dialog } from './Dialog'

export const categoryLabel: Record<MemoryCategory, string> = {
  profile: 'О вас',
  work: 'Работа',
  home: 'Дом',
  health: 'Здоровье',
  finance: 'Финансы',
  interests: 'Интересы',
  other: 'Другое',
}
const categories = Object.keys(categoryLabel) as MemoryCategory[]
const sensitiveCategories: MemoryCategory[] = ['health', 'finance']

const sourceLabel: Record<string, string> = {
  user_said: 'Вы сказали',
  turn: 'Из разговора',
  plugin: 'Плагин',
  import: 'Импорт',
  app: 'Приложение',
  device_contacts: 'Контакты телефона',
  telegram: 'Telegram',
}
const actorLabel: Record<MemoryAccess['actor'], string> = {
  run: 'Ответ Мии',
  user: 'Вы',
  app: 'Приложение',
  system: 'Система',
}
const retentionLabel: Record<Retention, string> = {
  '30d': '30 дней',
  '1y': '1 год',
  forever: 'Без ограничения',
  none: 'Не хранить историю',
}
const identifierLabel: Record<string, string> = {
  phone: 'телефон',
  email: 'почта',
  telegram_user_id: 'Telegram',
  telegram_username: 'Telegram',
}

type Tab = 'suggested' | 'facts' | 'people' | 'prefs' | 'privacy'

type Data = {
  facts: Fact[]
  suggestedFacts: Fact[]
  people: Person[]
  suggestedPeople: Person[]
  prefs: Preference[]
  settings: MemorySettings
  access: MemoryAccess[]
}

async function loadAll(): Promise<Data> {
  const [facts, suggestedFacts, people, suggestedPeople, prefs, settings, access] =
    await Promise.all([
      api.facts('active'),
      api.facts('suggested'),
      api.people('active'),
      api.people('suggested'),
      api.preferences(),
      api.memorySettings(),
      api.memoryAccess(30),
    ])
  return {
    facts: facts.items,
    suggestedFacts: suggestedFacts.items,
    people: people.items,
    suggestedPeople: suggestedPeople.items,
    prefs: prefs.items,
    settings,
    access: access.items,
  }
}

/** «Что Мия знает»: everything remembered about the user, with one-tap control over each record. */
export function Memory() {
  const [data, setData] = useState<Data | null>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<Tab>('facts')
  const [dialog, setDialog] = useState<ReactNode>(null)

  const reload = useCallback(async () => {
    try {
      const next = await loadAll()
      setData(next)
      setError('')
      return next
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить память.'))
      return null
    }
  }, [])
  useEffect(() => {
    void reload().then((next) => {
      if (next && next.suggestedFacts.length + next.suggestedPeople.length > 0) setTab('suggested')
    })
  }, [reload])

  const suggestedCount = (data?.suggestedFacts.length ?? 0) + (data?.suggestedPeople.length ?? 0)
  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: 'suggested', label: 'Предложено', count: suggestedCount },
    { id: 'facts', label: 'Факты', count: data?.facts.length },
    { id: 'people', label: 'Люди', count: data?.people.length },
    { id: 'prefs', label: 'Предпочтения', count: data?.prefs.length },
    { id: 'privacy', label: 'Данные и приватность' },
  ]
  const close = () => setDialog(null)
  const changed = async (message: string) => {
    toast.success(message)
    await reload()
  }

  return (
    <section className="mw-page" aria-labelledby="memory-title">
      <header className="mw-page__head">
        <h1 id="memory-title">Что Мия знает</h1>
        <p>
          Всё, что Мия помнит о вас. Новое она сначала предлагает, а запоминает только после вашего
          согласия. Любую запись можно изменить или удалить насовсем.
        </p>
      </header>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      <div className="mw-tabs" role="tablist" aria-label="Разделы памяти">
        {tabs.map((item) => (
          <button
            key={item.id}
            role="tab"
            id={`memory-tab-${item.id}`}
            aria-selected={tab === item.id}
            aria-controls="memory-panel"
            className={`mw-tab ${tab === item.id ? 'is-current' : ''}`}
            onClick={() => setTab(item.id)}
          >
            {item.label}
            {item.count !== undefined && item.count > 0 && (
              <b className="mw-tab__count" aria-label={`: ${item.count}`}>
                {item.count}
              </b>
            )}
          </button>
        ))}
      </div>
      <div
        id="memory-panel"
        role="tabpanel"
        aria-labelledby={`memory-tab-${tab}`}
        className="mw-stack"
      >
        {!data && !error && (
          <p className="mw-muted" role="status">
            <LoaderCircle size={16} className="spin" /> Загружаем…
          </p>
        )}
        {data && tab === 'suggested' && (
          <Suggested data={data} onChanged={changed} setDialog={setDialog} close={close} />
        )}
        {data && tab === 'facts' && (
          <Facts facts={data.facts} onChanged={changed} setDialog={setDialog} close={close} />
        )}
        {data && tab === 'people' && (
          <People people={data.people} onChanged={changed} setDialog={setDialog} close={close} />
        )}
        {data && tab === 'prefs' && (
          <Prefs prefs={data.prefs} onChanged={changed} setDialog={setDialog} close={close} />
        )}
        {data && tab === 'privacy' && (
          <PrivacyTab
            data={data}
            onChanged={changed}
            setDialog={setDialog}
            close={close}
            toSuggested={() => setTab('suggested')}
          />
        )}
      </div>
      {dialog}
    </section>
  )
}

type Ctl = {
  onChanged: (message: string) => Promise<void>
  setDialog: (node: ReactNode) => void
  close: () => void
}

async function run(action: () => Promise<unknown>, fallback: string): Promise<boolean> {
  try {
    await action()
    return true
  } catch (e) {
    toast.error(messageOf(e, fallback))
    return false
  }
}

function Empty({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div className="mw-empty mia-glass">
      {icon}
      <p>{children}</p>
    </div>
  )
}

function Meta({ source, last }: { source: Fact['source']; last: string | null }) {
  return (
    <p className="mw-muted">
      {sourceLabel[source.type] ?? source.type}
      {last ? ` · использовано ${when(last)}` : ' · ещё не использовалось'}
    </p>
  )
}

function Suggested({ data, onChanged, setDialog, close }: { data: Data } & Ctl) {
  const total = data.suggestedFacts.length + data.suggestedPeople.length
  if (total === 0)
    return (
      <Empty icon={<Check size={28} aria-hidden="true" />}>
        Новых предложений нет. Когда Мия захочет что-то запомнить, она спросит здесь.
      </Empty>
    )
  return (
    <>
      <p className="mw-muted">
        Мия ещё ничего из этого не запомнила. Подтвердите то, что верно, остальное отклоните.
      </p>
      <ul className="mw-stack">
        {data.suggestedFacts.map((fact) => (
          <li key={fact.id} className="mia-glass mw-record" data-record="fact-suggested">
            <div className="mw-record__body">
              <p className="mw-record__text">{fact.text}</p>
              <p className="mw-muted">
                {categoryLabel[fact.category]} · {sourceLabel[fact.source.type] ?? fact.source.type}
              </p>
            </div>
            <div className="mw-record__actions">
              <button
                className="mia-btn mia-btn--primary"
                aria-label={`Запомнить: ${fact.text}`}
                onClick={async () => {
                  if (await run(() => api.patchFact(fact.id, { status: 'active' }), 'Не удалось'))
                    await onChanged('Запомнила')
                }}
              >
                <Check size={16} aria-hidden="true" /> Запомнить
              </button>
              <button
                className="mia-btn mia-btn--ghost"
                aria-label={`Изменить предложение: ${fact.text}`}
                onClick={() =>
                  setDialog(<FactDialog fact={fact} onSaved={onChanged} close={close} confirm />)
                }
              >
                <Pencil size={16} aria-hidden="true" /> Изменить
              </button>
              <button
                className="mia-btn mia-btn--ghost"
                aria-label={`Отклонить: ${fact.text}`}
                onClick={async () => {
                  if (await run(() => api.patchFact(fact.id, { status: 'rejected' }), 'Не удалось'))
                    await onChanged('Предложение отклонено')
                }}
              >
                <X size={16} aria-hidden="true" /> Отклонить
              </button>
            </div>
          </li>
        ))}
        {data.suggestedPeople.map((person) => (
          <li key={person.id} className="mia-glass mw-record" data-record="person-suggested">
            <div className="mw-record__body">
              <p className="mw-record__text">
                <UserRound size={16} aria-hidden="true" /> {person.display_name}
                {person.relation ? ` — ${person.relation}` : ''}
              </p>
              <p className="mw-muted">
                Человек · {sourceLabel[person.source.type] ?? person.source.type}
              </p>
            </div>
            <div className="mw-record__actions">
              <button
                className="mia-btn mia-btn--primary"
                aria-label={`Запомнить человека: ${person.display_name}`}
                onClick={async () => {
                  if (
                    await run(() => api.patchPerson(person.id, { status: 'active' }), 'Не удалось')
                  )
                    await onChanged('Запомнила')
                }}
              >
                <Check size={16} aria-hidden="true" /> Запомнить
              </button>
              <button
                className="mia-btn mia-btn--ghost"
                aria-label={`Отклонить человека: ${person.display_name}`}
                onClick={async () => {
                  if (
                    await run(
                      () => api.patchPerson(person.id, { status: 'rejected' }),
                      'Не удалось',
                    )
                  )
                    await onChanged('Предложение отклонено')
                }}
              >
                <X size={16} aria-hidden="true" /> Отклонить
              </button>
            </div>
          </li>
        ))}
      </ul>
    </>
  )
}

function Facts({ facts, onChanged, setDialog, close }: { facts: Fact[] } & Ctl) {
  return (
    <>
      <div className="mw-actions">
        <button
          className="mia-btn mia-btn--secondary"
          onClick={() => setDialog(<FactDialog onSaved={onChanged} close={close} />)}
        >
          <Plus size={16} aria-hidden="true" /> Добавить факт
        </button>
      </div>
      {facts.length === 0 ? (
        <Empty icon={<Brain size={28} aria-hidden="true" />}>
          Пока ничего не запомнено. Скажите Мие «Запомни, что…» или добавьте факт сами.
        </Empty>
      ) : (
        <ul className="mw-stack">
          {facts.map((fact) => (
            <li key={fact.id} className="mia-glass mw-record" data-record="fact">
              <div className="mw-record__body">
                <p className="mw-record__text">{fact.text}</p>
                <div className="mw-chips">
                  <span className="mia-chip">{categoryLabel[fact.category]}</span>
                  {fact.sensitivity === 'sensitive' && (
                    <span className="mia-chip mia-chip--warning">Чувствительное</span>
                  )}
                </div>
                <Meta source={fact.source} last={fact.last_used_at} />
              </div>
              <div className="mw-record__actions">
                <button
                  className="mia-btn mia-btn--ghost"
                  aria-label={`Изменить: ${fact.text}`}
                  onClick={() =>
                    setDialog(<FactDialog fact={fact} onSaved={onChanged} close={close} />)
                  }
                >
                  <Pencil size={16} aria-hidden="true" /> Изменить
                </button>
                <button
                  className="mia-btn mia-btn--ghost"
                  aria-label={`Удалить: ${fact.text}`}
                  onClick={() =>
                    setDialog(
                      <ConfirmDelete
                        title="Удалить факт?"
                        text={fact.text}
                        close={close}
                        onConfirm={async () => {
                          if (await run(() => api.deleteFact(fact.id), 'Не удалось удалить')) {
                            close()
                            await onChanged('Факт удалён')
                          }
                        }}
                      />,
                    )
                  }
                >
                  <Trash2 size={16} aria-hidden="true" /> Удалить
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function ConfirmDelete({
  title,
  text,
  close,
  onConfirm,
  action = 'Удалить насовсем',
}: {
  title: string
  text: string
  close: () => void
  onConfirm: () => Promise<void>
  action?: string
}) {
  const [busy, setBusy] = useState(false)
  return (
    <Dialog
      title={title}
      description="Запись будет стёрта насовсем, вернуть её нельзя."
      onClose={close}
    >
      <div className="mw-stack">
        <p className="mw-quote">{text}</p>
        <div className="mw-dialog__actions">
          <button className="mia-btn mia-btn--secondary" onClick={close} disabled={busy}>
            Отмена
          </button>
          <button
            className="mia-btn mia-btn--danger"
            disabled={busy}
            onClick={async () => {
              setBusy(true)
              await onConfirm()
              setBusy(false)
            }}
          >
            {busy && <LoaderCircle size={16} className="spin" />} {action}
          </button>
        </div>
      </div>
    </Dialog>
  )
}

function FactDialog({
  fact,
  onSaved,
  close,
  confirm = false,
}: {
  fact?: Fact
  onSaved: (message: string) => Promise<void>
  close: () => void
  confirm?: boolean
}) {
  const [text, setText] = useState(fact?.text ?? '')
  const [category, setCategory] = useState<MemoryCategory>(fact?.category ?? 'profile')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function save() {
    setBusy(true)
    setError('')
    try {
      if (!fact) await api.createFact(text.trim(), category)
      else
        await api.patchFact(fact.id, {
          text: text.trim(),
          category,
          ...(confirm ? { status: 'active' as const } : {}),
        })
      close()
      await onSaved(!fact ? 'Факт добавлен' : confirm ? 'Запомнила' : 'Факт изменён')
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }
  return (
    <Dialog
      title={!fact ? 'Новый факт' : confirm ? 'Исправить и запомнить' : 'Изменить факт'}
      description="Мия будет использовать этот факт в разговорах, если он нужен."
      onClose={close}
    >
      <form
        className="mw-form"
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <label>
          <span>Текст</span>
          <textarea
            value={text}
            maxLength={2000}
            rows={3}
            required
            onChange={(event) => setText(event.target.value)}
          />
        </label>
        <label>
          <span>Раздел</span>
          <select
            value={category}
            onChange={(event) => setCategory(event.target.value as MemoryCategory)}
          >
            {categories.map((value) => (
              <option key={value} value={value}>
                {categoryLabel[value]}
              </option>
            ))}
          </select>
        </label>
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
        <div className="mw-dialog__actions">
          <button type="button" className="mia-btn mia-btn--secondary" onClick={close}>
            Отмена
          </button>
          <button className="mia-btn mia-btn--primary" disabled={busy || !text.trim()}>
            {busy && <LoaderCircle size={16} className="spin" />} Сохранить
          </button>
        </div>
      </form>
    </Dialog>
  )
}

function People({ people, onChanged, setDialog, close }: { people: Person[] } & Ctl) {
  return (
    <>
      <div className="mw-actions">
        <button
          className="mia-btn mia-btn--secondary"
          onClick={() => setDialog(<PersonDialog onSaved={onChanged} close={close} />)}
        >
          <Plus size={16} aria-hidden="true" /> Добавить человека
        </button>
      </div>
      {people.length === 0 ? (
        <Empty icon={<UserRound size={28} aria-hidden="true" />}>
          Здесь будут близкие и коллеги. Скажите «Яна — моя жена», и Мия предложит запомнить.
        </Empty>
      ) : (
        <ul className="mw-stack">
          {people.map((person) => {
            const known = [
              ...new Set(Object.keys(person.identifiers).map((k) => identifierLabel[k] ?? k)),
            ]
            return (
              <li key={person.id} className="mia-glass mw-record" data-record="person">
                <div className="mw-record__body">
                  <p className="mw-record__text">
                    <UserRound size={16} aria-hidden="true" /> {person.display_name}
                    {person.relation ? ` — ${person.relation}` : ''}
                  </p>
                  <div className="mw-chips">
                    {person.aliases.map((alias) => (
                      <span key={alias} className="mia-chip">
                        {alias}
                      </span>
                    ))}
                    {known.length > 0 && (
                      <span className="mia-chip">Известно: {known.join(', ')}</span>
                    )}
                    {person.trusted && (
                      <span className="mia-chip mia-chip--accent">Доверенный</span>
                    )}
                  </div>
                  <Meta source={person.source} last={person.last_used_at} />
                </div>
                <div className="mw-record__actions">
                  <button
                    className="mia-btn mia-btn--ghost"
                    aria-label={`Изменить: ${person.display_name}`}
                    onClick={() =>
                      setDialog(<PersonDialog person={person} onSaved={onChanged} close={close} />)
                    }
                  >
                    <Pencil size={16} aria-hidden="true" /> Изменить
                  </button>
                  <button
                    className="mia-btn mia-btn--ghost"
                    aria-label={`Удалить: ${person.display_name}`}
                    onClick={() =>
                      setDialog(
                        <ConfirmDelete
                          title="Удалить человека?"
                          text={person.display_name}
                          close={close}
                          onConfirm={async () => {
                            if (
                              await run(() => api.deletePerson(person.id), 'Не удалось удалить')
                            ) {
                              close()
                              await onChanged('Человек удалён')
                            }
                          }}
                        />,
                      )
                    }
                  >
                    <Trash2 size={16} aria-hidden="true" /> Удалить
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}

function PersonDialog({
  person,
  onSaved,
  close,
}: {
  person?: Person
  onSaved: (message: string) => Promise<void>
  close: () => void
}) {
  const [name, setName] = useState(person?.display_name ?? '')
  const [relation, setRelation] = useState(person?.relation ?? '')
  const [aliases, setAliases] = useState(person?.aliases.join(', ') ?? '')
  const [trusted, setTrusted] = useState(person?.trusted ?? false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function save() {
    setBusy(true)
    setError('')
    const list = aliases
      .split(',')
      .map((a) => a.trim())
      .filter(Boolean)
    try {
      if (!person)
        await api.createPerson({
          display_name: name.trim(),
          ...(relation.trim() ? { relation: relation.trim() } : {}),
          aliases: list,
        })
      else
        await api.patchPerson(person.id, {
          display_name: name.trim(),
          relation: relation.trim(),
          aliases: list,
          trusted,
        })
      close()
      await onSaved(person ? 'Изменения сохранены' : 'Человек добавлен')
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }
  return (
    <Dialog
      title={person ? 'Изменить человека' : 'Новый человек'}
      description="Модель видит только имя и кем человек вам приходится, но не его контакты."
      onClose={close}
    >
      <form
        className="mw-form"
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <label>
          <span>Имя</span>
          <input value={name} maxLength={120} required onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          <span>Кто это (жена, коллега…)</span>
          <input value={relation} maxLength={60} onChange={(e) => setRelation(e.target.value)} />
        </label>
        <label>
          <span>Другие имена через запятую</span>
          <input value={aliases} onChange={(e) => setAliases(e.target.value)} />
        </label>
        {person && (
          <label className="mw-switch">
            <input
              type="checkbox"
              checked={trusted}
              onChange={(e) => setTrusted(e.target.checked)}
            />
            <span>Доверенный контакт: голосовое «да» достаточно для сообщения ему</span>
          </label>
        )}
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
        <div className="mw-dialog__actions">
          <button type="button" className="mia-btn mia-btn--secondary" onClick={close}>
            Отмена
          </button>
          <button className="mia-btn mia-btn--primary" disabled={busy || !name.trim()}>
            {busy && <LoaderCircle size={16} className="spin" />} Сохранить
          </button>
        </div>
      </form>
    </Dialog>
  )
}

function Prefs({ prefs, onChanged, setDialog, close }: { prefs: Preference[] } & Ctl) {
  if (prefs.length === 0)
    return (
      <Empty icon={<Brain size={28} aria-hidden="true" />}>
        Предпочтения (язык ответов, скорость речи) появятся, когда вы их задатите в разговоре.
      </Empty>
    )
  return (
    <ul className="mw-stack">
      {prefs.map((pref) => (
        <li key={pref.id} className="mia-glass mw-record" data-record="preference">
          <div className="mw-record__body">
            <p className="mw-record__text">
              <code>{pref.key}</code> = {String(pref.value)}
            </p>
            <p className="mw-muted">{sourceLabel[pref.source.type] ?? pref.source.type}</p>
          </div>
          <div className="mw-record__actions">
            <button
              className="mia-btn mia-btn--ghost"
              aria-label={`Изменить предпочтение ${pref.key}`}
              onClick={() =>
                setDialog(<PrefDialog pref={pref} onSaved={onChanged} close={close} />)
              }
            >
              <Pencil size={16} aria-hidden="true" /> Изменить
            </button>
          </div>
        </li>
      ))}
    </ul>
  )
}

function PrefDialog({
  pref,
  onSaved,
  close,
}: {
  pref: Preference
  onSaved: (message: string) => Promise<void>
  close: () => void
}) {
  const [value, setValue] = useState(String(pref.value))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function save() {
    setBusy(true)
    const typed =
      typeof pref.value === 'number' && value.trim() !== '' && !Number.isNaN(Number(value))
        ? Number(value)
        : typeof pref.value === 'boolean'
          ? value === 'true'
          : value
    try {
      await api.putPreference(pref.key, typed)
      close()
      await onSaved('Предпочтение сохранено')
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }
  return (
    <Dialog
      title={`Изменить ${pref.key}`}
      description="Новое значение действует сразу."
      onClose={close}
    >
      <form
        className="mw-form"
        onSubmit={(event) => {
          event.preventDefault()
          void save()
        }}
      >
        <label>
          <span>Значение</span>
          <input value={value} maxLength={200} onChange={(e) => setValue(e.target.value)} />
        </label>
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
        <div className="mw-dialog__actions">
          <button type="button" className="mia-btn mia-btn--secondary" onClick={close}>
            Отмена
          </button>
          <button className="mia-btn mia-btn--primary" disabled={busy || value === ''}>
            Сохранить
          </button>
        </div>
      </form>
    </Dialog>
  )
}

function PrivacyTab({
  data,
  onChanged,
  setDialog,
  close,
  toSuggested,
}: { data: Data; toSuggested: () => void } & Ctl) {
  const [settings, setSettings] = useState(data.settings)
  const [busy, setBusy] = useState('')
  useEffect(() => setSettings(data.settings), [data.settings])

  async function save(patch: Partial<MemorySettings>, message: string) {
    setSettings((current) => ({ ...current, ...patch }))
    setBusy('settings')
    if (await run(() => api.putMemorySettings(patch), 'Не удалось сохранить настройки'))
      await onChanged(message)
    else setSettings(data.settings)
    setBusy('')
  }
  async function download(format: 'zip' | 'json') {
    setBusy(format)
    try {
      await downloadFile(
        apiUrl(`/v1/memory/export?format=${format}`),
        format === 'zip' ? 'mia-memory.zip' : 'mia-memory.json',
      )
      toast.success('Файл сохранён')
    } catch (e) {
      toast.error(messageOf(e, 'Не удалось выгрузить память.'))
    } finally {
      setBusy('')
    }
  }

  return (
    <>
      <section className="mia-glass mw-block" aria-labelledby="mem-autosave">
        <h2 id="mem-autosave">Что запоминать сразу</h2>
        <p className="mw-muted">
          Эти разделы Мия сохраняет без вопроса. Здоровье и финансы всегда ждут вашего согласия.
        </p>
        <div className="mw-chips">
          {categories.map((value) => {
            const sensitive = sensitiveCategories.includes(value)
            const on = settings.autosave_categories.includes(value)
            return (
              <label key={value} className="mw-switch">
                <input
                  type="checkbox"
                  checked={on}
                  disabled={sensitive || busy === 'settings'}
                  onChange={(event) =>
                    void save(
                      {
                        autosave_categories: event.target.checked
                          ? [...settings.autosave_categories, value]
                          : settings.autosave_categories.filter((c) => c !== value),
                      },
                      'Настройки сохранены',
                    )
                  }
                />
                <span>{categoryLabel[value]}</span>
              </label>
            )
          })}
        </div>
        <label className="mw-field-row">
          <span>Как долго хранить историю разговоров</span>
          <select
            value={settings.history_retention}
            disabled={busy === 'settings'}
            onChange={(event) =>
              void save(
                { history_retention: event.target.value as Retention },
                'Срок хранения сохранён',
              )
            }
          >
            {(Object.keys(retentionLabel) as Retention[]).map((value) => (
              <option key={value} value={value}>
                {retentionLabel[value]}
              </option>
            ))}
          </select>
        </label>
      </section>

      <section className="mia-glass mw-block" aria-labelledby="mem-transfer">
        <h2 id="mem-transfer">Выгрузка и перенос</h2>
        <p className="mw-muted">
          Заберите свою память файлом или перенесите её из другого ассистента: импортированное
          сначала попадёт в «Предложено».
        </p>
        <div className="mw-actions">
          <button
            className="mia-btn mia-btn--secondary"
            disabled={busy !== ''}
            onClick={() => void download('zip')}
          >
            {busy === 'zip' ? (
              <LoaderCircle size={16} className="spin" />
            ) : (
              <Download size={16} aria-hidden="true" />
            )}{' '}
            Скачать архив (ZIP)
          </button>
          <button
            className="mia-btn mia-btn--secondary"
            disabled={busy !== ''}
            onClick={() => void download('json')}
          >
            <Download size={16} aria-hidden="true" /> Скачать JSON
          </button>
          <button
            className="mia-btn mia-btn--secondary"
            onClick={() =>
              setDialog(
                <ImportDialog
                  close={close}
                  onImported={async (message) => {
                    await onChanged(message)
                    toSuggested()
                  }}
                />,
              )
            }
          >
            <Upload size={16} aria-hidden="true" /> Импортировать
          </button>
        </div>
      </section>

      <section className="mia-glass mw-block" aria-labelledby="mem-log">
        <h2 id="mem-log">Кто обращался к памяти</h2>
        <p className="mw-muted">
          Журнал показывает, кто и когда читал или менял записи. Содержимое в него не попадает.
        </p>
        {data.access.length === 0 ? (
          <p className="mw-muted">Пока обращений не было.</p>
        ) : (
          <ul className="mw-log">
            {data.access.map((entry, index) => (
              <li key={`${entry.at}-${index}`}>
                <span>{when(entry.at)}</span>
                <strong>{actorLabel[entry.actor]}</strong>
                <span>
                  {entry.action} · записей: {entry.record_count}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mia-glass mw-block mw-danger" aria-labelledby="mem-forget">
        <h2 id="mem-forget">Забыть всё</h2>
        <p className="mw-muted">
          Стирает все факты, людей и предпочтения. Настройки и журнал остаются. Отменить нельзя.
        </p>
        <div className="mw-actions">
          <button
            className="mia-btn mia-btn--danger"
            onClick={() =>
              setDialog(
                <ConfirmDelete
                  title="Удалить всю память?"
                  text="Все факты, люди и предпочтения будут стёрты."
                  action="Удалить всё"
                  close={close}
                  onConfirm={async () => {
                    if (await run(() => api.forgetMemory(), 'Не удалось удалить память')) {
                      close()
                      await onChanged('Память очищена')
                    }
                  }}
                />,
              )
            }
          >
            <Trash2 size={16} aria-hidden="true" /> Удалить всю память
          </button>
          <Link to="/data" className="mw-link">
            Где хранятся мои данные
          </Link>
        </div>
      </section>
    </>
  )
}

function ImportDialog({
  close,
  onImported,
}: {
  close: () => void
  onImported: (message: string) => Promise<void>
}) {
  const [format, setFormat] = useState<'markdown' | 'text' | 'json'>('text')
  const [source, setSource] = useState('chatgpt')
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function submit() {
    setBusy(true)
    setError('')
    try {
      const result = await api.importMemory(format, source, content)
      close()
      await onImported(
        `В очередь проверки добавлено: фактов ${result.facts}, людей ${result.people}`,
      )
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }
  return (
    <Dialog
      title="Импорт памяти"
      description="Вставьте текст из другого ассистента или выгрузку Мии. Ничего не запомнится, пока вы не подтвердите."
      onClose={close}
      wide
    >
      <form
        className="mw-form"
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        <label>
          <span>Откуда</span>
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="chatgpt">ChatGPT</option>
            <option value="claude">Claude</option>
            <option value="gemini">Gemini</option>
            <option value="mia">Выгрузка Мии</option>
            <option value="other">Другое</option>
          </select>
        </label>
        <label>
          <span>Формат</span>
          <select value={format} onChange={(e) => setFormat(e.target.value as typeof format)}>
            <option value="text">Обычный текст</option>
            <option value="markdown">Markdown</option>
            <option value="json">JSON Мии</option>
          </select>
        </label>
        <label>
          <span>Файл (по желанию)</span>
          <input
            type="file"
            accept=".txt,.md,.json,text/plain,text/markdown,application/json"
            onChange={async (event) => {
              const file = event.target.files?.[0]
              if (file) setContent(await file.text())
            }}
          />
        </label>
        <label>
          <span>Содержимое</span>
          <textarea
            value={content}
            rows={8}
            maxLength={256000}
            required
            onChange={(e) => setContent(e.target.value)}
          />
        </label>
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
        <div className="mw-dialog__actions">
          <button type="button" className="mia-btn mia-btn--secondary" onClick={close}>
            Отмена
          </button>
          <button className="mia-btn mia-btn--primary" disabled={busy || !content.trim()}>
            {busy && <LoaderCircle size={16} className="spin" />} Отправить на проверку
          </button>
        </div>
      </form>
    </Dialog>
  )
}
