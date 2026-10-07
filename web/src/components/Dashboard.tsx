import { useState } from 'react'
import {
  ArrowDown,
  ArrowUp,
  GripVertical,
  LayoutDashboard,
  Maximize2,
  Plus,
  Settings2,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { api, type Connection, type WidgetLayout } from '../lib/api'
import { useWorkspace } from '../lib/workspace'
import type { Plugin } from '../plugins/catalog'
import { Tool } from '../plugins/Tools'

export function Dashboard({
  plugins,
  active,
  now,
  openModels,
  openStore,
}: {
  plugins: Plugin[]
  active?: Connection
  now: number
  openModels: () => void
  openStore: () => void
}) {
  const { snapshot, accept, error } = useWorkspace()
  const [editing, setEditing] = useState(false)
  const [dragging, setDragging] = useState<string | null>(null)
  const widgets = snapshot?.layout ?? []
  const available = plugins.filter(
    (p) => snapshot?.installed.includes(p.id) && !widgets.some((w) => w.plugin_id === p.id),
  )
  async function save(next: WidgetLayout[]) {
    try {
      accept(await api.layout(next))
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Не удалось сохранить раскладку')
    }
  }
  function move(id: string, position: number) {
    const next = [...widgets]
    const index = next.findIndex((w) => w.id === id)
    if (index < 0) return
    const [widget] = next.splice(index, 1)
    next.splice(Math.max(0, Math.min(position, next.length)), 0, widget)
    void save(next)
  }
  return (
    <div className="dashboard-page">
      <div className="page-heading dashboard-heading">
        <div>
          <span className="eyebrow">ОДНА МОДЕЛЬ. ВАШИ ПРИЛОЖЕНИЯ.</span>
          <h1>
            Ваш день.
            <br />
            <span>В одном пространстве.</span>
          </h1>
          <p>Скажите Мии, что сделать, — ваши виджеты подхватят.</p>
        </div>
        <button
          className={`button ${editing ? 'primary' : 'secondary'}`}
          onClick={() => setEditing(!editing)}
        >
          <Settings2 size={16} />
          {editing ? 'Готово' : 'Настроить дашборд'}
        </button>
      </div>
      <div className="dashboard-model-bar">
        <span className={`model-dot ${active ? 'connected' : ''}`} />
        <span>
          {active
            ? `${active.label} · ${active.model}`
            : 'Подключите модель для управления через чат'}
        </span>
        <a href="#chat">Попросить Мию →</a>
      </div>
      {error && (
        <p className="error-message" role="alert">
          {error}
        </p>
      )}
      {widgets.length === 0 ? (
        <div className="empty-state">
          <LayoutDashboard size={40} />
          <h2>Первые плитки для вашего дня</h2>
          <p>
            Установите таймер, новостную сводку или приложение автора.
            <br />
            Его view появится здесь и будет связан с вашей моделью.
          </p>
          <button className="button primary" onClick={openStore}>
            <Plus size={16} /> Выбрать плагины
          </button>
        </div>
      ) : (
        <div className={`dashboard-grid ${editing ? 'is-editing' : ''}`}>
          {widgets.map((widget, index) => {
            const plugin = plugins.find((p) => p.id === widget.plugin_id)
            if (!plugin) return null
            return (
              <section
                className={`dashboard-tile tile-${widget.size}`}
                data-plugin={plugin.id}
                key={widget.id}
                onDragOver={(e) => {
                  if (editing) e.preventDefault()
                }}
                onDrop={(e) => {
                  e.preventDefault()
                  if (dragging) move(dragging, index)
                  setDragging(null)
                }}
              >
                <div className="tile-heading">
                  <div>
                    <span className={`tile-color ${plugin.color}`} />
                    <h2>{plugin.name}</h2>
                  </div>
                  {editing ? (
                    <div className="tile-controls">
                      <button
                        className="icon-button drag-handle"
                        draggable
                        onDragStart={() => setDragging(widget.id)}
                        aria-label={`Перетащить ${plugin.name}`}
                      >
                        <GripVertical size={16} />
                      </button>
                      <button
                        className="icon-button"
                        disabled={index === 0}
                        aria-label={`Поднять ${plugin.name}`}
                        onClick={() => move(widget.id, index - 1)}
                      >
                        <ArrowUp size={15} />
                      </button>
                      <button
                        className="icon-button"
                        disabled={index === widgets.length - 1}
                        aria-label={`Опустить ${plugin.name}`}
                        onClick={() => move(widget.id, index + 1)}
                      >
                        <ArrowDown size={15} />
                      </button>
                      <button
                        className="icon-button"
                        aria-label={`Убрать виджет ${plugin.name}`}
                        onClick={() => void save(widgets.filter((w) => w.id !== widget.id))}
                      >
                        <X size={15} />
                      </button>
                    </div>
                  ) : (
                    <Maximize2 size={14} className="muted" />
                  )}
                </div>
                {editing && (
                  <label className="widget-size">
                    Размер плитки
                    <select
                      aria-label={`Размер ${plugin.name}`}
                      value={widget.size}
                      onChange={(e) =>
                        void save(
                          widgets.map((w) =>
                            w.id === widget.id
                              ? { ...w, size: e.target.value as WidgetLayout['size'] }
                              : w,
                          ),
                        )
                      }
                    >
                      <option value="small">Компактный</option>
                      <option value="medium">Обычный</option>
                      <option value="wide">Широкий</option>
                    </select>
                  </label>
                )}
                <Tool plugin={plugin} now={now} active={active} openModels={openModels} compact />
              </section>
            )
          })}
        </div>
      )}
      {editing && (
        <div className="dashboard-add">
          <h3>Добавить плитку</h3>
          {available.map((plugin) => (
            <button
              className="button secondary"
              key={plugin.id}
              onClick={() =>
                void save([
                  ...widgets,
                  { id: crypto.randomUUID(), plugin_id: plugin.id, size: 'medium' },
                ])
              }
            >
              <Plus size={15} />
              {plugin.name}
            </button>
          ))}
          <button className="button secondary" onClick={openStore}>
            <Plus size={15} /> Найти ещё в магазине
          </button>
        </div>
      )}
      {!!snapshot?.audit.length && (
        <details className="activity-log">
          <summary>Последние действия Мии и приложений</summary>
          {[...snapshot.audit]
            .reverse()
            .slice(0, 8)
            .map((item) => (
              <div key={item.id}>
                <span className={item.ok ? 'activity-ok' : 'activity-error'}>
                  {item.ok ? '✓' : '!'}
                </span>
                <span>
                  {plugins.find((p) => p.id === item.plugin_id)?.name ?? item.plugin_id} ·{' '}
                  {item.name.split('__')[1]}
                </span>
                <small>{new Date(item.at).toLocaleTimeString('ru')}</small>
              </div>
            ))}
        </details>
      )}
    </div>
  )
}
