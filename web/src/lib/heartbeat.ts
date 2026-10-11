import { useEffect } from 'react'
import { api } from './api'

const EVERY_MS = 60_000

/**
 * «Пульс» of an open tab (M84): while the page is visible the browser tells Mia once a minute that
 * it is here, so «Устройства» shows it «онлайн». A hidden tab stays quiet; failures are ignored.
 */
export function useDeviceHeartbeat() {
  useEffect(() => {
    let last = 0
    const beat = () => {
      if (document.visibilityState !== 'visible') return
      if (Date.now() - last < EVERY_MS / 2) return
      last = Date.now()
      api.deviceHeartbeat().catch(() => undefined)
    }
    beat()
    const timer = window.setInterval(beat, EVERY_MS)
    document.addEventListener('visibilitychange', beat)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', beat)
    }
  }, [])
}
