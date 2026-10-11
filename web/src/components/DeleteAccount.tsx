import { useState } from 'react'
import { LoaderCircle, Trash2 } from 'lucide-react'
import { api } from '../lib/api'
import { ApiError, messageOf } from '../lib/http'
import { Dialog } from './Dialog'

/** What the user types to confirm: deletion cannot be undone, so one click is not enough. */
export const DELETE_WORD = 'УДАЛИТЬ'

/** «Удалить аккаунт» on «Ещё» (M17-R1): a row that opens a type-to-confirm dialog. */
export function DeleteAccountRow() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <div className="mia-glass mw-block mw-danger-zone">
        <div className="mw-danger-zone__text">
          <strong>Удалить аккаунт</strong>
          <span className="mw-muted">
            Память, чаты, плагины и их аккаунты, подключения моделей и вход на всех устройствах
            будут удалены насовсем.
          </span>
        </div>
        <button className="mia-btn mia-btn--danger" onClick={() => setOpen(true)}>
          <Trash2 size={16} aria-hidden="true" /> Удалить аккаунт
        </button>
      </div>
      {open && <DeleteAccountDialog close={() => setOpen(false)} />}
    </>
  )
}

function DeleteAccountDialog({ close }: { close: () => void }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const confirmed = typed.trim().toUpperCase() === DELETE_WORD

  async function remove() {
    if (!confirmed || busy) return
    setBusy(true)
    setError('')
    try {
      await api.deleteAccount()
    } catch (e) {
      // 401: the account is already gone (a retry after a lost answer) — same as success.
      if (!(e instanceof ApiError && e.status === 401)) {
        setError(messageOf(e, 'Не удалось удалить аккаунт. Попробуйте ещё раз.'))
        setBusy(false)
        return
      }
    }
    location.assign(`${import.meta.env.BASE_URL}?account=deleted`)
  }

  return (
    <Dialog
      title="Удалить аккаунт?"
      description="Это нельзя отменить. Мия забудет всё, что знает о вас."
      onClose={() => !busy && close()}
    >
      <form
        className="mw-form"
        onSubmit={(event) => {
          event.preventDefault()
          void remove()
        }}
      >
        <ul className="mw-danger-list">
          <li>память, люди и предпочтения — ключ шифрования уничтожается;</li>
          <li>чаты, история, фоновые задачи и входящие;</li>
          <li>плагины, их аккаунты (доступ отзывается у сервиса) и подключения моделей;</li>
          <li>вход на телефоне и в браузере, приложения «Войти через Mia», Telegram и push.</li>
        </ul>
        <label>
          <span>
            Чтобы подтвердить, введите <b>{DELETE_WORD}</b>
          </span>
          <input
            value={typed}
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            aria-label={`Введите ${DELETE_WORD}`}
            onChange={(event) => setTyped(event.target.value)}
          />
        </label>
        {error && (
          <p className="mw-error" role="alert">
            {error}
          </p>
        )}
        <div className="mw-dialog__actions">
          <button
            type="button"
            className="mia-btn mia-btn--secondary"
            onClick={close}
            disabled={busy}
          >
            Отмена
          </button>
          <button type="submit" className="mia-btn mia-btn--danger" disabled={!confirmed || busy}>
            {busy && <LoaderCircle size={16} className="spin" />} Удалить навсегда
          </button>
        </div>
      </form>
    </Dialog>
  )
}
