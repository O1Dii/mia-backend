import { useEffect, useState } from 'react'
import { CheckCircle2, LoaderCircle, XCircle } from 'lucide-react'
import { api, type Connection } from '../lib/api'
import { errorText } from '../lib/errors'
import { Link } from '../lib/router'

/** Where the browser lands after the provider's consent: `/store/connected?connection=ok|error`. */
export function StoreConnected({ search }: { search: URLSearchParams }) {
  const ok = search.get('connection') === 'ok'
  const pluginId = search.get('plugin')
  const [connection, setConnection] = useState<Connection | null | undefined>(undefined)

  useEffect(() => {
    if (!ok) return
    let active = true
    api
      .connections()
      .then((list) => {
        if (active)
          setConnection(
            list.items.find(
              (c) => (pluginId ? c.plugin_id === pluginId : true) && c.status === 'connected',
            ) ?? null,
          )
      })
      .catch(() => active && setConnection(null))
    return () => {
      active = false
    }
  }, [ok, pluginId])

  return (
    <section className="mw-page" aria-labelledby="connected-title">
      <div className="mia-glass mw-result mia-rise">
        {ok ? (
          <CheckCircle2 size={40} className="mw-ok" aria-hidden="true" />
        ) : (
          <XCircle size={40} className="mw-bad" aria-hidden="true" />
        )}
        <h1 id="connected-title">{ok ? 'Аккаунт подключён' : 'Не удалось подключить аккаунт'}</h1>
        {ok ? (
          connection === undefined ? (
            <p className="mw-muted" role="status">
              <LoaderCircle size={16} className="spin" /> Проверяем подключение…
            </p>
          ) : (
            <p>
              {connection
                ? `Мия получила доступ к «${connection.account_label ?? connection.plugin_id}» только в рамках разрешений плагина.`
                : 'Подключение сохранено.'}
            </p>
          )
        ) : (
          <p role="alert">{errorText(search.get('code') ?? 'connection_oauth_failed')}</p>
        )}
        <div className="mw-actions">
          {ok && (
            <Link to="/chat" className="mia-btn mia-btn--primary">
              Попробовать в чате
            </Link>
          )}
          {pluginId && (
            <Link
              to={`/store/plugin/${encodeURIComponent(pluginId)}`}
              className="mia-btn mia-btn--secondary"
            >
              {ok ? 'К плагину' : 'Попробовать снова'}
            </Link>
          )}
          <Link to="/installed" className="mia-btn mia-btn--ghost">
            Мои плагины
          </Link>
        </div>
      </div>
    </section>
  )
}
