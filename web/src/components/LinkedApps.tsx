import { useCallback, useEffect, useState } from 'react'
import { Download, Link2, ShieldCheck, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api, webPath } from '../lib/api'
import { useWorkspace } from '../lib/workspace'
import { permissions } from '../plugins/catalog'

export function LinkedApps() {
  const { snapshot } = useWorkspace()
  const [code, setCode] = useState('')
  const [request, setRequest] = useState<Awaited<ReturnType<typeof api.inspectApp>> | null>(null)
  const [apps, setApps] = useState<Awaited<ReturnType<typeof api.apps>>>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const refresh = useCallback(async () => {
    try {
      setApps(await api.apps())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Сервер недоступен')
    }
  }, [])
  useEffect(() => {
    void refresh()
  }, [refresh])
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">ВАША МИЯ ЗА ПРЕДЕЛАМИ ВКЛАДКИ</span>
          <h1>
            Разные приложения.
            <br />
            <span>Одно пространство.</span>
          </h1>
          <p>
            Привяжите приложение автора к своему пространству. Оно получит доступ к вашей выбранной
            модели и только к состоянию своего плагина.
          </p>
        </div>
        <span className="heading-icon">
          <Link2 size={40} />
        </span>
      </div>
      <div className="pairing-layout">
        <section className="pairing-panel">
          <h2>Подключить приложение</h2>
          <p className="field-note">
            Откройте приложение и нажмите «Подключиться к Мии». Введите показанный им код.
          </p>
          <form
            onSubmit={async (e) => {
              e.preventDefault()
              setBusy(true)
              setError('')
              try {
                setRequest(await api.inspectApp(code.trim().toUpperCase()))
              } catch (e) {
                setError(e instanceof Error ? e.message : 'Код не найден')
              } finally {
                setBusy(false)
              }
            }}
          >
            <label>
              Код приложения
              <input
                aria-label="Код приложения"
                autoComplete="off"
                value={code}
                maxLength={8}
                onChange={(e) => {
                  setCode(e.target.value.toUpperCase())
                  setRequest(null)
                }}
                placeholder="A1B2C3D4"
              />
            </label>
            <button className="button primary" disabled={busy || code.trim().length !== 8}>
              Проверить код
            </button>
          </form>
          {request && (
            <div className="permissions">
              <h3>
                <ShieldCheck size={17} />
                {request.name}
              </h3>
              <p>Приложение будет работать от имени вашего пространства.</p>
              {request.permissions.map((permission) => (
                <p key={permission}>✓ {permissions[permission] ?? permission}</p>
              ))}
              <p>✓ Использование выбранной модели, возможны расходы у провайдера</p>
              <button
                className="button primary"
                disabled={busy}
                onClick={async () => {
                  setBusy(true)
                  try {
                    await api.approveApp(code)
                    setRequest(null)
                    setCode('')
                    await refresh()
                    toast.success('Приложение подключено')
                  } catch (e) {
                    setError(e instanceof Error ? e.message : 'Привязка не удалась')
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                Разрешить подключение
              </button>
            </div>
          )}
          {error && (
            <p className="error-message" role="alert">
              {error}
            </p>
          )}
        </section>
        <section className="pairing-panel">
          <h2>Попробуйте отдельный таймер</h2>
          <p className="field-note">
            Это самостоятельное HTML-приложение с SDK. Оно показывает тот же таймер, что дашборд, и
            отправляет запросы вашей выбранной модели.
          </p>
          <a className="button primary" href={webPath('/api/web/downloads/timer.zip')}>
            <Download size={16} /> Скачать приложение + SDK
          </a>
          <a
            className="text-button preview-app-link"
            href={webPath('/api/web/sdk/timer.html')}
            target="_blank"
            rel="noopener noreferrer"
          >
            Открыть демонстрацию в отдельной вкладке ↗
          </a>
          <p className="field-note">
            Привязка действует до часа и отзывается здесь. Приложение получает доступ только к
            своему плагину и выбранной модели. После перезапуска сервера потребуется новая привязка.
          </p>
        </section>
      </div>
      <div className="section-heading">
        <h2>Подключённые приложения</h2>
        <span className="muted">Пространство · {snapshot?.subject_id.slice(0, 8)}</span>
      </div>
      <div className="connection-list">
        {apps.length === 0 ? (
          <p className="field-note">Пока нет подключённых приложений.</p>
        ) : (
          apps.map((app) => (
            <div className="connection-row" key={app.id}>
              <Link2 size={22} />
              <div>
                <h3>{snapshot?.manifests[app.plugin_id]?.name ?? app.plugin_id}</h3>
                <p>Доступ ещё {Math.max(1, Math.ceil(app.expires_in / 60))} мин</p>
              </div>
              <button
                className="icon-button"
                aria-label={`Отозвать доступ ${app.plugin_id}`}
                onClick={async () => {
                  try {
                    await api.revokeApp(app.id)
                    await refresh()
                  } catch (e) {
                    toast.error(e instanceof Error ? e.message : 'Ошибка')
                  }
                }}
              >
                <Trash2 size={17} />
              </button>
            </div>
          ))
        )}
      </div>
    </>
  )
}
