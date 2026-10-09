import { useCallback, useState } from 'react'

// Per-browser conveniences only. Nothing secret is stored; every access tolerates blocked storage.
export type VoicePrefs = { autoSend: boolean; alwaysSpeak: boolean }
const KEY = 'mia.voice.prefs'
const defaults: VoicePrefs = { autoSend: false, alwaysSpeak: false }

function load(): VoicePrefs {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return defaults
    const data = JSON.parse(raw) as Partial<VoicePrefs>
    return { autoSend: data.autoSend === true, alwaysSpeak: data.alwaysSpeak === true }
  } catch {
    return defaults
  }
}

export function useVoicePrefs(): [VoicePrefs, (patch: Partial<VoicePrefs>) => void] {
  const [prefs, setPrefs] = useState(load)
  const update = useCallback((patch: Partial<VoicePrefs>) => {
    setPrefs((old) => {
      const next = { ...old, ...patch }
      try {
        localStorage.setItem(KEY, JSON.stringify(next))
      } catch {
        // storage unavailable: the choice then lasts until the page is closed
      }
      return next
    })
  }, [])
  return [prefs, update]
}
