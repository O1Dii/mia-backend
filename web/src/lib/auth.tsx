import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { LoaderCircle, LogIn, ShieldCheck, Sparkles } from 'lucide-react'
import { api, type AuthSession } from './api'

type AuthContext = AuthSession & { reload: () => Promise<void>; logout: () => Promise<void> }
const Context = createContext<AuthContext | null>(null)
export function useAuth() {
  const value = useContext(Context)
  if (!value) throw new Error('AuthProvider required')
  return value
}
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(null)
  const [error, setError] = useState('')
  const initial = useRef<Promise<AuthSession> | null>(null)
  const reload = useCallback(async () => {
    try {
      setSession(await api.auth())
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Сервер недоступен')
    }
  }, [])
  useEffect(() => {
    let active = true
    if (!initial.current)
      initial.current = (async () => {
        const current = await api.auth()
        if (new URLSearchParams(location.search).get('signin') === 'google' && current.google_hub) {
          const url = new URL(location.href)
          url.searchParams.delete('signin')
          history.replaceState(null, '', url)
          await api.hubLogin()
          return api.auth()
        }
        return current
      })()
    initial.current
      .then((value) => {
        if (active) setSession(value)
      })
      .catch(async (e) => {
        if (active) {
          setError(e instanceof Error ? e.message : 'Вход не удался')
          const current = await api.auth().catch(() => null)
          if (active) setSession(current)
        }
      })
    return () => {
      active = false
    }
  }, [])
  useEffect(() => {
    const refresh = () => {
      void reload()
    }
    window.addEventListener('mia:unauthorized', refresh)
    window.addEventListener('focus', refresh)
    return () => {
      window.removeEventListener('mia:unauthorized', refresh)
      window.removeEventListener('focus', refresh)
    }
  }, [reload])
  async function logout() {
    await api.logout()
    location.reload()
  }
  if (!session)
    return (
      <div className="auth-loading">
        <LoaderCircle className="spin" />
        <p>{error || 'Открываем ваше пространство…'}</p>
        {error && (
          <button className="button secondary" onClick={reload}>
            Повторить
          </button>
        )}
      </div>
    )
  return (
    <Context.Provider key={session.user?.id ?? 'local'} value={{ ...session, reload, logout }}>
      {session.required && !session.user ? (
        <Login session={session} error={error} onSuccess={reload} />
      ) : (
        children
      )}
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
function Login({
  session,
  error: initialError,
  onSuccess,
}: {
  session: AuthSession
  error: string
  onSuccess: () => Promise<void>
}) {
  const [error, setError] = useState(initialError)
  useEffect(() => {
    if (session.google_hub || !session.google_client_id) return
    let disposed = false
    const initialize = async () => {
      try {
        const { nonce } = await api.loginNonce()
        if (disposed) return
        const google = (window as unknown as { google: GoogleAPI }).google
        google.accounts.id.initialize({
          client_id: session.google_client_id,
          nonce,
          callback: async ({ credential }) => {
            try {
              await api.googleLogin(credential)
              await onSuccess()
            } catch (e) {
              setError(e instanceof Error ? e.message : 'Вход не удался')
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
        setError(e instanceof Error ? e.message : 'Не удалось загрузить Google')
      }
    }
    const existing = document.querySelector<HTMLScriptElement>('script[data-mia-google]')
    if ((window as unknown as { google?: GoogleAPI }).google) void initialize()
    else {
      const script = existing ?? document.createElement('script')
      script.src = 'https://accounts.google.com/gsi/client'
      script.async = true
      script.dataset.miaGoogle = 'true'
      script.onload = () => void initialize()
      script.onerror = () =>
        setError('Не удалось загрузить Google. Проверьте подключение и обновите страницу.')
      if (!existing) document.head.append(script)
    }
    return () => {
      disposed = true
    }
  }, [session.google_hub, session.google_client_id, onSuccess])
  return (
    <main className="login-page">
      <a className="login-hub" href="/">
        ← Все приложения
      </a>
      <div className="login-card">
        <span className="brand-symbol">м</span>
        <span className="eyebrow">МИЯ · ВАШЕ ЛИЧНОЕ ПРОСТРАНСТВО</span>
        <h1>
          Ваш интеллект.
          <br />
          <em>Ваш ритм жизни.</em>
        </h1>
        <p>
          Соберите приложения, которые понимают вас.
          <br />
          Один аккаунт для моделей, плагинов и маленьких ежедневных ритуалов.
        </p>
        <div className="login-features">
          <span>
            <Sparkles size={17} /> Любимая AI-модель
          </span>
          <span>
            <ShieldCheck size={17} /> Личные плагины и дашборд
          </span>
        </div>
        {session.google_hub ? (
          <a className="button google-login" href={session.login_url}>
            <LogIn size={18} /> Продолжить с Google
          </a>
        ) : (
          <div id="google-signin" />
        )}
        {error && (
          <p className="error-message" role="alert">
            {error}
          </p>
        )}
        <small>Вход через Google. API-ключи доступны только вашему пространству.</small>
      </div>
    </main>
  )
}
