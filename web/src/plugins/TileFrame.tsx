import { useCallback, useEffect, useRef, useState } from 'react'
import { LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react'
import { api, type TileDocument } from '../lib/api'
import { ApiError, messageOf } from '../lib/http'
import { Link } from '../lib/router'
import { AppBridge, type CallToolResult, type HostContext } from './AppBridge'

function hostContext(): HostContext {
  return {
    theme: window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    locale: navigator.language || 'ru',
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    platform: 'web',
    displayMode: 'inline',
  }
}

function asResult(value: {
  content: string
  structured_content: Record<string, unknown> | null
  is_error: boolean
}): CallToolResult {
  return {
    content: [{ type: 'text', text: value.content }],
    ...(value.structured_content ? { structuredContent: value.structured_content } : {}),
    ...(value.is_error ? { isError: true } : {}),
  }
}

const problemHint: Record<string, { text: string; to: string; label: string }> = {
  connection_required: {
    text: 'Плагину нужен подключённый аккаунт.',
    to: '/installed',
    label: 'Подключить',
  },
  tool_consent_required: {
    text: 'Плагин изменил инструменты: нужно новое согласие.',
    to: '/installed',
    label: 'Проверить',
  },
  tool_not_active: { text: 'Плагин отключён.', to: '/installed', label: 'Мои плагины' },
}

/**
 * One dashboard tile: the plugin's `ui://` document in a sandboxed iframe and its host bridge.
 * Data refreshes by calling the manifest's refresh tool through the backend, never through a model.
 */
export function TileFrame({
  installationId,
  tileId,
  title,
  onLoaded,
  initial,
  autoRefresh = true,
}: {
  installationId: string
  tileId: string
  title: string
  onLoaded?: (doc: TileDocument) => void
  /** A tool result to hand to the app first (an `app` card in the chat carries one). */
  initial?: Record<string, unknown>
  /** Call the manifest's refresh tool when the app is ready and then on its schedule. */
  autoRefresh?: boolean
}) {
  const [doc, setDoc] = useState<TileDocument | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [tampered, setTampered] = useState(false)
  const [busy, setBusy] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const frame = useRef<HTMLIFrameElement>(null)
  const bridge = useRef<AppBridge | null>(null)

  useEffect(() => {
    let alive = true
    setError(null)
    setDoc(null)
    api
      .tileDocument(installationId, tileId)
      .then((value) => {
        if (!alive) return
        setDoc(value)
        onLoaded?.(value)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof ApiError ? e : new ApiError(0, 'network_error', true))
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [installationId, tileId, attempt])

  const call = useCallback(
    async (name: string, args: Record<string, unknown>): Promise<CallToolResult> => {
      try {
        return asResult(await api.tileCall(installationId, tileId, name, args))
      } catch (e) {
        return { content: [{ type: 'text', text: messageOf(e) }], isError: true }
      }
    },
    [installationId, tileId],
  )

  const refresh = useCallback(async () => {
    const rule = doc?.refresh
    const current = bridge.current
    if (!rule || !current?.ready) return
    setBusy(true)
    try {
      current.notifyToolResult(await call(rule.tool, {}))
    } finally {
      setBusy(false)
    }
  }, [doc, call])

  // The bridge lives as long as the document; the iframe is keyed by the document text.
  useEffect(() => {
    const element = frame.current
    if (!doc || !element) return
    const created = new AppBridge(
      element,
      {
        callTool: call,
        openLink: (url) => window.open(url, '_blank', 'noopener,noreferrer'),
        onInitialized: () => void refreshSoon(),
        onTampered: () => setTampered(true),
      },
      hostContext(),
    )
    bridge.current = created
    const refreshSoon = async () => {
      if (initial)
        created.notifyToolResult({
          content: [{ type: 'text', text: '' }],
          structuredContent: initial,
        })
      if (autoRefresh && doc.refresh) created.notifyToolResult(await call(doc.refresh.tool, {}))
    }
    return () => {
      created.dispose()
      bridge.current = null
    }
  }, [doc, call])

  // Periodic refresh while the tab is visible.
  useEffect(() => {
    if (!doc?.refresh || !autoRefresh) return
    const every = Math.max(30, doc.refresh.every_seconds) * 1000
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, every)
    return () => window.clearInterval(timer)
  }, [doc, refresh, autoRefresh])

  if (error) {
    const hint = problemHint[error.code]
    return (
      <div className="mw-tile__state" role="alert">
        <TriangleAlert size={20} aria-hidden="true" />
        <p>{hint ? hint.text : error.message}</p>
        {hint ? (
          <Link to={hint.to} className="mia-btn mia-btn--secondary">
            {hint.label}
          </Link>
        ) : (
          <button className="mia-btn mia-btn--secondary" onClick={() => setAttempt((n) => n + 1)}>
            Повторить
          </button>
        )}
      </div>
    )
  }
  if (tampered)
    return (
      <div className="mw-tile__state" role="alert">
        <TriangleAlert size={20} aria-hidden="true" />
        <p>Плитка попыталась уйти со своей страницы и остановлена.</p>
        <button
          className="mia-btn mia-btn--secondary"
          onClick={() => {
            setTampered(false)
            setAttempt((n) => n + 1)
          }}
        >
          Загрузить заново
        </button>
      </div>
    )
  if (!doc)
    return (
      <div className="mw-tile__state" aria-busy="true">
        <LoaderCircle size={20} className="spin" aria-hidden="true" />
        <p className="mw-muted">Загружаю плитку…</p>
      </div>
    )
  return (
    <>
      {doc.refresh && (
        <button
          className="mia-icon-btn mw-tile__refresh"
          aria-label={`Обновить плитку ${title}`}
          disabled={busy}
          onClick={() => void refresh()}
        >
          <RefreshCw size={16} className={busy ? 'spin' : ''} />
        </button>
      )}
      <iframe
        key={doc.html.length + doc.resource_uri}
        ref={frame}
        className="mw-tile__frame"
        title={`Плитка: ${title}`}
        data-testid={`tile-frame-${tileId}`}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        allow=""
        srcDoc={doc.html}
      />
    </>
  )
}
