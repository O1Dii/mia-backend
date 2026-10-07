import { z } from 'zod'

export const timerSchema = z.record(
  z.string(),
  z.object({
    deadline: z.number().nullable(),
    remaining: z.number().min(0),
    label: z.string(),
  }),
)
export type Timers = z.infer<typeof timerSchema>
let audio: AudioContext | undefined

export function unlockAudio() {
  try {
    audio ??= new AudioContext()
    void audio.resume().catch(() => {})
  } catch {
    /* The visible reminder always works even if audio is unavailable. */
  }
}

export function chime() {
  if (!audio || audio.state !== 'running') return
  for (let i = 0; i < 3; i++) {
    const oscillator = audio.createOscillator()
    const gain = audio.createGain()
    oscillator.connect(gain)
    gain.connect(audio.destination)
    oscillator.frequency.value = i === 1 ? 660 : 523.25
    const start = audio.currentTime + i * 0.35
    gain.gain.setValueAtTime(0, start)
    gain.gain.linearRampToValueAtTime(0.18, start + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.001, start + 0.3)
    oscillator.start(start)
    oscillator.stop(start + 0.32)
  }
}

export function formatDuration(ms: number) {
  const seconds = Math.ceil(Math.max(ms, 0) / 1000)
  return `${Math.floor(seconds / 60)
    .toString()
    .padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`
}
