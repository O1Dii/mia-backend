import { useEffect, useState } from 'react'
import { api, type Display } from '../lib/api'
import { Markdown } from '../lib/markdown'
import { TileFrame } from './TileFrame'

type AppDisplay = Extract<Display, { kind: 'app' }>
type Found = { installationId: string; tileId: string; title: string }

/**
 * An assistant card of kind `app` (MCP Apps in the conversation): the same sandboxed host as a
 * dashboard tile, started with the result the model saw. Without a matching installed, active
 * plugin tile the card falls back to the text the assistant sent along.
 */
export function InlineApp({ display }: { display: AppDisplay }) {
  const [found, setFound] = useState<Found | null | undefined>(undefined)

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const installations = await api.installations()
        const installation = installations.items.find(
          (i) => i.plugin_id === display.plugin_id && i.status === 'active',
        )
        const detail = installation ? await api.plugin(display.plugin_id) : null
        const tile = detail?.manifest.surfaces?.tiles?.find(
          (t) => t.resourceUri === display.resource_uri,
        )
        if (alive)
          setFound(
            installation && tile
              ? {
                  installationId: installation.id,
                  tileId: tile.id,
                  title: tile.title ?? detail!.name,
                }
              : null,
          )
      } catch {
        if (alive) setFound(null)
      }
    })()
    return () => {
      alive = false
    }
  }, [display.plugin_id, display.resource_uri])

  if (found)
    return (
      <article className="mia-card mia-glass mw-display mw-inline-app" data-app={display.plugin_id}>
        <div className="mw-inline-app__body">
          <TileFrame
            installationId={found.installationId}
            tileId={found.tileId}
            title={found.title}
            initial={display.structured_content}
            autoRefresh={false}
          />
        </div>
      </article>
    )
  return (
    <article className="mia-card mia-glass mw-display">
      <span className="mia-card__eyebrow">Мини-приложение</span>
      <div className="mia-card__body">
        <Markdown text={display.fallback_markdown ?? `Приложение ${display.plugin_id}`} />
      </div>
    </article>
  )
}
