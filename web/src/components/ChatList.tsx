import { useEffect, useState } from 'react'
import { Check, Ellipsis, LoaderCircle, MessageSquarePlus, Pin, PinOff, X } from 'lucide-react'
import { toast } from 'sonner'
import { api, type Chat, type RetentionDays } from '../lib/api'
import { useChat } from '../lib/chat'
import { when } from '../lib/format'
import { messageOf } from '../lib/http'
import { plainText } from '../lib/markdown'
import { Dialog } from './Dialog'

export const chatTitle = (chat: Pick<Chat, 'title'>): string => chat.title?.trim() || 'Новый чат'

const retentionLabel = (days: RetentionDays): string =>
  days === null ? 'Не удалять' : `${days} дней`
const retentionValue = (days: RetentionDays): string => (days === null ? 'never' : String(days))

/**
 * Chat list (M75, ADR-0017): switch, create, rename, pin, delete and the retention setting.
 * A permanent column on a wide screen, a drawer on a phone or tablet (`open`).
 */
export function ChatList({ open, onClose }: { open: boolean; onClose: () => void }) {
  const chat = useChat()
  const [menu, setMenu] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const [confirm, setConfirm] = useState<Chat | null>(null)
  const [busy, setBusy] = useState(false)

  async function run(action: () => Promise<void>, fallback: string) {
    try {
      await action()
    } catch (error) {
      toast.error(messageOf(error, fallback))
    }
  }

  function startRename(item: Chat) {
    setMenu(null)
    setTitle(item.title ?? '')
    setEditing(item.id)
  }

  async function saveRename(item: Chat) {
    const clean = title.trim()
    setEditing(null)
    // An empty name returns the chat to the automatic title.
    if (clean === (item.title ?? '')) return
    await run(() => chat.renameChat(item.id, clean || null), 'Не удалось переименовать чат.')
  }

  return (
    <>
      {open && <div className="mw-chats__scrim" aria-hidden="true" onClick={onClose} />}
      <aside className="mw-chats mia-glass" data-open={open ? 'true' : 'false'} aria-label="Чаты">
        <div className="mw-chats__head">
          <h2>Чаты</h2>
          <button
            type="button"
            className="mia-btn mia-btn--primary mw-chats__new"
            onClick={() => {
              chat.newChat()
              onClose()
            }}
          >
            <MessageSquarePlus size={16} aria-hidden="true" /> Новый чат
          </button>
          <button
            type="button"
            className="mia-icon-btn mw-chats__close"
            aria-label="Закрыть список чатов"
            onClick={onClose}
          >
            <X size={20} />
          </button>
        </div>

        {chat.chatsError && (
          <p className="mw-error" role="alert">
            {chat.chatsError}{' '}
            <button className="mw-link" onClick={() => void chat.refreshChats()}>
              Повторить
            </button>
          </p>
        )}
        {chat.chatsLoaded && !chat.chats.length && !chat.chatsError && (
          <p className="mw-muted">Чатов пока нет. Напишите Мии, и он появится здесь.</p>
        )}
        <ul className="mw-chats__list" aria-label="Список чатов">
          {chat.draft && (
            <li className="mw-chats__item">
              <div className="mw-chats__row">
                <button
                  type="button"
                  className="mw-chats__main"
                  aria-current="true"
                  onClick={onClose}
                >
                  <span className="mw-chats__title">Новый чат</span>
                  <span className="mw-chats__preview">Ещё без сообщений</span>
                </button>
              </div>
            </li>
          )}
          {chat.chats.map((item) => {
            const active = item.id === chat.activeId
            const name = chatTitle(item)
            return (
              <li
                key={item.id}
                className="mw-chats__item"
                data-chat-id={item.id}
                data-pinned={item.pinned ? 'true' : undefined}
              >
                {editing === item.id ? (
                  <form
                    className="mw-chats__rename"
                    onSubmit={(event) => {
                      event.preventDefault()
                      void saveRename(item)
                    }}
                  >
                    <input
                      className="mia-field__input"
                      aria-label="Название чата"
                      value={title}
                      maxLength={80}
                      autoFocus
                      placeholder="Пусто — название подберёт Мия"
                      onChange={(event) => setTitle(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Escape') setEditing(null)
                      }}
                    />
                    <button type="submit" className="mia-icon-btn" aria-label="Сохранить название">
                      <Check size={18} />
                    </button>
                    <button
                      type="button"
                      className="mia-icon-btn"
                      aria-label="Отменить переименование"
                      onClick={() => setEditing(null)}
                    >
                      <X size={18} />
                    </button>
                  </form>
                ) : (
                  <div className="mw-chats__row">
                    <button
                      type="button"
                      className="mw-chats__main"
                      aria-current={active ? 'true' : undefined}
                      onClick={() => {
                        chat.selectChat(item.id)
                        onClose()
                      }}
                    >
                      <span className="mw-chats__title">
                        {item.pinned && <Pin size={12} aria-label="Закреплён" />}
                        <span className="mw-chats__name">{name}</span>
                        {item.kind === 'telegram' && <span className="mia-chip">Telegram</span>}
                      </span>
                      <span className="mw-chats__preview">
                        {item.preview_role === 'user' && item.preview ? 'Вы: ' : ''}
                        {item.preview ? plainText(item.preview) : 'Ещё без сообщений'}
                      </span>
                      <span className="mw-chats__when">
                        {when(item.last_message_at ?? item.created_at)}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="mia-icon-btn"
                      aria-label={`Действия чата «${name}»`}
                      aria-expanded={menu === item.id}
                      onClick={() => setMenu(menu === item.id ? null : item.id)}
                    >
                      <Ellipsis size={18} />
                    </button>
                  </div>
                )}
                {menu === item.id && editing !== item.id && (
                  <div className="mw-chats__menu" role="group" aria-label={`Чат «${name}»`}>
                    <button
                      type="button"
                      className="mia-btn mia-btn--ghost"
                      onClick={() => startRename(item)}
                    >
                      Переименовать
                    </button>
                    <button
                      type="button"
                      className="mia-btn mia-btn--ghost"
                      onClick={() => {
                        setMenu(null)
                        void run(
                          () => chat.pinChat(item.id, !item.pinned),
                          'Не удалось изменить закрепление.',
                        )
                      }}
                    >
                      {item.pinned ? (
                        <>
                          <PinOff size={14} aria-hidden="true" /> Открепить
                        </>
                      ) : (
                        <>
                          <Pin size={14} aria-hidden="true" /> Закрепить
                        </>
                      )}
                    </button>
                    <button
                      type="button"
                      className="mia-btn mia-btn--ghost mw-chats__delete"
                      onClick={() => {
                        setMenu(null)
                        setConfirm(item)
                      }}
                    >
                      Удалить
                    </button>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
        {chat.hasMoreChats && (
          <button
            type="button"
            className="mia-btn mia-btn--ghost"
            onClick={() => void chat.loadMoreChats()}
          >
            Показать ещё чаты
          </button>
        )}
        <Retention />
      </aside>

      {confirm && (
        <Dialog
          title="Удалить чат?"
          description={`Переписка «${chatTitle(confirm)}» будет стёрта насовсем. Память, задачи и входящие останутся.`}
          onClose={() => setConfirm(null)}
        >
          <div className="mw-dialog__actions">
            <button
              className="mia-btn mia-btn--secondary"
              disabled={busy}
              onClick={() => setConfirm(null)}
            >
              Отмена
            </button>
            <button
              className="mia-btn mia-btn--danger"
              disabled={busy}
              onClick={async () => {
                setBusy(true)
                await run(() => chat.removeChat(confirm.id), 'Не удалось удалить чат.')
                setBusy(false)
                setConfirm(null)
              }}
            >
              {busy && <LoaderCircle size={16} className="spin" />} Удалить чат
            </button>
          </div>
        </Dialog>
      )}
    </>
  )
}

/** How long a chat without new messages is kept: 30, 60, 180 days or never (pinned chats stay). */
function Retention() {
  const [days, setDays] = useState<RetentionDays | undefined>(undefined)
  const [choices, setChoices] = useState<RetentionDays[]>([30, 60, 180, null])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    api
      .chatSettings()
      .then((settings) => {
        if (!active) return
        setDays(settings.retention_days)
        setChoices(settings.choices)
      })
      .catch((e) => active && setError(messageOf(e, 'Не удалось загрузить срок хранения.')))
    return () => {
      active = false
    }
  }, [])

  async function change(value: string) {
    const next = value === 'never' ? null : (Number(value) as RetentionDays)
    const before = days
    setSaving(true)
    setError('')
    setDays(next)
    try {
      const saved = await api.putChatSettings(next)
      setDays(saved.retention_days)
      toast.success('Срок хранения чатов сохранён')
    } catch (e) {
      setDays(before)
      setError(messageOf(e, 'Не удалось сохранить срок хранения.'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mw-chats__retention">
      <label className="mw-field-row" htmlFor="chat-retention">
        <span>Хранить чаты</span>
        <select
          id="chat-retention"
          value={days === undefined ? '' : retentionValue(days)}
          disabled={days === undefined || saving}
          onChange={(event) => void change(event.target.value)}
        >
          {days === undefined && <option value="">…</option>}
          {choices.map((value) => (
            <option key={retentionValue(value)} value={retentionValue(value)}>
              {retentionLabel(value)}
            </option>
          ))}
        </select>
      </label>
      <p className="mw-muted">
        Чат без новых сообщений удаляется через этот срок. Закреплённые чаты остаются.
      </p>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
