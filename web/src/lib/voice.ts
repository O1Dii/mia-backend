import { useEffect, useRef, useState } from 'react'

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
const browser = window as VoiceWindow

export function useVoice(setText: (value: string) => void) {
  const [listening, setListening] = useState(false)
  const [speaking, setSpeaking] = useState<number | null>(null)
  const [error, setError] = useState('')
  const recognition = useRef<Recognition | null>(null)
  const utterances = useRef<SpeechSynthesisUtterance[]>([])
  const speechRun = useRef(0)
  const supported = Boolean(browser.SpeechRecognition || browser.webkitSpeechRecognition)
  const canSpeak = 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window
  function stopListening() {
    const current = recognition.current
    recognition.current = null
    if (current) {
      current.onresult = null
      current.onerror = null
      current.onend = null
      current.abort()
    }
    setListening(false)
  }
  function stopSpeaking() {
    speechRun.current++
    if (canSpeak) window.speechSynthesis.cancel()
    utterances.current = []
    setSpeaking(null)
  }
  useEffect(
    () => () => {
      const current = recognition.current
      if (current) {
        current.onresult = null
        current.onerror = null
        current.onend = null
        current.abort()
      }
      speechRun.current++
      if ('speechSynthesis' in window) window.speechSynthesis.cancel()
    },
    [],
  )
  function dictate(previous: string) {
    if (recognition.current) {
      stopListening()
      return
    }
    const Constructor = browser.SpeechRecognition || browser.webkitSpeechRecognition
    if (!Constructor) {
      setError(
        'Этот браузер не поддерживает диктовку. Попробуйте Chrome или Safari либо используйте клавиатуру.',
      )
      return
    }
    stopSpeaking()
    setError('')
    const current = new Constructor()
    recognition.current = current
    current.lang = 'ru-RU'
    current.continuous = false
    current.interimResults = true
    current.onresult = (event) => {
      const parts = []
      for (let i = 0; i < event.results.length; i++) parts.push(event.results[i][0].transcript)
      setText([previous.trim(), parts.join(' ').trim()].filter(Boolean).join(' ').slice(0, 16000))
    }
    current.onerror = (event) => {
      setError(
        (
          {
            'not-allowed': 'Разрешите доступ к микрофону в настройках браузера.',
            'audio-capture': 'Микрофон недоступен. Проверьте подключение.',
            network: 'Сервис распознавания недоступен. Проверьте интернет.',
            'no-speech': 'Речь не распознана. Попробуйте ещё раз.',
          } as Record<string, string>
        )[event.error] ?? 'Диктовка остановлена. Можно продолжить с клавиатуры.',
      )
      recognition.current = null
      setListening(false)
    }
    current.onend = () => {
      if (recognition.current === current) recognition.current = null
      setListening(false)
    }
    try {
      setListening(true)
      current.start()
    } catch {
      recognition.current = null
      setListening(false)
      setError('Не удалось включить микрофон. Попробуйте ещё раз.')
    }
  }
  function speak(text: string, index: number) {
    if (speaking === index) {
      stopSpeaking()
      return
    }
    stopListening()
    stopSpeaking()
    setError('')
    if (!canSpeak) {
      setError('Озвучка не поддерживается этим браузером.')
      return
    }
    const run = speechRun.current
    const plain = text
      .replace(/```[\s\S]*?```/g, ' Фрагмент кода показан на экране. ')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/[#*_`]/g, '')
    const chunks = plain.match(/.{1,450}(?:\s|$)|\S{1,450}/gs) ?? [plain]
    const voices = window.speechSynthesis.getVoices()
    const voice = voices.find((v) => v.lang.toLowerCase().startsWith('ru'))
    utterances.current = chunks
      .filter((s) => s.trim())
      .map((chunk) => {
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
    if (!utterances.current.length) return
    utterances.current.at(-1)!.onend = () => {
      if (speechRun.current === run) {
        setSpeaking(null)
        utterances.current = []
      }
    }
    setSpeaking(index)
    for (const utterance of utterances.current) window.speechSynthesis.speak(utterance)
  }
  return {
    listening,
    speaking,
    error,
    supported,
    canSpeak,
    dictate,
    speak,
    stopListening,
    stopSpeaking,
  }
}
