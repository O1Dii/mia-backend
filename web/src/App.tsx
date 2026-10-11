import { useEffect } from 'react'
import {
  Brain,
  Ellipsis,
  Inbox as InboxIcon,
  LayoutGrid,
  LogOut,
  MessageCircle,
  ShieldCheck,
  Blocks,
} from 'lucide-react'
import { useAuth } from './lib/auth'
import { ChatProvider, chatPath, useChat } from './lib/chat'
import { useDeviceHeartbeat } from './lib/heartbeat'
import { Link, navigate, usePlace } from './lib/router'
import { Approvals, usePendingApprovals } from './components/Approvals'
import { Chat } from './components/Chat'
import { Dashboard } from './components/Dashboard'
import { Devices } from './components/Devices'
import { Inbox, useUnreadInbox } from './components/Inbox'
import { Memory } from './components/Memory'
import { Models } from './components/Models'
import { More } from './components/More'
import { Notifications } from './components/Notifications'
import { PluginPage } from './components/PluginPage'
import { Privacy } from './components/Privacy'
import { Store } from './components/Store'
import { StoreAddUrl } from './components/StoreAddUrl'
import { StoreConnected } from './components/StoreConnected'
import { StoreInstalled } from './components/StoreInstalled'

type Tab = {
  to: string
  label: string
  short?: string
  icon: typeof MessageCircle
  match: RegExp
}

// Seven tabs fit a phone's bottom bar; plugins, models and the data page live under «Ещё».
const tabs: Tab[] = [
  { to: '/dashboard', label: 'Дашборд', icon: LayoutGrid, match: /^\/dashboard/ },
  { to: '/chat', label: 'Чат', icon: MessageCircle, match: /^\/chat/ },
  { to: '/inbox', label: 'Входящие', icon: InboxIcon, match: /^\/inbox/ },
  { to: '/memory', label: 'Память', icon: Brain, match: /^\/memory/ },
  { to: '/store', label: 'Магазин', icon: Blocks, match: /^\/store/ },
  {
    to: '/approvals',
    label: 'Подтверждения',
    short: 'Решения',
    icon: ShieldCheck,
    match: /^\/approvals/,
  },
  {
    to: '/more',
    label: 'Ещё',
    icon: Ellipsis,
    match: /^\/(more|installed|models|data|notifications|devices)/,
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
  const unread = useUnreadInbox()
  const chat = useChat()
  useDeviceHeartbeat()
  // The tab returns to the chat that was open, not to the latest one.
  const chatHref = chatPath(chat.activeId, chat.draft)

  useEffect(() => {
    if (place.path === '/') navigate('/chat', true)
  }, [place.path])

  const path = place.path
  let screen
  if (path.startsWith('/chat')) screen = <Chat />
  else if (path.startsWith('/dashboard')) screen = <Dashboard />
  else if (path.startsWith('/inbox')) screen = <Inbox />
  else if (path.startsWith('/memory')) screen = <Memory />
  else if (path.startsWith('/data')) screen = <Privacy />
  else if (path.startsWith('/notifications')) screen = <Notifications />
  else if (path === '/devices' || path === '/devices/') screen = <Devices />
  else if (path.startsWith('/devices/'))
    screen = <Devices deviceId={decodeURIComponent(path.slice('/devices/'.length))} />
  else if (path.startsWith('/more')) screen = <More />
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
        <Link to={chatHref} className="mw-brand" aria-label="Мия — чат">
          <span className="mia-orb mia-orb--sm" data-state="idle" aria-hidden="true" />
          <span>Мия</span>
        </Link>
        <nav className="mw-nav" aria-label="Главная навигация">
          {tabs.map(({ to: base, label, short, icon: Icon, match }) => {
            const to = base === '/chat' ? chatHref : base
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
                {to === '/inbox' && unread > 0 && (
                  <b className="mw-badge" aria-label={`непрочитано: ${unread}`}>
                    {unread}
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
