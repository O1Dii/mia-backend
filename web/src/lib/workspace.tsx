import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { toast } from 'sonner'
import { api, type WorkspaceSnapshot } from './legacyApi'
import { useAuth } from './auth'
import { catalog, recipeManifest, recipeSchema } from '../plugins/catalog'

type RuntimeContext = {
  snapshot: WorkspaceSnapshot | null
  error: string
  ready: boolean
  refresh: () => Promise<void>
  accept: (snapshot: WorkspaceSnapshot) => void
  act: (name: string, args?: Record<string, unknown>) => Promise<Record<string, unknown>>
}
const Context = createContext<RuntimeContext | null>(null)

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const auth = useAuth()
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null)
  const [error, setError] = useState('')
  const initial = useRef<Promise<void> | null>(null)
  const latest = useRef<WorkspaceSnapshot | null>(null)
  const accept = useCallback((next: WorkspaceSnapshot) => {
    if (latest.current?.subject_id === next.subject_id && latest.current.revision > next.revision)
      return
    latest.current = next
    setSnapshot(next)
    setError('')
  }, [])
  const refresh = useCallback(async () => {
    try {
      accept(await api.workspace())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Runtime недоступен')
    }
  }, [accept])
  useEffect(() => {
    if (!initial.current)
      initial.current = (async () => {
        try {
          let next = await api.workspace()
          // One-time import of old installations; author content is revalidated on the server.
          const migration = `mia.runtime-migrated.${next.subject_id}`
          if (!auth.user && !localStorage.getItem(migration) && next.installed.length === 0) {
            const old = JSON.parse(localStorage.getItem('mia.installed.v1') || '[]') as unknown
            const recipes = recipeSchema
              .array()
              .safeParse(JSON.parse(localStorage.getItem('mia.recipes.v1') || '[]'))
            if (Array.isArray(old))
              for (const id of old.slice(0, 50)) {
                if (typeof id !== 'string') continue
                const recipe = recipes.success ? recipes.data.find((r) => r.id === id) : undefined
                if (catalog.some((p) => p.id === id) || recipe)
                  next = await api.install(id, recipe ? recipeManifest(recipe) : undefined)
              }
            localStorage.setItem(migration, '1')
          }
          accept(next)
        } catch (e) {
          setError(e instanceof Error ? e.message : 'Runtime недоступен')
        }
      })()
    const interval = setInterval(() => {
      if (!document.hidden) void refresh()
    }, 1500)
    const onFocus = () => void refresh()
    window.addEventListener('focus', onFocus)
    return () => {
      clearInterval(interval)
      window.removeEventListener('focus', onFocus)
    }
  }, [accept, refresh])
  const act = useCallback(
    async (name: string, args: Record<string, unknown> = {}) => {
      const result = await api.action(name, args)
      accept(result.workspace)
      if (!result.ok) {
        const message = result.error || 'Действие не выполнено'
        toast.error(message)
        throw new Error(message)
      }
      return result.data || {}
    },
    [accept],
  )
  return (
    <Context.Provider value={{ snapshot, error, ready: Boolean(snapshot), accept, refresh, act }}>
      {children}
    </Context.Provider>
  )
}

export function useWorkspace() {
  const value = useContext(Context)
  if (!value) throw new Error('WorkspaceProvider required')
  return value
}
