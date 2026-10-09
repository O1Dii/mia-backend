import { useEffect, useMemo, useState } from 'react'
import { Link2, LoaderCircle, Search } from 'lucide-react'
import { api, type CatalogEntry } from '../lib/api'
import { messageOf } from '../lib/http'
import { Link } from '../lib/router'
import { PluginMark, Stars, categoryTitles, useStoreState } from './StoreShared'

export function Store() {
  const state = useStoreState()
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')
  const [items, setItems] = useState<CatalogEntry[] | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loadingMore, setLoadingMore] = useState(false)
  const [categories, setCategories] = useState<string[]>([])

  useEffect(() => {
    let active = true
    const timer = window.setTimeout(
      () => {
        api
          .plugins(query.trim() || undefined, category || undefined)
          .then((page) => {
            if (!active) return
            setItems(page.items)
            setCursor(page.next_cursor)
            setError('')
            if (!query && !category)
              setCategories([...new Set(page.items.map((p) => p.category))].sort())
          })
          .catch((e) => active && setError(messageOf(e, 'Не удалось загрузить каталог.')))
      },
      query ? 250 : 0,
    )
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [query, category])

  const installedIds = useMemo(
    () => new Set(state.installations?.map((i) => i.plugin_id) ?? []),
    [state.installations],
  )

  async function more() {
    if (!cursor) return
    setLoadingMore(true)
    try {
      const page = await api.plugins(query.trim() || undefined, category || undefined, cursor)
      setItems((old) => [...(old ?? []), ...page.items])
      setCursor(page.next_cursor)
    } catch (e) {
      setError(messageOf(e))
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <section className="mw-page" aria-labelledby="store-title">
      <header className="mw-page__head">
        <h1 id="store-title">Магазин</h1>
        <p>
          Плагины дают Мие новые возможности. Перед установкой вы видите, что именно они смогут
          делать.
        </p>
      </header>
      <div className="mw-toolbar">
        <label className="mia-field mia-glass mw-search">
          <Search size={18} aria-hidden="true" />
          <input
            className="mia-field__input"
            type="search"
            placeholder="Найти плагин"
            aria-label="Найти плагин"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <Link to="/store/add-url" className="mia-btn mia-btn--secondary">
          <Link2 size={16} aria-hidden="true" /> Добавить по URL
        </Link>
      </div>
      {categories.length > 1 && (
        <div className="mw-chips" role="group" aria-label="Категории">
          <button
            className={`mia-chip mw-chip-btn ${category === '' ? 'mia-chip--accent' : ''}`}
            aria-pressed={category === ''}
            onClick={() => setCategory('')}
          >
            Все
          </button>
          {categories.map((value) => (
            <button
              key={value}
              className={`mia-chip mw-chip-btn ${category === value ? 'mia-chip--accent' : ''}`}
              aria-pressed={category === value}
              onClick={() => setCategory(value)}
            >
              {categoryTitles[value] ?? value}
            </button>
          ))}
        </div>
      )}
      {(error || state.error) && (
        <p className="mw-error" role="alert">
          {error || state.error}
        </p>
      )}
      {items === null && !error && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем каталог…
        </p>
      )}
      {items?.length === 0 && <p className="mw-muted">Ничего не найдено.</p>}
      <ul className="mw-grid">
        {items?.map((entry) => (
          <li key={entry.plugin_id}>
            <Link
              to={`/store/plugin/${encodeURIComponent(entry.plugin_id)}`}
              className="mia-glass mw-plugin-card"
              aria-label={`О плагине ${entry.name}`}
            >
              <PluginMark entry={entry} />
              <div className="mw-plugin-card__body">
                <h2>{entry.name}</h2>
                <p>{entry.tagline || entry.description}</p>
                <div className="mw-plugin-card__meta">
                  <span className="mia-chip">
                    {categoryTitles[entry.category] ?? entry.category}
                  </span>
                  {entry.manifest.server?.auth === 'oauth' && (
                    <span className="mia-chip">Нужен аккаунт</span>
                  )}
                  {installedIds.has(entry.plugin_id) && (
                    <span className="mia-chip mia-chip--success">Установлено</span>
                  )}
                  <Stars average={entry.rating.average} count={entry.rating.count} />
                </div>
              </div>
            </Link>
          </li>
        ))}
      </ul>
      {cursor && (
        <button
          className="mia-btn mia-btn--secondary mw-more"
          disabled={loadingMore}
          onClick={() => void more()}
        >
          {loadingMore && <LoaderCircle size={16} className="spin" />} Показать ещё
        </button>
      )}
    </section>
  )
}
