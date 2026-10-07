import { useRef, useState } from 'react'
import {
  ArrowUp,
  LoaderCircle,
  MessageCircle,
  Plus,
  Sparkles,
  Mic,
  MicOff,
  Volume2,
  Square,
} from 'lucide-react'
import { useVoice } from '../lib/voice'
import { useWorkspace } from '../lib/workspace'
import { unlockAudio } from '../plugins/scheduler'
import { api, type Connection, type Message, type ActionResult } from '../lib/api'

export function Chat({ active, openModels }: { active?: Connection; openModels: () => void }) {
  const [messages, setMessages] = useState<Message[]>([])
  const [text, setText] = useState('')
  const voice = useVoice(setText)
  const [autoRead, setAutoRead] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [actions, setActions] = useState<ActionResult[]>([])
  const { refresh, snapshot } = useWorkspace()
  const retry = useRef<{ fingerprint: string; id: string } | null>(null)
  async function send() {
    if (!active || !text.trim() || busy) return
    const next: Message[] = [...messages, { role: 'user', content: text.trim() }]
    if (next.length > 39 || next.reduce((sum, m) => sum + m.content.length, 0) > 46000) {
      setError('Начните новый диалог: лимит истории POC достигнут.')
      return
    }
    const fingerprint = JSON.stringify([active.id, next])
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, id: crypto.randomUUID() }
    voice.stopListening()
    voice.stopSpeaking()
    unlockAudio()
    setMessages(next)
    setText('')
    setBusy(true)
    setError('')
    try {
      const result = await api.chat(active.id, next, { request_id: retry.current!.id })
      setMessages([...next, { role: 'assistant', content: result.text }])
      if (autoRead) voice.speak(result.text, next.length)
      setActions(result.actions ?? [])
      if (result.warning) setError(result.warning)
      retry.current = null
      await refresh()
    } catch (e) {
      await refresh()
      setMessages(messages)
      setText(next[next.length - 1].content)
      setError(e instanceof Error ? e.message : 'Не удалось получить ответ.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="chat-page">
      <div className="section-heading">
        <h1>Диалог с Мией</h1>
        <button
          className="button secondary"
          disabled={busy}
          onClick={() => {
            voice.stopSpeaking()
            voice.stopListening()
            setMessages([])
            setError('')
          }}
        >
          <Plus size={16} /> Новый диалог
        </button>
      </div>
      <p className="chat-model">
        {active ? `${active.label} · ${active.model}` : 'Ваша модель ещё не подключена'} · история
        хранится до ухода с экрана
      </p>
      <div className="chat-installed-skills">
        {snapshot?.installed.length ? (
          <>
            <span>Доступно навыков: {snapshot.installed.length}</span>
            <a href="#dashboard">Открыть дашборд →</a>
          </>
        ) : (
          <a href="#store">Установите плагины, чтобы Мия могла выполнять действия →</a>
        )}
      </div>
      <div className="chat-scroll" aria-live="polite">
        {messages.length === 0 && (
          <div className="chat-welcome">
            <div className="chat-orb">
              <Sparkles size={36} strokeWidth={1} />
            </div>
            <span className="eyebrow">ПРОСТРАНСТВО ДЛЯ ВАШИХ ИДЕЙ</span>
            <h2>С чего начнём?</h2>
            <p>
              Разложим сложное по полочкам, найдём идею
              <br />
              или просто подумаем вместе.
            </p>
            {!active ? (
              <button className="button primary" onClick={openModels}>
                Подключить свою модель
              </button>
            ) : (
              <div className="chat-suggestions">
                {[
                  'Поставь таймер на 5 минут',
                  'Собери последние новости об искусственном интеллекте',
                  'Сохрани заметку: проверить мой проект',
                ].map((s) => (
                  <button key={s} onClick={() => setText(s)}>
                    {s}
                    <ArrowUp size={14} />
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {messages.map((message, index) => (
          <div className={`chat-message ${message.role}`} key={index}>
            <span>{message.role === 'user' ? 'Вы' : 'Мия'}</span>
            <p>{message.content}</p>
            {message.role === 'assistant' && voice.canSpeak && (
              <button
                className="read-message text-button"
                aria-label={voice.speaking === index ? 'Остановить озвучку' : 'Озвучить ответ'}
                onClick={() => voice.speak(message.content, index)}
              >
                {voice.speaking === index ? <Square size={14} /> : <Volume2 size={14} />}{' '}
                {voice.speaking === index ? 'Остановить' : 'Озвучить'}
              </button>
            )}
          </div>
        ))}
        {busy && (
          <div className="thinking">
            <LoaderCircle className="spin" size={17} /> Модель думает…
          </div>
        )}
      </div>
      {actions.length > 0 && (
        <div className="chat-action-results">
          {actions.map((action, index) => (
            <a
              key={index}
              href="#dashboard"
              className={action.ok ? 'action-success' : 'action-failed'}
            >
              <span>
                {action.ok ? '✓' : '!'}{' '}
                {snapshot?.manifests[action.plugin_id ?? '']?.name ?? action.name}
              </span>
              <small>{action.ok ? 'Выполнено · открыть виджет →' : action.error}</small>
            </a>
          ))}
        </div>
      )}
      {error && (
        <p className="error-message" role="alert">
          {error}
        </p>
      )}
      <div className="voice-toolbar">
        <button
          className={`button secondary voice-input ${voice.listening ? 'listening' : ''}`}
          disabled={busy || !active || !voice.supported}
          onClick={() => voice.dictate(text)}
          aria-label={voice.listening ? 'Остановить диктовку' : 'Голосовой ввод'}
        >
          {voice.listening ? <MicOff size={17} /> : <Mic size={17} />}{' '}
          {voice.listening ? 'Слушаю… остановить' : 'Голосовой ввод'}
        </button>
        {voice.canSpeak && (
          <label className="auto-read">
            <input
              type="checkbox"
              checked={autoRead}
              onChange={(e) => {
                setAutoRead(e.target.checked)
                if (!e.target.checked) voice.stopSpeaking()
              }}
            />{' '}
            Озвучивать ответы
          </label>
        )}
      </div>
      <p className="voice-note">
        {voice.supported
          ? 'Диктовка заполняет текст — отправку подтверждаете вы. Браузер может передавать аудио своему сервису распознавания.'
          : 'Диктовка недоступна в этом браузере. Используйте клавиатуру или Chrome/Safari с поддержкой Web Speech.'}
      </p>
      {voice.error && (
        <p role="alert" className="error-message">
          {voice.error}
        </p>
      )}
      <form
        className="chat-composer"
        onSubmit={(e) => {
          e.preventDefault()
          void send()
        }}
      >
        <MessageCircle size={19} />
        <textarea
          aria-label="Сообщение Мии"
          rows={2}
          maxLength={16000}
          placeholder={active ? 'Напишите Мии…' : 'Сначала подключите модель'}
          disabled={!active || busy}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <button
          className="send-button"
          aria-label="Отправить сообщение"
          disabled={!active || !text.trim() || busy}
        >
          {busy ? <LoaderCircle className="spin" size={19} /> : <ArrowUp size={21} />}
        </button>
      </form>
      <p className="field-note chat-disclaimer">
        {active
          ? 'Текст будет отправлен выбранному провайдеру. Модель может ошибаться.'
          : 'Подключите облачный API или используйте локальную модель.'}
      </p>
    </div>
  )
}
