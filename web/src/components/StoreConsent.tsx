import { useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import type { Consent } from '../lib/api'
import { messageOf } from '../lib/http'
import { Dialog } from './Dialog'
import { ConsentSummary } from './StoreShared'
import { DeviceCapabilities } from './StoreWhere'

/** The consent screen: what the plugin can do, exactly the digest and grants the user saw. */
export function ConsentDialog({
  title,
  description,
  consent,
  added,
  action,
  onConfirm,
  onClose,
}: {
  title: string
  description: string
  consent: Consent
  added?: string[]
  action: string
  onConfirm: () => Promise<void>
  onClose: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function confirm() {
    setBusy(true)
    setError('')
    try {
      await onConfirm()
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }
  return (
    <Dialog
      title={title}
      description={description}
      onClose={busy ? () => undefined : onClose}
      wide
      tall
    >
      {/* The consent scrolls between the title and the actions, which always stay on screen. */}
      <div className="mw-dialog__body">
        <ConsentSummary consent={consent} added={added} />
        <DeviceCapabilities consent={consent} />
      </div>
      {error && (
        <p className="mw-error" role="alert">
          {error}
        </p>
      )}
      <div className="mw-dialog__actions">
        <button className="mia-btn mia-btn--secondary" disabled={busy} onClick={onClose}>
          Отмена
        </button>
        <button className="mia-btn mia-btn--primary" disabled={busy} onClick={() => void confirm()}>
          {busy && <LoaderCircle size={16} className="spin" />} {action}
        </button>
      </div>
    </Dialog>
  )
}
