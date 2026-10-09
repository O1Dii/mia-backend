import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react'
import { ArrowDown, ArrowUp, LayoutGrid, LoaderCircle, Pencil, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import {
  api,
  type CatalogDetail,
  type Dashboard as DashboardData,
  type DashboardTile,
  type Installation,
  type TileSize,
} from '../lib/api'
import { ApiError, messageOf } from '../lib/http'
import { Link } from '../lib/router'
import { TileFrame } from '../plugins/TileFrame'
import { Dialog } from './Dialog'

type TileMeta = { plugin: string; title: string; sizes: TileSize[]; hidden: boolean }
type Option = {
  installation: Installation
  plugin: string
  id: string
  title: string
  sizes: TileSize[]
}

const sizeLabel: Record<TileSize, string> = {
  '1x1': 'Маленькая (1×1)',
  '2x1': 'Узкая (2×1)',
  '2x2': 'Средняя (2×2)',
  '4x2': 'Широкая (4×2)',
  '4x4': 'Большая (4×4)',
}

/** The dashboard: every tile is an MCP Apps document of an installed plugin. */
export function Dashboard() {
  const [dash, setDash] = useState<DashboardData | null>(null)
  const [installs, setInstalls] = useState<Installation[]>([])
  const [details, setDetails] = useState<Record<string, CatalogDetail | null>>({})
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(false)
  const [adding, setAdding] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const [layout, installations] = await Promise.all([api.dashboard(), api.installations()])
      setDash(layout)
      setInstalls(installations.items)
      const ids = [...new Set(installations.items.map((i) => i.plugin_id))]
      const entries = await Promise.all(
        ids.map(async (id) => [id, await api.plugin(id).catch(() => null)] as const),
      )
      setDetails(Object.fromEntries(entries))
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить дашборд.'))
    }
  }, [])
  useEffect(() => {
    void load()
  }, [load])

  const meta = useCallback(
    (tile: DashboardTile): TileMeta => {
      const installation = installs.find((i) => i.id === tile.installation_id)
      const detail = installation ? details[installation.plugin_id] : null
      const declared = detail?.manifest.surfaces?.tiles?.find((t) => t.id === tile.tile_id)
      return {
        plugin: detail?.name ?? installation?.plugin_id ?? 'Плагин',
        title: declared?.title ?? detail?.name ?? tile.tile_id,
        sizes: declared?.sizes ?? [tile.size],
        hidden: declared?.mobile === 'hidden',
      }
    },
    [installs, details],
  )

  const ordered = useMemo(
    () => [...(dash?.tiles ?? [])].sort((a, b) => a.position - b.position),
    [dash],
  )

  async function save(next: DashboardTile[]) {
    if (!dash) return
    setSaving(true)
    const tiles = next.map((tile, index) => ({
      id: tile.id,
      installation_id: tile.installation_id,
      tile_id: tile.tile_id,
      settings: tile.settings,
      size: tile.size,
      position: index,
      mobile: { size: tile.mobile?.size ?? tile.size, position: index },
    }))
    try {
      setDash(await api.putDashboard({ expected_version: dash.expected_version, tiles }))
    } catch (e) {
      if (e instanceof ApiError && e.code === 'version_conflict') await load()
      toast.error(messageOf(e, 'Не удалось сохранить расположение.'))
    } finally {
      setSaving(false)
    }
  }

  function resize(tile: DashboardTile, size: TileSize) {
    void save(
      ordered.map((t) => (t.id === tile.id ? { ...t, size, mobile: { ...t.mobile, size } } : t)),
    )
  }
  function move(tile: DashboardTile, delta: -1 | 1) {
    const list = [...ordered]
    const index = list.findIndex((t) => t.id === tile.id)
    const target = index + delta
    if (target < 0 || target >= list.length) return
    ;[list[index], list[target]] = [list[target]!, list[index]!]
    void save(list)
  }
  function remove(tile: DashboardTile) {
    void save(ordered.filter((t) => t.id !== tile.id))
  }

  const available = useMemo(() => {
    const options: Option[] = []
    for (const installation of installs) {
      if (installation.status !== 'active') continue
      const detail = details[installation.plugin_id]
      for (const tile of detail?.manifest.surfaces?.tiles ?? []) {
        if (ordered.some((t) => t.installation_id === installation.id && t.tile_id === tile.id))
          continue
        options.push({
          installation,
          plugin: detail?.name ?? installation.plugin_id,
          id: tile.id,
          title: tile.title ?? detail?.name ?? tile.id,
          sizes: tile.sizes,
        })
      }
    }
    return options
  }, [installs, details, ordered])

  async function add(option: Option, size: TileSize) {
    const tile: DashboardTile = {
      id: crypto.randomUUID(),
      installation_id: option.installation.id,
      tile_id: option.id,
      size,
      position: ordered.length,
      mobile: { size, position: ordered.length },
      settings: {},
      state_version: 0,
    }
    setAdding(false)
    await save([...ordered, tile])
  }

  return (
    <section className="mw-page" aria-labelledby="dash-title">
      <header className="mw-page__head mw-dash__head">
        <div>
          <h1 id="dash-title">Дашборд</h1>
          <p>Плитки установленных плагинов. Они обновляются сами и работают без модели.</p>
        </div>
        <div className="mw-actions">
          <button
            className="mia-btn mia-btn--secondary"
            onClick={() => setAdding(true)}
            disabled={!dash}
          >
            <Plus size={16} aria-hidden="true" /> Добавить плитку
          </button>
          {ordered.length > 0 && (
            <button
              className={`mia-btn ${editing ? 'mia-btn--primary' : 'mia-btn--ghost'}`}
              aria-pressed={editing}
              onClick={() => setEditing((value) => !value)}
            >
              <Pencil size={16} aria-hidden="true" /> {editing ? 'Готово' : 'Изменить'}
            </button>
          )}
        </div>
      </header>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      {!dash && !error && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      )}
      {dash && ordered.length === 0 && (
        <div className="mw-empty mia-glass">
          <LayoutGrid size={28} aria-hidden="true" />
          <h2>На дашборде пока пусто</h2>
          <p>
            {available.length > 0
              ? 'Добавьте плитку установленного плагина, например таймер.'
              : 'Установите плагин с плиткой в магазине, например таймер.'}
          </p>
          {available.length > 0 ? (
            <button className="mia-btn mia-btn--primary" onClick={() => setAdding(true)}>
              Добавить плитку
            </button>
          ) : (
            <Link to="/store" className="mia-btn mia-btn--primary">
              Открыть магазин
            </Link>
          )}
        </div>
      )}
      <ul className="mw-tiles" aria-label="Плитки">
        {ordered.map((tile, index) => {
          const info = meta(tile)
          return (
            <li
              key={tile.id}
              className={`mw-tile mia-glass${info.hidden ? ' mw-tile--no-mobile' : ''}`}
              data-size={tile.size}
              data-mobile-size={tile.mobile.size}
              data-tile={tile.tile_id}
              style={
                {
                  '--mw-pos': tile.position,
                  '--mw-pos-mobile': tile.mobile.position,
                } as CSSProperties
              }
            >
              <header className="mw-tile__head">
                <h2>{info.title}</h2>
                <span className="mw-tile__plugin">{info.plugin}</span>
              </header>
              <div className="mw-tile__body">
                <TileFrame
                  installationId={tile.installation_id}
                  tileId={tile.tile_id}
                  title={info.title}
                />
              </div>
              {editing && (
                <div
                  className="mw-tile__edit"
                  role="group"
                  aria-label={`Настройки плитки ${info.title}`}
                >
                  <label className="mw-tile__size">
                    <span className="mw-muted">Размер</span>
                    <select
                      value={tile.size}
                      disabled={saving || info.sizes.length < 2}
                      onChange={(event) => resize(tile, event.target.value as TileSize)}
                    >
                      {info.sizes.map((size) => (
                        <option key={size} value={size}>
                          {sizeLabel[size]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="mia-icon-btn"
                    aria-label={`Сдвинуть «${info.title}» раньше`}
                    disabled={saving || index === 0}
                    onClick={() => move(tile, -1)}
                  >
                    <ArrowUp size={18} />
                  </button>
                  <button
                    className="mia-icon-btn"
                    aria-label={`Сдвинуть «${info.title}» позже`}
                    disabled={saving || index === ordered.length - 1}
                    onClick={() => move(tile, 1)}
                  >
                    <ArrowDown size={18} />
                  </button>
                  <button
                    className="mia-icon-btn"
                    aria-label={`Убрать плитку «${info.title}»`}
                    disabled={saving}
                    onClick={() => remove(tile)}
                  >
                    <Trash2 size={18} />
                  </button>
                </div>
              )}
            </li>
          )
        })}
      </ul>
      {adding && (
        <Dialog
          title="Добавить плитку"
          description="Плитки есть у установленных плагинов."
          onClose={() => setAdding(false)}
        >
          {available.length === 0 ? (
            <div className="mw-stack">
              <p className="mw-muted">
                Нет плиток, которые можно добавить. Установите плагин с плиткой в магазине.
              </p>
              <Link
                to="/store"
                className="mia-btn mia-btn--primary"
                onClick={() => setAdding(false)}
              >
                Открыть магазин
              </Link>
            </div>
          ) : (
            <ul className="mw-stack">
              {available.map((option) => (
                <li key={`${option.installation.id}/${option.id}`} className="mw-choice">
                  <div>
                    <strong>{option.title}</strong>
                    <p className="mw-muted">{option.plugin}</p>
                  </div>
                  <div className="mw-actions">
                    {option.sizes.map((size) => (
                      <button
                        key={size}
                        className="mia-btn mia-btn--secondary"
                        aria-label={`Добавить «${option.title}», ${sizeLabel[size]}`}
                        onClick={() => void add(option, size)}
                      >
                        {size}
                      </button>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Dialog>
      )}
    </section>
  )
}
