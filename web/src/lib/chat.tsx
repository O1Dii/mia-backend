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
import {
  api,
  type Approval,
  type Chat as ChatInfo,
  type Display,
  type FeedCard,
  type FeedItem,
  type Risk,
  type Run,
  type ScreenAction,
} from './api'
import { ApiError, messageOf, newKey } from './http'
import { errorText } from './errors'
import { plainText } from './markdown'
import { navigate, usePlace } from './router'
import { followRun, type RunEvent } from './runs'
import { useVoicePrefs, type VoicePrefs } from './prefs'
import { useVoice } from './voice'

export type ToolStep = {
  id: string
  tool: string
  risk: Risk
  status: 'running' | 'succeeded' | 'failed' | 'denied' | 'expired' | 'uncertain' | 'cancelled'
  /** Redacted arguments of the call (tool.call.started, M73): shown folded in the journal. */
  args?: Record<string, unknown>
  /** Why the call did not succeed, in the tool's words (tool.call.completed.error_detail, M73). */
  reason?: string
  /** Fallback reason: the public error code in the user's words. */
  codeText?: string
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
  /** Screen-only actions of the run's tools (M41-R1): buttons the model never sees. */
  actions: ScreenAction[]
  notes: string[]
  dictated: boolean
  createdAt?: string
  /** Result card of a background job started in this chat (M74). */
  card?: FeedCard
}

const blank = (patch: Partial<ChatMessage> & Pick<ChatMessage, 'key' | 'role'>): ChatMessage => ({
  text: '',
  display: null,
  followups: [],
  runId: null,
  pending: false,
  steps: [],
  approvals: [],
  actions: [],
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
    actions: item.role === 'assistant' ? (item.actions ?? []) : [],
    card: item.role === 'assistant' ? item.card : undefined,
  })
}

function withAction(actions: ScreenAction[], action: ScreenAction | undefined): ScreenAction[] {
  if (!action || actions.some((a) => a.url === action.url && a.label === action.label))
    return actions
  return [...actions, action]
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
          {
            id: payload.tool_call_id,
            tool: payload.tool,
            risk: payload.risk,
            status: 'running',
            args: payload.arguments,
          },
        ],
      }
    case 'tool.call.completed':
      return {
        ...message,
        steps: message.steps.map((s) =>
          s.id === payload.tool_call_id
            ? {
                ...s,
                status: payload.status,
                reason: payload.error_detail,
                codeText:
                  payload.status !== 'succeeded' && payload.error?.code
                    ? errorText(payload.error.code)
                    : undefined,
              }
            : s,
        ),
        actions: withAction(message.actions, payload.action as ScreenAction | undefined),
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

/** Which chat the address names: `/chat` (the latest), `/chat/new` (a draft), `/chat/<id>`. */
export type ChatTarget = { kind: 'default' } | { kind: 'new' } | { kind: 'chat'; id: string }
export function chatTarget(path: string): ChatTarget | null {
  if (path === '/chat' || path === '/chat/') return { kind: 'default' }
  if (!path.startsWith('/chat/')) return null
  const rest = decodeURIComponent(path.slice('/chat/'.length)).replace(/\/$/, '')
  return rest === 'new' ? { kind: 'new' } : { kind: 'chat', id: rest }
}
export const chatPath = (id: string | null, draft = false): string =>
  draft ? '/chat/new' : id ? `/chat/${encodeURIComponent(id)}` : '/chat'

type ChatState = {
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
  // ---- chats (M75, ADR-0017)
  chats: ChatInfo[]
  chatsLoaded: boolean
  chatsError: string
  hasMoreChats: boolean
  /** The chat on screen; null for a draft that is created with its first message. */
  activeId: string | null
  draft: boolean
  refreshChats: () => Promise<void>
  /** Append what arrived in the open chat meanwhile (job result cards). */
  syncTail: () => Promise<void>
  loadMoreChats: () => Promise<void>
  newChat: () => void
  selectChat: (id: string) => void
  renameChat: (id: string, title: string | null) => Promise<void>
  pinChat: (id: string, pinned: boolean) => Promise<void>
  removeChat: (id: string) => Promise<void>
  /** Bumped whenever a run ended: «Сейчас» reloads (a timer may have been set). */
  activity: number
  /** Bumped when the user picks, starts or deletes a chat: the composer starts afresh. */
  switched: number
}

const Context = createContext<ChatState | null>(null)
export function useChat(): ChatState {
  const value = useContext(Context)
  if (!value) throw new Error('ChatProvider required')
  return value
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const place = usePlace()
  const target = chatTarget(place.path)
  const targetKey = target ? (target.kind === 'chat' ? target.id : target.kind) : null

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [cursor, setCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [pendingApprovals, setPendingApprovals] = useState(0)
  const [chats, setChats] = useState<ChatInfo[]>([])
  const [chatsNext, setChatsNext] = useState<string | null>(null)
  const [chatsLoaded, setChatsLoaded] = useState(false)
  const [chatsError, setChatsError] = useState('')
  const [activeId, setActiveId] = useState<string | null>(null)
  const [draft, setDraft] = useState(false)
  const [activity, setActivity] = useState(0)
  const [switched, setSwitched] = useState(0)
  const [prefs, setPrefs] = useVoicePrefs()
  const voice = useVoice()
  const prefsRef = useRef(prefs)
  prefsRef.current = prefs
  const speakRef = useRef(voice.speak)
  speakRef.current = voice.speak
  const canSpeak = voice.canSpeak
  const controllers = useRef(new Map<string, AbortController>())
  /** The chat (or 'default' / 'new') whose messages are in `messages`. */
  const loadedKey = useRef<string | null>(null)
  const activeRef = useRef<string | null>(null)
  const seq = useRef(0)
  /** Resolves once the open chat's history is on screen: a message sent earlier would be wiped. */
  const loadGate = useRef<Promise<void>>(Promise.resolve())

  const patch = useCallback((key: string, change: (m: ChatMessage) => ChatMessage) => {
    setMessages((all) => all.map((m) => (m.key === key ? change(m) : m)))
  }, [])

  const refreshChats = useCallback(async () => {
    try {
      const page = await api.chats(undefined, 100)
      setChats(page.items)
      setChatsNext(page.next_cursor)
      setChatsError('')
    } catch (error) {
      setChatsError(messageOf(error, 'Не удалось загрузить список чатов.'))
    } finally {
      setChatsLoaded(true)
    }
  }, [])

  const loadMoreChats = useCallback(async () => {
    if (!chatsNext) return
    try {
      const page = await api.chats(chatsNext, 100)
      setChats((all) => {
        const known = new Set(all.map((c) => c.id))
        return [...all, ...page.items.filter((c) => !known.has(c.id))]
      })
      setChatsNext(page.next_cursor)
    } catch (error) {
      setChatsError(messageOf(error, 'Не удалось загрузить список чатов.'))
    }
  }, [chatsNext])

  const abortAll = useCallback(() => {
    for (const controller of controllers.current.values()) controller.abort()
    controllers.current.clear()
  }, [])

  /** The titles come a moment after the answer: refresh now and once more shortly. */
  const afterRun = useCallback(() => {
    void refreshChats()
    setActivity((n) => n + 1)
    window.setTimeout(() => void refreshChats(), 3000)
  }, [refreshChats])

  /**
   * After a lost stream the answer comes from GET /v1/runs/{id}, which knows no screen actions:
   * the chat's feed has them (OI-M41-R1-002), so it is read again for that run's entry.
   */
  const rereadFeed = useCallback(
    async (botKey: string, runId: string, chatId: string | null) => {
      for (let attempt = 0; attempt < 4; attempt++) {
        const page = await api.feed(undefined, 10, chatId ?? undefined).catch(() => null)
        const entry = page?.items.find((i) => i.role === 'assistant' && i.run_id === runId)
        if (entry) {
          patch(botKey, (m) => ({
            ...m,
            actions: (entry.actions ?? []).reduce(
              (list, action) => withAction(list, action),
              m.actions,
            ),
            ...(m.text ? {} : { text: entry.text, display: entry.display, error: undefined }),
          }))
          return
        }
        await new Promise((resolve) => window.setTimeout(resolve, 600))
      }
    },
    [patch],
  )

  /** Follow one run into the assistant message `botKey`; speaks the answer when the rules say so. */
  const follow = useCallback(
    async (botKey: string, runId: string, dictated: boolean, chatId: string | null) => {
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
          await rereadFeed(botKey, runId, chatId)
        }
        if (result !== 'aborted') afterRun()
      } finally {
        controllers.current.delete(botKey)
      }
    },
    [afterRun, canSpeak, patch, rereadFeed],
  )

  /** After a reload: runs of this chat still waiting for an approval or answering are picked up. */
  const resume = useCallback(
    async (newest: FeedItem | undefined, chatId: string | null, token: number) => {
      const waiting = await api.approvals('pending').catch(() => ({ items: [] as Approval[] }))
      const runIds = new Set(waiting.items.map((a) => a.run_id))
      if (newest?.role === 'user' && newest.run_id) runIds.add(newest.run_id)
      for (const runId of runIds) {
        const run = await api.getRun(runId).catch(() => null)
        if (!run || token !== seq.current) continue
        // Approvals are the account's, the chat shows only its own runs.
        if (chatId && run.conversation_id && run.conversation_id !== chatId) continue
        if (chatId && !run.conversation_id && runId !== newest?.run_id) continue
        const key = crypto.randomUUID()
        const active = ['running', 'waiting_approval', 'cancel_requested'].includes(run.status)
        const bot = blank({ key, role: 'assistant', runId, pending: active })
        if (active) {
          setMessages((all) =>
            all.some((m) => m.runId === runId && m.role === 'assistant') ? all : [...all, bot],
          )
          void follow(key, runId, false, chatId)
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

  /** Load one chat's feed; `id` undefined = the default chat (the address then names it). */
  const loadChat = useCallback(
    async (id: string | undefined) => {
      const token = ++seq.current
      abortAll()
      let release: () => void = () => undefined
      loadGate.current = new Promise<void>((resolve) => (release = resolve))
      setLoading(true)
      setLoadError('')
      try {
        const page = await api.feed(undefined, 30, id)
        if (token !== seq.current) return
        const resolved = id ?? page.conversation_id ?? null
        activeRef.current = resolved
        loadedKey.current = resolved ?? 'default'
        setActiveId(resolved)
        setDraft(false)
        setMessages(page.items.slice().reverse().map(fromFeed))
        setCursor(page.next_cursor)
        setLoading(false)
        release()
        if (!id && resolved) navigate(chatPath(resolved), true)
        await resume(page.items[0], resolved, token)
      } catch (error) {
        if (token !== seq.current) return
        if (error instanceof ApiError && error.code === 'conversation_not_found' && id) {
          // The chat was deleted elsewhere: fall back to the latest one.
          activeRef.current = null
          loadedKey.current = null
          void refreshChats()
          navigate('/chat', true)
          return
        }
        setLoadError(messageOf(error, 'Не удалось загрузить историю.'))
      } finally {
        if (token === seq.current) setLoading(false)
        release()
      }
    },
    [abortAll, refreshChats, resume],
  )

  const reload = useCallback(async () => {
    await loadChat(activeRef.current ?? undefined)
  }, [loadChat])

  /** Entries that arrived without this page (a job's result card, the Telegram dialog). */
  const syncTail = useCallback(async () => {
    const chatId = activeRef.current
    if (!chatId || loadedKey.current !== chatId) return
    const page = await api.feed(undefined, 30, chatId).catch(() => null)
    if (!page || activeRef.current !== chatId) return
    setMessages((all) => {
      const keys = new Set(all.map((m) => m.key))
      const runs = new Set(all.map((m) => m.runId).filter(Boolean))
      const fresh = page.items
        .slice()
        .reverse()
        .filter((i) => !keys.has(i.id) && !(i.run_id && runs.has(i.run_id)))
        .map(fromFeed)
      const newest = all.length
        ? Date.parse(all.filter((m) => m.createdAt).at(-1)?.createdAt ?? '')
        : 0
      const later = fresh.filter((m) => !m.createdAt || !newest || Date.parse(m.createdAt) > newest)
      return later.length ? [...all, ...later] : all
    })
  }, [])

  useEffect(() => {
    if (!target || !targetKey) return
    if (loadedKey.current === targetKey) return
    if (target.kind === 'new') {
      seq.current++
      abortAll()
      loadedKey.current = 'new'
      activeRef.current = null
      setActiveId(null)
      setDraft(true)
      setMessages([])
      setCursor(null)
      setLoading(false)
      setLoadError('')
      return
    }
    if (target.kind === 'default' && activeRef.current) {
      navigate(chatPath(activeRef.current), true)
      return
    }
    void loadChat(target.kind === 'chat' ? target.id : undefined)
    // `target` is derived from targetKey
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetKey, loadChat, abortAll])

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
      const page = await api.feed(cursor, 30, activeRef.current ?? undefined)
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
      await loadGate.current
      const userKey = crypto.randomUUID()
      const botKey = crypto.randomUUID()
      const dictated = options.dictated === true
      setMessages((all) => [
        ...all,
        blank({ key: userKey, role: 'user', text: clean, dictated }),
        blank({ key: botKey, role: 'assistant', pending: true, dictated }),
      ])
      try {
        let chatId = activeRef.current
        if (!chatId) {
          // A draft becomes a chat with its first message, so no empty chats pile up.
          const created = await api.createChat(newKey())
          chatId = created.id
          activeRef.current = chatId
          loadedKey.current = chatId
          setActiveId(chatId)
          setDraft(false)
          navigate(chatPath(chatId), true)
          void refreshChats()
        }
        const run = await api.createRun(clean, newKey(), chatId)
        patch(botKey, (m) => ({ ...m, runId: run.id }))
        void refreshChats()
        await follow(botKey, run.id, dictated, chatId)
      } catch (error) {
        patch(botKey, (m) => ({
          ...m,
          pending: false,
          error: messageOf(error, 'Не удалось отправить.'),
        }))
      }
    },
    [follow, patch, refreshChats],
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

  const newChat = useCallback(() => {
    if (loadedKey.current === 'new') return
    setSwitched((n) => n + 1)
    navigate('/chat/new')
  }, [])

  const selectChat = useCallback((id: string) => {
    if (activeRef.current === id && loadedKey.current === id) return
    setSwitched((n) => n + 1)
    navigate(chatPath(id))
  }, [])

  const renameChat = useCallback(async (id: string, title: string | null) => {
    const updated = await api.patchChat(id, { title })
    setChats((all) => all.map((c) => (c.id === id ? updated : c)))
  }, [])

  const pinChat = useCallback(
    async (id: string, pinned: boolean) => {
      await api.patchChat(id, { pinned })
      await refreshChats()
    },
    [refreshChats],
  )

  const removeChat = useCallback(
    async (id: string) => {
      await api.deleteChat(id)
      setChats((all) => all.filter((c) => c.id !== id))
      if (activeRef.current === id) {
        seq.current++
        abortAll()
        activeRef.current = null
        loadedKey.current = null
        setActiveId(null)
        setSwitched((n) => n + 1)
        navigate('/chat', true)
      }
      void refreshChats()
    },
    [abortAll, refreshChats],
  )

  const busy = messages.some((m) => m.pending)
  const value = useMemo<ChatState>(
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
      chats,
      chatsLoaded,
      chatsError,
      hasMoreChats: chatsNext !== null,
      activeId,
      draft,
      refreshChats,
      syncTail,
      loadMoreChats,
      newChat,
      selectChat,
      renameChat,
      pinChat,
      removeChat,
      activity,
      switched,
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
      chats,
      chatsLoaded,
      chatsError,
      chatsNext,
      activeId,
      draft,
      refreshChats,
      syncTail,
      loadMoreChats,
      newChat,
      selectChat,
      renameChat,
      pinChat,
      removeChat,
      activity,
      switched,
    ],
  )
  return <Context.Provider value={value}>{children}</Context.Provider>
}
