import { useCallback, useEffect, useRef, useState } from 'react'

// Browser dictation (SpeechRecognition) and read-aloud (speechSynthesis). The recognized text goes
// into the input field; nothing is sent until the user (or the "send at once" setting) says so.

type RecognitionResult = { isFinal: boolean; [index: number]: { transcript: string } }
type RecognitionEvent = { results: { length: number; [index: number]: RecognitionResult } }
type Recognition = {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((event: RecognitionEvent) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  abort: () => void
  stop: () => void
}
type VoiceWindow = Window & {
  SpeechRecognition?: new () => Recognition
  webkitSpeechRecognition?: new () => Recognition
}

function recognitionConstructor() {
  const browser = window as VoiceWindow
  return browser.SpeechRecognition || browser.webkitSpeechRecognition
}

const dictationErrors: Record<string, string> = {
  'not-allowed': 'Разрешите доступ к микрофону в настройках браузера.',
  'service-not-allowed': 'Браузер не разрешил распознавание речи.',
  'audio-capture': 'Микрофон недоступен. Проверьте подключение.',
  network: 'Сервис распознавания недоступен. Проверьте интернет.',
  'no-speech': 'Речь не распознана. Попробуйте ещё раз.',
}

export type DictationHandlers = {
  /** Called with the whole field text (what was there before + what is recognized so far). */
  onText: (text: string) => void
  /** Called once when recognition ends with the final field text; `heard` is false for silence. */
  onEnd: (text: string, heard: boolean) => void
}

export function speakable(text: string): string[] {
  const plain = text.replace(/\s+/g, ' ').trim()
  if (!plain) return []
  return (plain.match(/.{1,450}(?:\s|$)|\S{1,450}/gs) ?? [plain]).filter((s) => s.trim())
}

export function useVoice() {
  const [listening, setListening] = useState(false)
  const [speaking, setSpeaking] = useState<string | null>(null)
  const [error, setError] = useState('')
  const recognition = useRef<Recognition | null>(null)
  const speechRun = useRef(0)
  const supported = Boolean(recognitionConstructor())
  const canSpeak = 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window

  const cancelDictation = useCallback(() => {
    const current = recognition.current
    recognition.current = null
    if (current) {
      current.onresult = null
      current.onerror = null
      current.onend = null
      current.abort()
    }
    setListening(false)
  }, [])

  const stopSpeaking = useCallback(() => {
    speechRun.current++
    if ('speechSynthesis' in window) window.speechSynthesis.cancel()
    setSpeaking(null)
  }, [])

  useEffect(
    () => () => {
      cancelDictation()
      speechRun.current++
      if ('speechSynthesis' in window) window.speechSynthesis.cancel()
    },
    [cancelDictation],
  )

  /** Toggle: a second call while listening asks the browser to finish and deliver the result. */
  const toggleDictation = useCallback(
    (previous: string, handlers: DictationHandlers) => {
      if (recognition.current) {
        recognition.current.stop()
        return
      }
      const Constructor = recognitionConstructor()
      if (!Constructor) {
        setError('Этот браузер не поддерживает диктовку. Используйте клавиатуру.')
        return
      }
      stopSpeaking()
      setError('')
      const current = new Constructor()
      recognition.current = current
      current.lang = 'ru-RU'
      current.continuous = false
      current.interimResults = true
      let latest = previous
      let heard = false
      current.onresult = (event) => {
        const parts: string[] = []
        for (let i = 0; i < event.results.length; i++) parts.push(event.results[i][0].transcript)
        const spoken = parts.join(' ').trim()
        if (!spoken) return
        heard = true
        latest = [previous.trim(), spoken].filter(Boolean).join(' ').slice(0, 16000)
        handlers.onText(latest)
      }
      current.onerror = (event) => {
        setError(
          dictationErrors[event.error] ?? 'Диктовка остановлена. Можно продолжить с клавиатуры.',
        )
      }
      current.onend = () => {
        if (recognition.current !== current) return
        recognition.current = null
        setListening(false)
        handlers.onEnd(latest, heard)
      }
      try {
        setListening(true)
        current.start()
      } catch {
        recognition.current = null
        setListening(false)
        setError('Не удалось включить микрофон. Попробуйте ещё раз.')
      }
    },
    [stopSpeaking],
  )

  /** Read `text` aloud; calling again with the same `key` while speaking stops it. */
  const speak = useCallback(
    (text: string, key: string) => {
      if (speaking === key) {
        stopSpeaking()
        return
      }
      cancelDictation()
      stopSpeaking()
      setError('')
      if (!canSpeak) {
        setError('Озвучка не поддерживается этим браузером.')
        return
      }
      const chunks = speakable(text)
      if (!chunks.length) return
      const run = speechRun.current
      const voice = window.speechSynthesis
        .getVoices()
        .find((v) => v.lang.toLowerCase().startsWith('ru'))
      const utterances = chunks.map((chunk) => {
        const utterance = new SpeechSynthesisUtterance(chunk)
        utterance.lang = 'ru-RU'
        if (voice) utterance.voice = voice
        utterance.onerror = (event) => {
          if (
            speechRun.current === run &&
            event.error !== 'interrupted' &&
            event.error !== 'canceled'
          ) {
            setError('Браузер не смог озвучить ответ. Нажмите «Озвучить» ещё раз.')
            stopSpeaking()
          }
        }
        return utterance
      })
      utterances[utterances.length - 1].onend = () => {
        if (speechRun.current === run) setSpeaking(null)
      }
      setSpeaking(key)
      for (const utterance of utterances) window.speechSynthesis.speak(utterance)
    },
    [speaking, canSpeak, cancelDictation, stopSpeaking],
  )

  return {
    listening,
    speaking,
    error,
    supported,
    canSpeak,
    toggleDictation,
    cancelDictation,
    speak,
    stopSpeaking,
    clearError: () => setError(''),
  }
}
