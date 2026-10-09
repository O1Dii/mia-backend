import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { api, type Approval, type Display, type FeedItem, type Risk, type Run } from './api'
import { ApiError, messageOf, newKey } from './http'
import { errorText } from './errors'
import { plainText } from './markdown'
import { followRun, type RunEvent } from './runs'
import { useVoicePrefs, type VoicePrefs } from './prefs'
import { useVoice } from './voice'

export type ToolStep = {
  id: string
  tool: string
  risk: Risk
  status: 'running' | 'succeeded' | 'failed' | 'denied' | 'expired' | 'uncertain' | 'cancelled'
}
export type ChatMessage = {
  key: string
  role: 'user' | 'assistant'
  text: string
  display: Display | null
  speech?: string
  followups: string[]
  runId: string | null
  pending: boolean
  error?: string
  steps: ToolStep[]
  approvals: Approval[]
  notes: string[]
  dictated: boolean
  createdAt?: string
}

const blank = (patch: Partial<ChatMessage> & Pick<ChatMessage, 'key' | 'role'>): ChatMessage => ({
  text: '',
  display: null,
  followups: [],
  runId: null,
  pending: false,
  steps: [],
  approvals: [],
  notes: [],
  dictated: false,
  ...patch,
})

export function fromFeed(item: FeedItem): ChatMessage {
  return blank({
    key: item.id,
    role: item.role,
    text: item.text,
    display: item.role === 'assistant' ? item.display : null,
    runId: item.run_id,
    createdAt: item.created_at,
  })
}

export function textOfDisplay(display: Display | null, fallback: string): string {
  if (!display) return fallback
  if (display.kind === 'text' || display.kind === 'card') return display.markdown
  if (display.kind === 'app') return display.fallback_markdown ?? fallback
  return fallback
}

/** One server event folded into the assistant message of its run. */
export function applyEvent(message: ChatMessage, event: RunEvent): ChatMessage {
  const payload = event.payload
  switch (event.type) {
    case 'message.delta':
      return message.display
        ? message
        : { ...message, text: message.text + String(payload.text ?? '') }
    case 'tool.call.started':
      return {
        ...message,
        steps: [
          ...message.steps.filter((s) => s.id !== payload.tool_call_id),
          { id: payload.tool_call_id, tool: payload.tool, risk: payload.risk, status: 'running' },
        ],
      }
    case 'tool.call.completed':
      return {
        ...message,
        steps: message.steps.map((s) =>
          s.id === payload.tool_call_id ? { ...s, status: payload.status } : s,
        ),
      }
    case 'approval.requested': {
      const approval = payload.approval as Approval
      return {
        ...message,
        approvals: [...message.approvals.filter((a) => a.id !== approval.id), approval],
      }
    }
    case 'approval.resolved':
      return {
        ...message,
        approvals: message.approvals.map((a) =>
          a.id === payload.approval_id ? { ...a, status: payload.status } : a,
        ),
      }
    case 'memory.suggested':
      return { ...message, notes: [...message.notes, `Предлагаю запомнить: ${payload.text}`] }
    case 'job.created':
      return { ...message, notes: [...message.notes, `Фоновая задача: ${payload.goal}`] }
    case 'response.final': {
      const response = payload.response as {
        speech: string
        display: Display | null
        followups: string[]
      }
      return {
        ...message,
        text: textOfDisplay(response.display, response.speech),
        display: response.display,
        speech: response.speech,
        followups: response.followups ?? [],
        pending: false,
        error: undefined,
      }
    }
    case 'run.failed':
      return {
        ...message,
        pending: false,
        error: errorText(payload.error?.code, 'Не получилось ответить.'),
      }
    case 'run.cancelled':
      return { ...message, pending: false, error: message.text ? undefined : 'Остановлено.' }
    case 'run.completed':
      return { ...message, pending: false }
    default:
      return message
  }
}

function finalizeFromRun(message: ChatMessage, run: Run): ChatMessage {
  if (run.response)
    return {
      ...message,
      text: textOfDisplay(run.response.display, run.response.speech),
      display: run.response.display,
      speech: run.response.speech,
      followups: run.response.followups,
      pending: false,
    }
  if (run.status === 'failed')
    return {
      ...message,
      pending: false,
      error: errorText(run.error?.code, 'Не получилось ответить.'),
    }
  if (run.status === 'cancelled') return { ...message, pending: false, error: 'Остановлено.' }
  return { ...message, pending: false, error: 'Связь потеряна. Ответ появится в истории.' }
}

export function speechOf(message: ChatMessage): string {
  return message.speech?.trim() || plainText(message.text)
}

type Chat = {
  messages: ChatMessage[]
  loading: boolean
  loadError: string
  hasMore: boolean
  loadingMore: boolean
  busy: boolean
  prefs: VoicePrefs
  setPrefs: (patch: Partial<VoicePrefs>) => void
  voice: ReturnType<typeof useVoice>
  send: (text: string, options?: { dictated?: boolean }) => Promise<void>
  stop: () => void
  loadMore: () => Promise<void>
  reload: () => Promise<void>
  decide: (approval: Approval, decision: 'approve' | 'deny') => Promise<void>
  pendingApprovals: number
  setPendingApprovals: (count: number) => void
}

const Context = createContext<Chat | null>(null)
export function useChat(): Chat {
  const value = useContext(Context)
  if (!value) throw new Error('ChatProvider required')
  return value
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [cursor, setCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [pendingApprovals, setPendingApprovals] = useState(0)
  const [prefs, setPrefs] = useVoicePrefs()
  const voice = useVoice()
  const prefsRef = useRef(prefs)
  prefsRef.current = prefs
  const speakRef = useRef(voice.speak)
  speakRef.current = voice.speak
  const canSpeak = voice.canSpeak
  const controllers = useRef(new Map<string, AbortController>())
  const loaded = useRef(false)

  const patch = useCallback((key: string, change: (m: ChatMessage) => ChatMessage) => {
    setMessages((all) => all.map((m) => (m.key === key ? change(m) : m)))
  }, [])

  /** Follow one run into the assistant message `botKey`; speaks the answer when the rules say so. */
  const follow = useCallback(
    async (botKey: string, runId: string, dictated: boolean) => {
      const controller = new AbortController()
      controllers.current.set(botKey, controller)
      try {
        const result = await followRun(
          runId,
          (event) => {
            patch(botKey, (m) => applyEvent(m, event))
            if (
              event.type === 'response.final' &&
              canSpeak &&
              (dictated || prefsRef.current.alwaysSpeak)
            ) {
              const speech = String(event.payload.response?.speech ?? '')
              if (speech) speakRef.current(speech, botKey)
            }
          },
          controller.signal,
        )
        if (result === 'lost') {
          const run = await api.getRun(runId).catch(() => null)
          patch(botKey, (m) =>
            run
              ? finalizeFromRun(m, run)
              : { ...m, pending: false, error: errorText('network_error') },
          )
        }
      } finally {
        controllers.current.delete(botKey)
      }
    },
    [canSpeak, patch],
  )

  /** After a reload: runs still waiting for an approval or still answering are picked up again. */
  const resume = useCallback(
    async (newest: FeedItem | undefined) => {
      const waiting = await api.approvals('pending').catch(() => ({ items: [] as Approval[] }))
      const runIds = new Set(waiting.items.map((a) => a.run_id))
      if (newest?.role === 'user' && newest.run_id) runIds.add(newest.run_id)
      for (const runId of runIds) {
        const run = await api.getRun(runId).catch(() => null)
        if (!run) continue
        const key = crypto.randomUUID()
        const active = ['running', 'waiting_approval', 'cancel_requested'].includes(run.status)
        const bot = blank({ key, role: 'assistant', runId, pending: active })
        if (active) {
          setMessages((all) =>
            all.some((m) => m.runId === runId && m.role === 'assistant') ? all : [...all, bot],
          )
          void follow(key, runId, false)
        } else if (run.response || run.status === 'failed') {
          setMessages((all) =>
            all.some((m) => m.runId === runId && m.role === 'assistant')
              ? all
              : [...all, finalizeFromRun(bot, run)],
          )
        }
      }
    },
    [follow],
  )

  const reload = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const page = await api.feed(undefined, 30)
      setMessages(page.items.slice().reverse().map(fromFeed))
      setCursor(page.next_cursor)
      setLoading(false)
      await resume(page.items[0])
    } catch (error) {
      setLoadError(messageOf(error, 'Не удалось загрузить историю.'))
    } finally {
      setLoading(false)
    }
  }, [resume])

  useEffect(() => {
    if (loaded.current) return
    loaded.current = true
    void reload()
  }, [reload])

  useEffect(() => {
    const running = controllers.current
    return () => {
      for (const controller of running.values()) controller.abort()
    }
  }, [])

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return
    setLoadingMore(true)
    try {
      const page = await api.feed(cursor, 30)
      setMessages((all) => {
        const known = new Set(all.map((m) => m.key))
        return [
          ...page.items
            .slice()
            .reverse()
            .map(fromFeed)
            .filter((m) => !known.has(m.key)),
          ...all,
        ]
      })
      setCursor(page.next_cursor)
    } catch (error) {
      if (error instanceof ApiError && error.code === 'invalid_feed_cursor') await reload()
      else setLoadError(messageOf(error, 'Не удалось загрузить историю.'))
    } finally {
      setLoadingMore(false)
    }
  }, [cursor, loadingMore, reload])

  const send = useCallback(
    async (text: string, options: { dictated?: boolean } = {}) => {
      const clean = text.trim()
      if (!clean) return
      const userKey = crypto.randomUUID()
      const botKey = crypto.randomUUID()
      const dictated = options.dictated === true
      setMessages((all) => [
        ...all,
        blank({ key: userKey, role: 'user', text: clean, dictated }),
        blank({ key: botKey, role: 'assistant', pending: true, dictated }),
      ])
      try {
        const run = await api.createRun(clean, newKey())
        patch(botKey, (m) => ({ ...m, runId: run.id }))
        await follow(botKey, run.id, dictated)
      } catch (error) {
        patch(botKey, (m) => ({
          ...m,
          pending: false,
          error: messageOf(error, 'Не удалось отправить.'),
        }))
      }
    },
    [follow, patch],
  )

  const stop = useCallback(() => {
    for (const message of messages)
      if (message.pending && message.runId) void api.cancelRun(message.runId).catch(() => undefined)
  }, [messages])

  const decide = useCallback(async (approval: Approval, decision: 'approve' | 'deny') => {
    const updated = await api.decide(approval, decision)
    setMessages((all) =>
      all.map((m) =>
        m.runId === approval.run_id
          ? { ...m, approvals: m.approvals.map((a) => (a.id === updated.id ? updated : a)) }
          : m,
      ),
    )
  }, [])

  const busy = messages.some((m) => m.pending)
  const value = useMemo<Chat>(
    () => ({
      messages,
      loading,
      loadError,
      hasMore: cursor !== null,
      loadingMore,
      busy,
      prefs,
      setPrefs,
      voice,
      send,
      stop,
      loadMore,
      reload,
      decide,
      pendingApprovals,
      setPendingApprovals,
    }),
    [
      messages,
      loading,
      loadError,
      cursor,
      loadingMore,
      busy,
      prefs,
      setPrefs,
      voice,
      send,
      stop,
      loadMore,
      reload,
      decide,
      pendingApprovals,
    ],
  )
  return <Context.Provider value={value}>{children}</Context.Provider>
}
