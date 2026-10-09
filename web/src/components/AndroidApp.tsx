import { useEffect, useState } from 'react'
import { ChevronRight, Download } from 'lucide-react'
import { api, type AndroidApp } from '../lib/api'
import { apiUrl } from '../lib/http'

// «Скачать для Android» (M62-T1): the server offers its APK at /downloads/mia.apk. When the
// server has none (local development, 404) nothing is shown.

function useAndroidApp(): AndroidApp | null {
  const [app, setApp] = useState<AndroidApp | null>(null)
  useEffect(() => {
    let alive = true
    api.androidApp().then(
      (info) => alive && setApp(info),
      () => undefined,
    )
    return () => {
      alive = false
    }
  }, [])
  return app
}

function details(app: AndroidApp): string {
  const size = `${Math.max(1, Math.round(app.size_bytes / 1_048_576))} МБ`
  return app.version_name ? `Версия ${app.version_name} · ${size}` : size
}

/** A row in «Ещё». */
export function AndroidAppRow() {
  const app = useAndroidApp()
  if (!app) return null
  return (
    <a
      href={apiUrl('/downloads/mia.apk')}
      download="mia.apk"
      className="mia-glass mw-data-row mw-more-link"
      data-testid="android-download"
    >
      <span className="mw-data-row__icon" aria-hidden="true">
        <Download size={20} />
      </span>
      <span className="mw-data-row__body">
        <strong>Скачать для Android</strong>
        <span className="mw-muted">
          Приложение Мии с голосом и чатом. {details(app)}. Разрешите установку из браузера.
        </span>
      </span>
      <ChevronRight size={18} aria-hidden="true" />
    </a>
  )
}

/** A quiet link under the sign-in button. */
export function AndroidAppLink() {
  const app = useAndroidApp()
  if (!app) return null
  return (
    <p className="mw-muted">
      <a href={apiUrl('/downloads/mia.apk')} download="mia.apk" data-testid="android-download">
        <Download size={14} aria-hidden="true" /> Скачать для Android
      </a>{' '}
      · {details(app)}
    </p>
  )
}
