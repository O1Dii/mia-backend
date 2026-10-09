// Small display helpers shared by the M31 screens.
const dateTime = new Intl.DateTimeFormat('ru-RU', {
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
})

export function when(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : dateTime.format(date)
}

/** Save a response of the API as a file the browser downloads (cookie session, same origin). */
export async function downloadFile(url: string, filename: string): Promise<void> {
  const response = await fetch(url, { credentials: 'same-origin' })
  if (!response.ok) throw new Error(`download failed: ${response.status}`)
  const blob = await response.blob()
  const link = document.createElement('a')
  const href = URL.createObjectURL(blob)
  link.href = href
  link.download = filename
  document.body.append(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(href), 10_000)
}
