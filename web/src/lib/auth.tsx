import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { LoaderCircle, ShieldCheck, Sparkles } from 'lucide-react'
import { api, type User } from './api'
import { ApiError, messageOf } from './http'
import { AndroidAppLink } from '../components/AndroidApp'

type AuthContext = { user: User; logout: () => Promise<void> }
const Context = createContext<AuthContext | null>(null)
export function useAuth() {
  const value = useContext(Context)
  if (!value) throw new Error('AuthProvider required')
  return value
}

// The web client id is public by design (it identifies the app to Google); it is set at build time.
const GOOGLE_CLIENT_ID: string = import.meta.env.VITE_GOOGLE_CLIENT_ID ?? ''

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<
    | { status: 'loading' }
    | { status: 'anonymous' }
    | { status: 'in'; user: User }
    | { status: 'down'; message: string }
  >({ status: 'loading' })

  const reload = useCallback(async () => {
    try {
      setState({ status: 'in', user: await api.me() })
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) setState({ status: 'anonymous' })
      else setState({ status: 'down', message: messageOf(error, 'Сервер недоступен.') })
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])
  useEffect(() => {
    const expired = () => setState((old) => (old.status === 'in' ? { status: 'anonymous' } : old))
    window.addEventListener('mia:unauthorized', expired)
    return () => window.removeEventListener('mia:unauthorized', expired)
  }, [])

  if (state.status === 'loading')
    return (
      <div className="mw-center" role="status">
        <LoaderCircle className="spin" />
        <p>Открываем ваше пространство…</p>
      </div>
    )
  if (state.status === 'down')
    return (
      <div className="mw-center" role="alert">
        <p>{state.message}</p>
        <button className="mia-btn mia-btn--secondary" onClick={() => void reload()}>
          Повторить
        </button>
      </div>
    )
  if (state.status === 'anonymous') return <Login onSignedIn={reload} />
  return (
    <Context.Provider
      key={state.user.id}
      value={{
        user: state.user,
        logout: async () => {
          await api.logout().catch(() => undefined)
          location.assign(import.meta.env.BASE_URL)
        },
      }}
    >
      {children}
    </Context.Provider>
  )
}

type GoogleAPI = {
  accounts: {
    id: {
      initialize: (options: {
        client_id: string
        nonce: string
        callback: (response: { credential: string }) => void
      }) => void
      renderButton: (element: HTMLElement, options: Record<string, unknown>) => void
    }
  }
}

function Login({ onSignedIn }: { onSignedIn: () => Promise<void> }) {
  // After «Удалить аккаунт» (M17-R1) the page reloads here with ?account=deleted.
  const [deleted] = useState(
    () => new URLSearchParams(location.search).get('account') === 'deleted',
  )
  const [error, setError] = useState(
    GOOGLE_CLIENT_ID ? '' : 'Вход не настроен: не задан VITE_GOOGLE_CLIENT_ID.',
  )
  useEffect(() => {
    if (!GOOGLE_CLIENT_ID) return
    let disposed = false
    const start = async () => {
      try {
        const { nonce } = await api.nonce()
        if (disposed) return
        const google = (window as unknown as { google: GoogleAPI }).google
        google.accounts.id.initialize({
          client_id: GOOGLE_CLIENT_ID,
          nonce,
          callback: async ({ credential }) => {
            try {
              await api.googleLogin(credential)
              await onSignedIn()
            } catch (e) {
              setError(messageOf(e, 'Вход не удался.'))
            }
          },
        })
        const element = document.getElementById('google-signin')
        if (element)
          google.accounts.id.renderButton(element, {
            theme: 'filled_black',
            size: 'large',
            shape: 'pill',
            locale: 'ru',
          })
      } catch (e) {
        setError(messageOf(e, 'Не удалось подготовить вход.'))
      }
    }
    if ((window as unknown as { google?: GoogleAPI }).google) void start()
    else {
      const script = document.createElement('script')
      script.src = 'https://accounts.google.com/gsi/client'
      script.async = true
      script.onload = () => void start()
      script.onerror = () => setError('Не удалось загрузить Google. Проверьте подключение.')
      document.head.append(script)
    }
    return () => {
      disposed = true
    }
  }, [onSignedIn])
  return (
    <main className="mw-login">
      <div className="mia-glass mia-glass--top mw-login__card mia-rise">
        <span className="mia-orb mia-orb--lg" data-state="idle" aria-hidden="true" />
        <span className="mia-card__eyebrow">Мия · ваше личное пространство</span>
        <h1>Ваш интеллект. Ваш ритм жизни.</h1>
        <p>Один аккаунт для чата, моделей, плагинов и подтверждений действий.</p>
        <div className="mw-login__features">
          <span className="mia-chip">
            <Sparkles size={14} /> Любимая модель
          </span>
          <span className="mia-chip">
            <ShieldCheck size={14} /> Вы подтверждаете действия
          </span>
        </div>
        {deleted && (
          <p className="mw-notice" role="status">
            Аккаунт удалён. Если войти снова, будет создан новый пустой аккаунт.
          </p>
        )}
        <div id="google-signin" />
        <AndroidAppLink />
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </main>
  )
}
