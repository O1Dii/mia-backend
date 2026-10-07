import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  ArrowDownToLine,
  ArrowRight,
  ArrowUpRight,
  Blocks,
  Bell,
  Check,
  ChevronDown,
  ChevronRight,
  Code2,
  Cpu,
  Download,
  Layers3,
  LayoutDashboard,
  Link2,
  LoaderCircle,
  MessageCircle,
  Plus,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Terminal,
  Trash2,
  Upload,
  Zap,
} from 'lucide-react'
import { toast } from 'sonner'
import { z } from 'zod'
import { webPath, api, type Connection } from './lib/api'
import { useAuth } from './lib/auth'
import { useWorkspace } from './lib/workspace'
import {
  catalog,
  applicationPlugin,
  applicationSchema,
  exampleApplication,
  recipeManifest,
  type ApplicationManifest,
  exampleRecipe,
  permissions,
  recipePlugin,
  recipeSchema,
  type Plugin,
  type Recipe,
} from './plugins/catalog'
import { chime, formatDuration, unlockAudio } from './plugins/scheduler'
import { Tool } from './plugins/Tools'
import { Dialog } from './components/Dialog'
import { PluginArt } from './components/PluginArt'
import { Models } from './components/Models'
import { Chat } from './components/Chat'
import { Dashboard } from './components/Dashboard'
import { PluginPage } from './components/PluginPage'
import { LinkedApps } from './components/LinkedApps'

type Page = 'plugin' | 'dashboard' | 'apps' | 'store' | 'library' | 'models' | 'chat' | 'studio'
const pages: { id: Page; label: string; icon: typeof Blocks }[] = [
  { id: 'dashboard', label: 'Дашборд', icon: LayoutDashboard },
  { id: 'store', label: 'Магазин плагинов', icon: Blocks },
  { id: 'library', label: 'Мои плагины', icon: Layers3 },
  { id: 'models', label: 'Модели и ключи', icon: Cpu },
  { id: 'chat', label: 'Диалог с Мией', icon: MessageCircle },
  { id: 'apps', label: 'Приложения и доступ', icon: Link2 },
  { id: 'studio', label: 'Для авторов', icon: Code2 },
]
function currentPage(): Page {
  const hash = location.hash.slice(1)
  if (hash.startsWith('plugin/')) return 'plugin'
  return pages.some((page) => page.id === hash) ? (hash as Page) : 'store'
}

export function App() {
  const [page, setPage] = useState<Page>(currentPage)
  const [pluginId, setPluginId] = useState(() => location.hash.slice('#plugin/'.length))
  const runtime = useWorkspace()
  const auth = useAuth()
  const installed = runtime.snapshot?.installed ?? []
  const activeId = runtime.snapshot?.active_connection_id ?? ''
  const timers = Object.fromEntries(
    Object.entries(runtime.snapshot?.states ?? {}).filter(([id]) =>
      ['mia.timer', 'mia.focus', 'mia.alarm'].includes(id),
    ),
  )
  const [now, setNow] = useState(Date.now())
  const [connections, setConnections] = useState<Connection[]>([])
  const [serverError, setServerError] = useState('')
  const [installing, setInstalling] = useState<Plugin | null>(null)
  const [opened, setOpened] = useState<Plugin | null>(null)
  const [mobileNav, setMobileNav] = useState(false)
  const notifiedDeadlines = useRef(new Map<string, number>())
  const allPlugins = [
    ...catalog,
    ...Object.values(runtime.snapshot?.manifests ?? {})
      .filter((m) => m.id.startsWith('community.'))
      .map(applicationPlugin),
  ]
  const active = connections.find((c) => c.id === activeId)
  const installedPlugins = allPlugins.filter((plugin) => installed.includes(plugin.id))
  const refresh = useCallback(async () => {
    try {
      setConnections(await api.connections())
      setServerError('')
    } catch (e) {
      setServerError(e instanceof Error ? e.message : 'Web API недоступен')
    }
  }, [])
  useEffect(() => {
    if (runtime.ready) void refresh()
  }, [refresh, runtime.ready, runtime.snapshot?.active_connection_id])
  useEffect(() => {
    const onHash = () => {
      setPage(currentPage())
      setPluginId(location.hash.slice('#plugin/'.length))
      setMobileNav(false)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(interval)
  }, [])
  useEffect(() => {
    const expired = Object.entries(timers).filter(
      ([, timer]) => timer.deadline != null && timer.deadline <= now,
    )
    if (expired.length === 0) return
    let shouldChime = false
    for (const [id, timer] of expired) {
      const notificationKey = `mia.alert.${runtime.snapshot?.subject_id}.${id}`
      if (
        notifiedDeadlines.current.get(id) === timer.deadline ||
        sessionStorage.getItem(notificationKey) === String(timer.deadline)
      )
        continue
      sessionStorage.setItem(notificationKey, String(timer.deadline))
      notifiedDeadlines.current.set(id, timer.deadline!)
      shouldChime = true
      toast.success(`⏰ ${timer.label}`, { description: 'Время пришло.', duration: 15000 })
    }
    if (shouldChime) chime()
  }, [now, timers])
  function navigate(next: Page) {
    location.hash = next
    setPage(next)
    setMobileNav(false)
    setOpened(null)
  }
  async function install() {
    if (!installing) return
    try {
      runtime.accept(
        await api.install(
          installing.id,
          installing.manifest ??
            (installing.recipe ? recipeManifest(installing.recipe) : undefined),
        ),
      )
      toast.success(`${installing.name}: skill подключён, виджет добавлен на дашборд`)
      setInstalling(null)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Не удалось установить плагин')
    }
  }
  async function uninstall(plugin: Plugin) {
    try {
      runtime.accept(await api.uninstall(plugin.id))
      toast.success('Плагин отключён, доступ внешних приложений отозван')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Не удалось удалить плагин')
    }
  }
  async function selectModel(id: string) {
    try {
      runtime.accept(await api.select(id))
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Не удалось выбрать модель')
    }
  }
  const runningTimers = Object.entries(timers).filter(
    ([, timer]) => timer.deadline != null && timer.deadline > now,
  )
  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? 'mobile-open' : ''}`}>
        <a className="brand" href="#store" aria-label="Мия — на главную">
          <span className="brand-symbol">м</span>
          <span>
            мия<span className="brand-period">.</span>
          </span>
        </a>
        <div className="space-switch">
          <span className="space-avatar">✳</span>
          <div>
            <strong>Моё пространство</strong>
            <small>Личное · Web POC</small>
          </div>
          <ChevronDown size={15} />
        </div>
        <span className="nav-caption">ОТКРОЙТЕ ВОЗМОЖНОСТИ</span>
        <nav aria-label="Главная навигация">
          {pages.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => navigate(id)}
              className={`nav-link ${page === id ? 'active' : ''} ${id === 'studio' ? 'nav-studio' : ''}`}
              aria-current={page === id ? 'page' : undefined}
            >
              <Icon size={19} strokeWidth={1.6} />
              <span>{label}</span>
              {id === 'library' && installed.length > 0 && (
                <span className="nav-count">{installed.length}</span>
              )}
              {id === 'studio' && <ArrowUpRight size={14} />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-note">
            <span className="local-dot" />
            <span>
              Больше свободы.
              <br />
              <strong>Меньше зависимости.</strong>
            </span>
          </div>
          <button className="sidebar-model" onClick={() => navigate('models')}>
            <span className={`model-dot ${active ? 'connected' : ''}`} />
            <div>
              <strong>{active?.label ?? 'Ваша модель'}</strong>
              <small>{active ? active.model : 'Подключить интеллект'}</small>
            </div>
            <ChevronRight size={14} />
          </button>
          <div className="sidebar-footer">
            <span>Мия © 2026</span>
            <span>сделано для людей ↗</span>
          </div>
        </div>
      </aside>
      {mobileNav && (
        <button
          className="mobile-backdrop"
          aria-label="Закрыть меню"
          onClick={() => setMobileNav(false)}
        />
      )}
      <main className="main-area">
        <header className="topbar">
          <button
            className="mobile-menu icon-button"
            aria-label="Открыть меню"
            onClick={() => setMobileNav(!mobileNav)}
          >
            <Layers3 size={21} />
          </button>
          <div className="breadcrumb">
            <span>Ваше пространство</span>
            <ChevronRight size={13} />
            <strong>
              {page === 'plugin' ? 'О плагине' : pages.find((p) => p.id === page)?.label}
            </strong>
          </div>
          <div className="topbar-right">
            <span className="poc-badge">
              <span /> WEB POC
            </span>
            <span className="account-name">{auth.user?.name ?? 'Локальное пространство'}</span>
            {auth.user && (
              <button className="text-button" onClick={() => void auth.logout()}>
                Выйти из Мии
              </button>
            )}
          </div>
        </header>
        <div className="page-content">
          <AnimatePresence mode="wait">
            <motion.div
              key={page === 'plugin' ? pluginId : page}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -5 }}
              transition={{ duration: 0.18 }}
            >
              {page === 'plugin' && (
                <PluginPage
                  plugin={allPlugins.find((p) => p.id === pluginId)}
                  installed={installed.includes(pluginId)}
                  onInstall={setInstalling}
                  onOpen={setOpened}
                />
              )}
              {page === 'store' && (
                <Store
                  plugins={allPlugins}
                  installed={installed}
                  onInstall={setInstalling}
                  onOpen={setOpened}
                  openModels={() => navigate('models')}
                />
              )}
              {page === 'dashboard' && (
                <Dashboard
                  plugins={allPlugins}
                  active={active}
                  now={now}
                  openModels={() => navigate('models')}
                  openStore={() => navigate('store')}
                />
              )}
              {page === 'apps' && <LinkedApps />}
              {page === 'library' && (
                <>
                  <div className="page-heading">
                    <div>
                      <span className="eyebrow">ПОД ВАШ РИТМ</span>
                      <h1>
                        Всё нужное.
                        <br />
                        <span>На своём месте.</span>
                      </h1>
                      <p>Ваши приложения, ваши маленькие повседневные ритуалы.</p>
                    </div>
                    <button className="button primary" onClick={() => navigate('store')}>
                      <Plus size={16} /> Добавить плагин
                    </button>
                  </div>
                  {installedPlugins.length === 0 ? (
                    <div className="empty-state">
                      <Layers3 size={40} strokeWidth={1} />
                      <h2>Здесь начинается ваше пространство</h2>
                      <p>
                        Добавьте первый плагин из магазина.
                        <br />
                        Часы, заметки и таймер работают без AI-модели.
                      </p>
                      <button className="button primary" onClick={() => navigate('store')}>
                        Открыть магазин <ArrowRight size={16} />
                      </button>
                    </div>
                  ) : (
                    <div className="library-grid">
                      {installedPlugins.map((plugin) => (
                        <div className="library-card" key={plugin.id}>
                          <PluginArt kind={plugin.art} compact />
                          <div className="library-card-info">
                            <span className="eyebrow">
                              {plugin.manifest ? 'ПРИЛОЖЕНИЕ АВТОРА' : 'SKILL + VIEW'}
                            </span>
                            <h3>{plugin.name}</h3>
                            <p>{plugin.description}</p>
                            {plugin.id === 'mia.timer' && (
                              <a
                                className="text-button app-download-link"
                                href={webPath('/api/web/downloads/timer.zip')}
                              >
                                <Download size={14} /> Скачать отдельное приложение
                              </a>
                            )}
                            <div className="library-actions">
                              <button className="button primary" onClick={() => setOpened(plugin)}>
                                Открыть <ArrowUpRight size={16} />
                              </button>
                              <button
                                className="icon-button"
                                aria-label={`Удалить плагин ${plugin.name}`}
                                onClick={() => uninstall(plugin)}
                              >
                                <Trash2 size={17} />
                              </button>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
              {page === 'models' && (
                <Models
                  connections={connections}
                  activeId={activeId}
                  select={selectModel}
                  refresh={refresh}
                  serverError={serverError}
                />
              )}
              {page === 'chat' && (
                <Chat key={activeId} active={active} openModels={() => navigate('models')} />
              )}
              {page === 'studio' && <Studio onImport={setInstalling} />}
            </motion.div>
          </AnimatePresence>
        </div>
        {runningTimers.length > 0 && (
          <div className="running-timers" aria-label="Активные таймеры">
            <button
              onClick={() => {
                unlockAudio()
                toast.success('Звук напоминаний включён')
              }}
              aria-label="Включить звук напоминаний"
            >
              <Bell size={14} /> Звук
            </button>
            {runningTimers.map(([id, timer]) => (
              <button
                key={id}
                onClick={() => {
                  const plugin = allPlugins.find((p) => p.id === id)
                  if (plugin && installed.includes(id)) setOpened(plugin)
                }}
              >
                <span className="local-dot" />
                {timer.label}
                <strong>{formatDuration(timer.deadline! - now)}</strong>
              </button>
            ))}
          </div>
        )}
        <footer className="main-footer">
          <span>
            <span className="footer-symbol">✳</span> Ваши модели. Ваши возможности.
          </span>
          <span>Локально или в облаке — решать вам.</span>
        </footer>
      </main>
      {installing && (
        <Dialog
          title={installed.includes(installing.id) ? 'Обновить рецепт' : 'Добавить в пространство'}
          description={`${installing.name} · ${installing.recipe?.author ?? 'Команда Мия'} · бесплатно`}
          onClose={() => setInstalling(null)}
        >
          <PluginArt kind={installing.art} compact />
          <p className="install-description">{installing.description}</p>
          {installing.id === 'mia.digest' && (
            <div className="inline-notice">
              <p>
                Также будет установлен «Интернет»: поиск свежих источников и чтение публичных
                сайтов. Эти возможности станут доступны выбранной модели.
              </p>
            </div>
          )}
          {installing.manifest?.view.type === 'html' && (
            <p className="field-note">
              Виджет автора содержит HTML/JavaScript и запустится в изолированном iframe с доступом
              только к разрешённым методам своего плагина.
            </p>
          )}
          <div className="permissions">
            <h3>
              <ShieldCheck size={17} /> Разрешения плагина
            </h3>
            {installing.permissions.length === 0 ? (
              <p>Дополнительные разрешения не нужны.</p>
            ) : (
              installing.permissions.map((permission) => (
                <p key={permission}>
                  <Check size={15} />
                  {permissions[permission]}
                </p>
              ))
            )}
          </div>
          <p className="field-note">
            Установка подключает skill и действия к выбранной модели. Виджет и привязанные
            приложения используют общее состояние вашего пространства.
          </p>
          <button className="button primary full-width" onClick={install}>
            <Plus size={17} />
            {installed.includes(installing.id) ? 'Обновить рецепт' : 'Добавить плагин'}
          </button>
        </Dialog>
      )}
      {opened && (
        <Dialog
          title={opened.name}
          description={opened.tagline}
          onClose={() => setOpened(null)}
          wide
        >
          <Tool plugin={opened} now={now} active={active} openModels={() => navigate('models')} />
        </Dialog>
      )}
    </div>
  )
}

function Store({
  plugins,
  installed,
  onInstall,
  onOpen,
  openModels,
}: {
  plugins: Plugin[]
  installed: string[]
  onInstall: (plugin: Plugin) => void
  onOpen: (plugin: Plugin) => void
  openModels: () => void
}) {
  const [category, setCategory] = useState('Все плагины')
  const [query, setQuery] = useState('')
  const filtered = plugins.filter(
    (plugin) =>
      (category === 'Все плагины' || plugin.category === category) &&
      `${plugin.name} ${plugin.description}`.toLowerCase().includes(query.toLowerCase()),
  )
  return (
    <>
      <section className="store-hero">
        <div className="hero-copy">
          <span className="hero-pill">
            <Sparkles size={13} /> Ваша AI-модель умеет больше
          </span>
          <h1>
            Маленькие приложения.
            <br />
            <em>Большие возможности.</em>
          </h1>
          <p>
            Всё, что делает жизнь чуть проще, — в одном месте.
            <br />
            Добавляйте плагины и подключайте любимые модели.
          </p>
          <div className="hero-actions">
            <button
              className="button primary"
              onClick={() =>
                document.getElementById('catalog')?.scrollIntoView({
                  behavior: matchMedia('(prefers-reduced-motion: reduce)').matches
                    ? 'auto'
                    : 'smooth',
                })
              }
            >
              Найти свой плагин <ArrowRight size={16} />
            </button>
            <button className="hero-model-link" onClick={openModels}>
              Подключить модель <ArrowUpRight size={15} />
            </button>
          </div>
          <div className="hero-proof">
            <span>
              <Check size={12} /> Ваши API-ключи
            </span>
            <span>
              <Check size={12} /> Локальные модели
            </span>
            <span>
              <Check size={12} /> Бесплатные плагины
            </span>
          </div>
        </div>
        <div className="hero-art" aria-hidden="true">
          <div className="hero-grid" />
          <div className="orb orb-main">
            <span>✳</span>
          </div>
          <div className="orb orb-small" />
          <div className="orb orb-tiny" />
          <div className="hero-float float-one">
            <Sparkles size={15} /> под ваш ритм
          </div>
          <div className="hero-float float-two">
            <Zap size={15} /> больше возможностей
          </div>
          <span className="hero-art-caption">MADE TO MAKE LIFE EASIER</span>
        </div>
      </section>
      <div className="discovery-strip">
        <span>
          <Blocks size={16} />
          <strong>Соберите свою Мию</strong>
        </span>
        <p>Начните с привычного. Откройте что-то новое.</p>
        <span className="strip-right">
          {plugins.length} приложений для вашего дня <ArrowDownToLine size={14} />
        </span>
      </div>
      <section className="catalog-section" id="catalog">
        <div className="section-heading">
          <div>
            <span className="eyebrow">МЕНЬШЕ РУТИНЫ, БОЛЬШЕ ЖИЗНИ</span>
            <h2>
              Найдите что-то своё <span className="count-pill">{plugins.length}</span>
            </h2>
          </div>
          <label className="search-box">
            <Search size={17} />
            <input
              aria-label="Поиск плагинов"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Найти плагин…"
            />
            <span>⌕</span>
          </label>
        </div>
        <div className="catalog-filters">
          <div className="category-chips">
            {['Все плагины', 'Повседневное', 'Продуктивность', 'AI'].map((item) => (
              <button
                key={item}
                className={`chip ${category === item ? 'selected' : ''}`}
                onClick={() => setCategory(item)}
              >
                {item === 'Все плагины' && <SlidersHorizontal size={13} />}
                {item}
              </button>
            ))}
          </div>
          <span className="catalog-count">{filtered.length} в каталоге</span>
        </div>
        <div className="plugin-grid">
          {filtered.map((plugin, index) => (
            <motion.article
              className="plugin-card"
              key={plugin.id}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(index * 0.035, 0.25) }}
            >
              <a
                className="card-art-wrap"
                href={`#plugin/${plugin.id}`}
                aria-label={`О плагине ${plugin.name}`}
              >
                <PluginArt kind={plugin.art} />
                <span className={`art-tag ${plugin.ai ? 'ai-tag' : ''}`}>
                  {plugin.ai ? <Sparkles size={11} /> : <Terminal size={11} />}
                  {plugin.ai ? 'С вашей AI-моделью' : 'Управление через Мию'}
                </span>
              </a>
              <div className="plugin-info">
                <div className="plugin-title">
                  <h3>
                    <a href={`#plugin/${plugin.id}`}>{plugin.name}</a>
                  </h3>
                  <ArrowUpRight size={17} />
                </div>
                <p>{plugin.description}</p>
                <div className="plugin-meta">
                  <span>
                    {plugin.manifest?.author ?? plugin.recipe?.author ?? 'Мия'}
                    <span className="meta-dot">·</span>
                    {plugin.category}
                  </span>
                  <strong>Бесплатно</strong>
                </div>
                <button
                  className={`plugin-install ${installed.includes(plugin.id) ? 'is-installed' : ''}`}
                  onClick={() =>
                    installed.includes(plugin.id) ? onOpen(plugin) : onInstall(plugin)
                  }
                >
                  {installed.includes(plugin.id) ? <Check size={15} /> : <Plus size={15} />}
                  {installed.includes(plugin.id) ? 'Открыть плагин' : 'Добавить в пространство'}
                  <ArrowRight size={14} />
                </button>
              </div>
            </motion.article>
          ))}
        </div>
        {filtered.length === 0 && (
          <div className="empty-state small">
            <Search size={28} />
            <h3>Пока ничего не нашлось</h3>
            <p>Попробуйте другой запрос или категорию.</p>
            <button
              className="button secondary"
              onClick={() => {
                setQuery('')
                setCategory('Все плагины')
              }}
            >
              Сбросить фильтры
            </button>
          </div>
        )}
      </section>
      <section className="creator-banner">
        <span className="creator-icon">
          <Code2 size={29} strokeWidth={1.5} />
        </span>
        <div>
          <span className="eyebrow">ХОРОШАЯ ИДЕЯ МОЖЕТ БЫТЬ ВАШЕЙ</span>
          <h3>Создайте то, чего здесь ещё нет.</h3>
          <p>Соберите свой AI-рецепт и поделитесь им с другими.</p>
        </div>
        <a className="button secondary" href="#studio">
          Стать автором <ArrowUpRight size={16} />
        </a>
      </section>
    </>
  )
}

function Studio({ onImport }: { onImport: (plugin: Plugin) => void }) {
  const [source, setSource] = useState(JSON.stringify(exampleApplication, null, 2))
  const [error, setError] = useState('')
  const [reading, setReading] = useState(false)
  function parse(): Recipe | ApplicationManifest | null {
    setError('')
    if (source.length > 48000) {
      setError('Рецепт слишком большой. Лимит: 48 000 символов.')
      return null
    }
    try {
      const value = JSON.parse(source)
      return value.schemaVersion === '2.0'
        ? applicationSchema.parse(value)
        : recipeSchema.parse(value)
    } catch (e) {
      setError(
        e instanceof z.ZodError
          ? e.issues
              .map((issue) => `${issue.path.join('.') || 'manifest'}: ${issue.message}`)
              .join('\n')
          : 'Проверьте синтаксис JSON.',
      )
      return null
    }
  }
  function download() {
    const recipe = parse()
    if (!recipe) return
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(recipe, null, 2)], { type: 'application/json' }),
    )
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${recipe.id}.json`
    anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">ОТ ИДЕИ ДО ПРИЛОЖЕНИЯ</span>
          <h1>
            Мия открыта
            <br />
            <span>вашим идеям.</span>
          </h1>
          <p>Плагин — ваш интерфейс, skill и действия. Модель выбирает пользователь.</p>
        </div>
        <span className="heading-icon">
          <Code2 size={45} strokeWidth={1} />
        </span>
      </div>
      <div className="studio-formats">
        <button
          className="button secondary"
          onClick={() => setSource(JSON.stringify(exampleApplication, null, 2))}
        >
          Приложение v2
        </button>
        <button
          className="button secondary"
          onClick={() => setSource(JSON.stringify(exampleRecipe, null, 2))}
        >
          Рецепт v1
        </button>
        <a className="button secondary" href={webPath('/api/web/downloads/timer.zip')}>
          <Download size={15} /> SDK + пример приложения
        </a>
      </div>
      <div className="studio-layout">
        <div className="studio-guide">
          <span className="studio-number">01 — 03</span>
          <h2>Ваш первый плагин</h2>
          <ol>
            <li>
              <strong>Опишите идею</strong>
              <p>Название, автор и короткое описание помогут другим понять пользу.</p>
            </li>
            <li>
              <strong>Добавьте инструкцию</strong>
              <p>
                В skill объясните, когда использовать приложение. В actions опишите команды, а в
                view — виджет.
              </p>
            </li>
            <li>
              <strong>Попробуйте и поделитесь</strong>
              <p>
                Импортируйте manifest, проверьте действия через чат и дашборд. Подключите отдельное
                приложение через SDK.
              </p>
            </li>
          </ol>
          <div className="studio-note">
            <ShieldCheck size={20} />
            <p>
              Приложение получает право работать от имени пользователя только после привязки. Ключ
              модели всегда остаётся у Мии.
            </p>
          </div>
          <p className="field-note">
            Импорт локальный. Публичная публикация, донаты и модерация появятся на следующем этапе.
          </p>
        </div>
        <div className="recipe-editor">
          <div className="editor-header">
            <span>
              <span className="local-dot" /> plugin.manifest.json
            </span>
            <label className="text-button file-upload">
              {reading ? <LoaderCircle className="spin" size={14} /> : <Upload size={14} />}Импорт
              файла
              <input
                type="file"
                accept=".json,application/json"
                aria-label="Импорт JSON-рецепта"
                onChange={async (e) => {
                  const file = e.target.files?.[0]
                  if (!file) return
                  if (file.size > 96000) {
                    setError('Файл слишком большой. Лимит: 96 КБ.')
                    e.target.value = ''
                    return
                  }
                  setReading(true)
                  try {
                    setSource(await file.text())
                    setError('')
                  } catch {
                    setError('Не удалось прочитать файл.')
                  } finally {
                    setReading(false)
                    e.target.value = ''
                  }
                }}
              />
            </label>
          </div>
          <textarea
            className="code-editor"
            aria-label="JSON-рецепт плагина"
            spellCheck={false}
            value={source}
            onChange={(e) => setSource(e.target.value)}
            maxLength={48000}
          />
          <div className="editor-actions">
            <button className="button secondary" onClick={download}>
              <Download size={15} /> Скачать JSON
            </button>
            <button
              className="button primary"
              onClick={() => {
                const recipe = parse()
                if (recipe)
                  onImport(
                    recipe.schemaVersion === '2.0'
                      ? applicationPlugin(recipe)
                      : recipePlugin(recipe),
                  )
              }}
            >
              <Plus size={16} /> Добавить рецепт
            </button>
          </div>
          {error && (
            <pre className="error-message" role="alert">
              {error}
            </pre>
          )}
        </div>
      </div>
    </>
  )
}
