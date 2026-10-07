import { useEffect, useRef } from 'react'
import { api } from '../lib/api'
import { useWorkspace } from '../lib/workspace'
import type { Plugin } from './catalog'
import { toolName } from './Tools'
import sdkSource from '../../../packages/mia-sdk/mia-sdk.mjs?raw'

// The opaque sandbox has no account credentials. Every RPC is scoped by this host.
export function PluginFrame({ plugin }: { plugin: Plugin }) {
  const frame = useRef<HTMLIFrameElement>(null)
  const { snapshot, refresh, act } = useWorkspace()
  const latest = useRef({ snapshot, refresh, act })
  latest.current = { snapshot, refresh, act }
  useEffect(() => {
    let inFlight = false
    const onMessage = async (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.data?.channel !== 'mia-sdk:v1')
        return
      const { id, method, params = {} } = event.data
      if (typeof id !== 'string' || id.length > 80 || inFlight) return
      const source = event.source as Window
      const installed = latest.current.snapshot?.manifests[plugin.id]
      if (!installed) return
      inFlight = true
      try {
        let result: unknown
        if (method === 'state.get') result = latest.current.snapshot?.states[plugin.id] ?? {}
        else if (method === 'actions.invoke') {
          const action = installed.actions?.find((a) => a.name === params.name)
          if (!action) throw new Error('Действие не объявлено в manifest')
          result = await latest.current.act(
            await toolName(plugin.id, action.name),
            params.arguments ?? {},
          )
        } else if (method === 'ai.generate' && installed.permissions.includes('ai.generate')) {
          if (typeof params.text !== 'string' || params.text.length > 16000)
            throw new Error('Некорректный запрос')
          const connectionId = latest.current.snapshot?.active_connection_id
          if (!connectionId) throw new Error('Сначала выберите модель в Мии')
          result = await api.chat(connectionId, [{ role: 'user', content: params.text }], {
            plugin_id: plugin.id,
          })
          await latest.current.refresh()
        } else throw new Error('Метод SDK не разрешён')
        source.postMessage({ channel: 'mia-sdk:v1', id, result }, '*')
      } catch (e) {
        source.postMessage(
          { channel: 'mia-sdk:v1', id, error: e instanceof Error ? e.message : 'Ошибка SDK' },
          '*',
        )
      } finally {
        inFlight = false
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [plugin.id])
  useEffect(() => {
    frame.current?.contentWindow?.postMessage(
      { channel: 'mia-sdk:v1', event: 'state.changed', state: snapshot?.states[plugin.id] ?? {} },
      '*',
    )
  }, [snapshot?.revision, plugin.id, snapshot?.states])
  const csp =
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'"
  const bridge =
    sdkSource.slice(sdkSource.indexOf('export class MiaView')).replace('export class ', 'class ') +
    ';window.mia = new MiaView();'
  const document = `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="viewport" content="width=device-width, initial-scale=1"><style>body{font:14px system-ui;background:#181d16;color:#d9e8c6;margin:18px}button{padding:10px;border-radius:8px;border:0;background:#d6f882;color:#223018}input,textarea{font:inherit;width:90%;padding:10px;background:#252e1e;color:#eaf4dc;border:1px solid #5a6d44;border-radius:8px}p{line-height:1.6}</style></head><body><script>${bridge}</script>${plugin.manifest?.view.html ?? ''}</body></html>`
  return (
    <iframe
      ref={frame}
      title={`${plugin.name} — виджет автора`}
      className="plugin-frame"
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      srcDoc={document}
    />
  )
}
