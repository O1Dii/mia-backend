import { useEffect } from 'react'
import { Blocks, Cpu, Layers3, LogOut, MessageCircle, ShieldCheck } from 'lucide-react'
import { useAuth } from './lib/auth'
import { ChatProvider } from './lib/chat'
import { Link, navigate, usePlace } from './lib/router'
import { Approvals, usePendingApprovals } from './components/Approvals'
import { Chat } from './components/Chat'
import { Models } from './components/Models'
import { PluginPage } from './components/PluginPage'
import { Store } from './components/Store'
import { StoreAddUrl } from './components/StoreAddUrl'
import { StoreConnected } from './components/StoreConnected'
import { StoreInstalled } from './components/StoreInstalled'

const tabs = [
  { to: '/chat', label: 'Чат', icon: MessageCircle, match: /^\/chat/ },
  { to: '/store', label: 'Магазин', icon: Blocks, match: /^\/store/ },
  { to: '/installed', label: 'Мои плагины', icon: Layers3, match: /^\/installed/ },
  { to: '/models', label: 'Модели', icon: Cpu, match: /^\/models/ },
  {
    to: '/approvals',
    label: 'Подтверждения',
    short: 'Одобрения',
    icon: ShieldCheck,
    match: /^\/approvals/,
  },
]

export function App() {
  return (
    <ChatProvider>
      <Shell />
    </ChatProvider>
  )
}

function Shell() {
  const place = usePlace()
  const auth = useAuth()
  const pending = usePendingApprovals()
  const count = pending.items?.length ?? 0

  useEffect(() => {
    if (place.path === '/') navigate('/chat', true)
  }, [place.path])

  const path = place.path
  let screen
  if (path.startsWith('/chat')) screen = <Chat />
  else if (path === '/store/connected') screen = <StoreConnected search={place.search} />
  else if (path === '/store/add-url') screen = <StoreAddUrl search={place.search} />
  else if (path.startsWith('/store/plugin/'))
    screen = (
      <PluginPage key={path} pluginId={decodeURIComponent(path.slice('/store/plugin/'.length))} />
    )
  else if (path.startsWith('/store')) screen = <Store />
  else if (path.startsWith('/installed')) screen = <StoreInstalled />
  else if (path.startsWith('/models')) screen = <Models />
  else if (path.startsWith('/approvals')) screen = <Approvals pending={pending} />
  else if (path === '/') screen = null
  else
    screen = (
      <section className="mw-page">
        <h1>Страница не найдена</h1>
        <Link to="/chat" className="mia-btn mia-btn--primary">
          В чат
        </Link>
      </section>
    )

  return (
    <div className="mw-app mia-root">
      <header className="mw-header">
        <Link to="/chat" className="mw-brand" aria-label="Мия — чат">
          <span className="mia-orb mia-orb--sm" data-state="idle" aria-hidden="true" />
          <span>Мия</span>
        </Link>
        <nav className="mw-nav" aria-label="Главная навигация">
          {tabs.map(({ to, label, short, icon: Icon, match }) => {
            const current = match.test(path)
            return (
              <Link
                key={to}
                to={to}
                className={`mw-nav__item ${current ? 'is-current' : ''}`}
                aria-current={current ? 'page' : undefined}
              >
                <Icon size={20} aria-hidden="true" />
                <span className={`mw-nav__long ${short ? 'has-short' : ''}`}>{label}</span>
                {short && (
                  <span className="mw-nav__short" aria-hidden="true">
                    {short}
                  </span>
                )}
                {to === '/approvals' && count > 0 && (
                  <b className="mw-badge" aria-label={`ожидают: ${count}`}>
                    {count}
                  </b>
                )}
              </Link>
            )
          })}
        </nav>
        <div className="mw-user">
          <span className="mw-user__name" title={auth.user.email}>
            {auth.user.display_name ?? auth.user.email}
          </span>
          <button className="mia-icon-btn" aria-label="Выйти" onClick={() => void auth.logout()}>
            <LogOut size={18} />
          </button>
        </div>
      </header>
      <main className="mw-main">{screen}</main>
    </div>
  )
}
