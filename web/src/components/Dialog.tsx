import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import type { ReactNode } from 'react'

export function Dialog({
  title,
  description,
  children,
  onClose,
  wide = false,
}: {
  title: string
  description: string
  children: ReactNode
  onClose: () => void
  wide?: boolean
}) {
  return (
    <DialogPrimitive.Root
      open
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="dialog-overlay" />
        <DialogPrimitive.Content className={`dialog mw-dialog ${wide ? 'dialog-wide' : ''}`}>
          <DialogPrimitive.Title>{title}</DialogPrimitive.Title>
          <DialogPrimitive.Description>{description}</DialogPrimitive.Description>
          <DialogPrimitive.Close className="mia-icon-btn dialog-close" aria-label="Закрыть">
            <X size={20} />
          </DialogPrimitive.Close>
          {children}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
