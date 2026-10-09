import { useEffect, useMemo, useState } from 'react'
import { ConsentError, type ConsentRequest, consentApi, signInUrl } from './api'

const CATEGORY_LABELS: Record<string, string> = {
  profile: 'О вас',
  work: 'Работа',
  home: 'Дом',
  interests: 'Интересы',
  other: 'Прочее и люди',
}
const CONTEXT_CATEGORIES = ['profile', 'work', 'home', 'interests', 'other']
const REQUIRED = new Set(['openid'])

type State =
  | { kind: 'loading' }
  | { kind: 'framed' }
  | { kind: 'signin' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; request: ConsentRequest }
  | { kind: 'leaving' }

function errorText(error: unknown): string {
  if (!(error instanceof ConsentError)) return 'Что-то пошло не так. Попробуйте ещё раз.'
  switch (error.code) {
    case 'oauth_request_expired':
      return 'Запрос устарел. Вернитесь в приложение и начните вход заново.'
    case 'oauth_request_decided':
      return 'Этот запрос уже обработан. Вернитесь в приложение.'
    case 'oauth_request_not_found':
      return 'Запрос не найден или открыт в другом аккаунте.'
    case 'network':
      return 'Нет связи с Мией. Проверьте подключение.'
    default:
      return 'Не удалось обработать запрос. Попробуйте ещё раз.'
  }
}

function newKey(): string {
  return typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
        b.toString(16).padStart(2, '0'),
      ).join('')
}

export function Consent({ handle }: { handle: string | null }) {
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [categories, setCategories] = useState<Set<string>>(new Set())
  const [limit, setLimit] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const key = useMemo(newKey, [])

  useEffect(() => {
    // Clickjacking guard: consent is never given inside someone else's frame.
    if (window.top !== window.self) {
      setState({ kind: 'framed' })
      return
    }
    if (!handle) {
      setState({ kind: 'error', message: 'В ссылке нет запроса на вход.' })
      return
    }
    consentApi
      .load(handle)
      .then((request) => {
        setChosen(new Set(request.scopes.filter((s) => s.available).map((s) => s.scope)))
        setCategories(new Set(request.memory_categories))
        setLimit(request.monthly_limit_usd)
        setState({ kind: 'ready', request })
      })
      .catch((error: unknown) => {
        if (error instanceof ConsentError && error.status === 401) setState({ kind: 'signin' })
        else setState({ kind: 'error', message: errorText(error) })
      })
  }, [handle])

  async function decide(approve: boolean) {
    if (!handle || state.kind !== 'ready') return
    setBusy(true)
    setFailure(null)
    try {
      const scopes = state.request.scopes.map((s) => s.scope).filter((s) => chosen.has(s))
      const { redirect_to } = await consentApi.decide(
        handle,
        approve
          ? {
              decision: 'approve',
              scopes,
              memory_categories: CONTEXT_CATEGORIES.filter((c) => categories.has(c)),
              monthly_limit_usd: limit || undefined,
            }
          : { decision: 'deny' },
        key,
      )
      setState({ kind: 'leaving' })
      window.location.assign(redirect_to)
    } catch (error) {
      setFailure(errorText(error))
      setBusy(false)
    }
  }

  if (state.kind === 'loading')
    return (
      <Shell>
        <p className="mia-card__body">Загружаем запрос…</p>
      </Shell>
    )
  if (state.kind === 'leaving')
    return (
      <Shell>
        <p className="mia-card__body">Возвращаемся в приложение…</p>
      </Shell>
    )
  if (state.kind === 'framed')
    return (
      <Shell>
        <p className="mia-card__body" role="alert">
          Эту страницу нельзя открывать внутри другого сайта. Откройте вход в отдельной вкладке.
        </p>
      </Shell>
    )
  if (state.kind === 'signin')
    return (
      <Shell>
        <h1 className="mia-card__title">Сначала войдите в Мию</h1>
        <p className="mia-card__body">
          Приложение просит доступ к вашему аккаунту Мии. Войдите и вернитесь к этому окну.
        </p>
        <div className="mia-card__actions">
          <a className="mia-btn mia-btn--primary" href={signInUrl()}>
            Войти в Мию
          </a>
        </div>
      </Shell>
    )
  if (state.kind === 'error')
    return (
      <Shell>
        <p className="mia-card__body" role="alert">
          {state.message}
        </p>
      </Shell>
    )

  const { request } = state
  const wantsContext = request.scopes.some(
    (s) => s.scope === 'memory.context' && chosen.has(s.scope),
  )
  const wantsModel = request.scopes.some((s) => s.scope === 'llm.generate' && chosen.has(s.scope))
  const toggle = (scope: string) =>
    setChosen((current) => {
      const next = new Set(current)
      if (next.has(scope)) next.delete(scope)
      else next.add(scope)
      return next
    })
  const toggleCategory = (category: string) =>
    setCategories((current) => {
      const next = new Set(current)
      if (next.has(category)) next.delete(category)
      else next.add(category)
      return next
    })

  return (
    <Shell>
      <div className="oauth-client">
        {request.client.logo_uri ? (
          <img
            className="oauth-client__logo"
            src={request.client.logo_uri}
            alt=""
            referrerPolicy="no-referrer"
            width={48}
            height={48}
          />
        ) : (
          <span className="oauth-client__logo oauth-client__logo--letter" aria-hidden="true">
            {request.client.name.slice(0, 1).toUpperCase()}
          </span>
        )}
        <div>
          <p className="mia-card__eyebrow">Вход через Мию</p>
          <h1 className="mia-card__title" data-testid="client-name">
            {request.client.name}
          </h1>
          <p className="oauth-client__host">{request.client.redirect_host}</p>
        </div>
      </div>
      <p className="mia-card__body">
        Приложение просит доступ к вашему аккаунту. Отметьте, что разрешить; отозвать доступ можно в
        разделе «Подключённые приложения».
      </p>
      <ul className="oauth-scopes" aria-label="Запрошенные разрешения">
        {request.scopes.map((item) => (
          <li key={item.scope} className="oauth-scope">
            <label>
              <input
                type="checkbox"
                checked={chosen.has(item.scope)}
                disabled={!item.available || REQUIRED.has(item.scope) || busy}
                onChange={() => toggle(item.scope)}
                data-scope={item.scope}
              />
              <span>
                <span className="oauth-scope__title">
                  {item.title}
                  {item.sensitive && (
                    <span className="mia-chip mia-chip--warning">чувствительно</span>
                  )}
                  {!item.available && <span className="mia-chip">пока не используется</span>}
                </span>
                <span className="oauth-scope__text">{item.description}</span>
              </span>
            </label>
          </li>
        ))}
      </ul>
      {wantsContext && (
        <fieldset className="oauth-group">
          <legend className="mia-type-label">Какую память можно подмешивать</legend>
          <div className="oauth-chips">
            {CONTEXT_CATEGORIES.map((category) => (
              <button
                key={category}
                type="button"
                className={`mia-chip ${categories.has(category) ? 'mia-chip--accent' : ''}`}
                aria-pressed={categories.has(category)}
                onClick={() => toggleCategory(category)}
                disabled={busy}
              >
                {CATEGORY_LABELS[category]}
              </button>
            ))}
          </div>
          <p className="oauth-hint">Здоровье и финансы приложениям не подмешиваются.</p>
        </fieldset>
      )}
      {wantsModel && (
        <label className="oauth-group oauth-limit">
          <span className="mia-type-label">Лимит расходов в месяц, $</span>
          <input
            className="oauth-limit__input"
            type="number"
            inputMode="decimal"
            min="0"
            step="0.5"
            max={request.max_monthly_limit_usd}
            value={limit}
            onChange={(event) => setLimit(event.target.value)}
            disabled={busy}
            data-testid="monthly-limit"
          />
        </label>
      )}
      {failure && (
        <p className="oauth-failure" role="alert">
          {failure}
        </p>
      )}
      <div className="mia-card__actions oauth-actions">
        <button
          type="button"
          className="mia-btn mia-btn--secondary"
          onClick={() => decide(false)}
          disabled={busy}
        >
          Отказать
        </button>
        <button
          type="button"
          className="mia-btn mia-btn--primary"
          onClick={() => decide(true)}
          disabled={busy || chosen.size === 0}
        >
          Разрешить
        </button>
      </div>
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="oauth-page">
      <section className="mia-glass mia-glass--top mia-card oauth-card">{children}</section>
    </main>
  )
}
