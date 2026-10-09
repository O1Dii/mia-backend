import { useCallback, useEffect, useState } from 'react'
import { LoaderCircle, ShieldCheck } from 'lucide-react'
import { api, type Approval } from '../lib/api'
import { useChat } from '../lib/chat'
import { messageOf } from '../lib/http'
import { ApprovalCard } from './ChatParts'

const POLL_MS = 4000

/** Pending approvals of all the user's runs, wherever they were started (web or phone). */
export function usePendingApprovals() {
  const { setPendingApprovals } = useChat()
  const [items, setItems] = useState<Approval[] | null>(null)
  const [error, setError] = useState('')
  const refresh = useCallback(async () => {
    try {
      const page = await api.approvals('pending')
      setItems(page.items)
      setPendingApprovals(page.items.length)
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить подтверждения.'))
    }
  }, [setPendingApprovals])
  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])
  return { items, error, refresh }
}

export function Approvals({ pending }: { pending: ReturnType<typeof usePendingApprovals> }) {
  const chat = useChat()
  const { items, error, refresh } = pending
  const [history, setHistory] = useState<Approval[]>([])

  const loadHistory = useCallback(async () => {
    const results = await Promise.all(
      (['approved', 'denied', 'expired'] as const).map((status) =>
        api.approvals(status).then(
          (p) => p.items,
          () => [] as Approval[],
        ),
      ),
    )
    setHistory(
      results
        .flat()
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, 20),
    )
  }, [])
  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  async function decide(approval: Approval, decision: 'approve' | 'deny') {
    await chat.decide(approval, decision)
    await Promise.all([refresh(), loadHistory()])
  }

  return (
    <section className="mw-page" aria-labelledby="approvals-title">
      <header className="mw-page__head">
        <h1 id="approvals-title">Подтверждения</h1>
        <p>
          Мия спрашивает вас перед действиями во внешних сервисах. Без вашего решения ничего не
          происходит.
        </p>
      </header>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      {items === null && !error && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={16} className="spin" /> Загружаем…
        </p>
      )}
      {items?.length === 0 && (
        <div className="mw-empty mia-glass">
          <ShieldCheck size={28} aria-hidden="true" />
          <h2>Ничего не ждёт решения</h2>
          <p>Когда Мия попросит разрешение на действие, оно появится здесь и в чате.</p>
        </div>
      )}
      <div className="mw-stack">
        {items?.map((approval) => (
          <ApprovalCard key={approval.id} approval={approval} onDecide={decide} />
        ))}
      </div>
      {history.length > 0 && (
        <>
          <h2 className="mw-section">Недавние решения</h2>
          <div className="mw-stack">
            {history.map((approval) => (
              <ApprovalCard key={approval.id} approval={approval} onDecide={decide} compact />
            ))}
          </div>
        </>
      )}
    </section>
  )
}
