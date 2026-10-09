import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  ArrowUp,
  History,
  LoaderCircle,
  Mic,
  MicOff,
  Settings2,
  Square,
  Volume2,
} from 'lucide-react'
import { api } from '../lib/api'
import { speechOf, useChat, type ChatMessage } from '../lib/chat'
import { Link } from '../lib/router'
import { ApprovalCard, DisplayView, ToolSteps } from './ChatParts'

const starters = [
  'Привет! Что ты умеешь?',
  'Покажи список задач',
  'Создай задачу Купить молоко',
  'Покажи карточку на сегодня',
]

export function Chat() {
  const chat = useChat()
  const { voice, prefs } = chat
  const [text, setText] = useState('')
  const [options, setOptions] = useState(false)
  const [hasModel, setHasModel] = useState<boolean | null>(null)
  const dictated = useRef(false)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const field = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    let active = true
    api
      .models()
      .then((list) => active && setHasModel(list.items.length > 0))
      .catch(() => active && setHasModel(null))
    return () => {
      active = false
    }
  }, [])

  useLayoutEffect(() => {
    const el = scroller.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [chat.messages, chat.loading])

  useLayoutEffect(() => {
    const el = field.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`
  }, [text])

  function submit(value: string, fromVoice: boolean) {
    const clean = value.trim()
    if (!clean || chat.busy) return
    voice.cancelDictation()
    stick.current = true
    setText('')
    const wasDictated = fromVoice || dictated.current
    dictated.current = false
    void chat.send(clean, { dictated: wasDictated })
  }

  function mic() {
    voice.toggleDictation(text, {
      onText: (value) => {
        dictated.current = true
        setText(value)
      },
      onEnd: (value, heard) => {
        if (heard && chat.prefs.autoSend && value.trim()) submit(value, true)
      },
    })
  }

  const last = chat.messages.at(-1)
  const followups = !chat.busy && last?.role === 'assistant' ? last.followups : []

  return (
    <section className="mw-chat" aria-label="Чат с Мией">
      <div
        className="mw-chat__scroll"
        ref={scroller}
        role="log"
        aria-live="polite"
        onScroll={(event) => {
          const el = event.currentTarget
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
        }}
      >
        {chat.hasMore && (
          <button
            className="mia-btn mia-btn--ghost mw-chat__more"
            disabled={chat.loadingMore}
            onClick={() => void chat.loadMore()}
          >
            {chat.loadingMore ? <LoaderCircle size={16} className="spin" /> : <History size={16} />}{' '}
            Показать более ранние
          </button>
        )}
        {chat.loading && (
          <p className="mw-muted" role="status">
            <LoaderCircle size={16} className="spin" /> Загружаем историю…
          </p>
        )}
        {chat.loadError && (
          <p className="mw-error" role="alert">
            {chat.loadError}{' '}
            <button className="mw-link" onClick={() => void chat.reload()}>
              Повторить
            </button>
          </p>
        )}
        {!chat.loading && !chat.messages.length && !chat.loadError && (
          <div className="mw-empty mia-rise">
            <span className="mia-orb mia-orb--lg" data-state="idle" aria-hidden="true" />
            <h2>С чего начнём?</h2>
            <p>
              Спросите что угодно или попросите Мию что-то сделать с помощью установленных плагинов.
            </p>
            <div className="mw-chips">
              {starters.map((starter) => (
                <button
                  key={starter}
                  className="mia-chip mw-chip-btn"
                  onClick={() => submit(starter, false)}
                >
                  {starter}
                </button>
              ))}
            </div>
          </div>
        )}
        {chat.messages.map((message) => (
          <MessageView key={message.key} message={message} />
        ))}
      </div>

      <div className="mw-chat__dock">
        {hasModel === false && (
          <p className="mw-notice">
            Модель ещё не подключена. <Link to="/models">Подключить модель</Link>
          </p>
        )}
        {followups.length > 0 && (
          <div className="mw-chips" aria-label="Что спросить дальше">
            {followups.map((item) => (
              <button
                key={item}
                className="mia-chip mw-chip-btn"
                onClick={() => submit(item, false)}
              >
                {item}
              </button>
            ))}
          </div>
        )}
        {voice.error && (
          <p className="mw-error" role="alert">
            {voice.error}
          </p>
        )}
        <form
          className="mia-field mia-glass mw-composer"
          onSubmit={(event) => {
            event.preventDefault()
            submit(text, false)
          }}
        >
          <textarea
            ref={field}
            className="mia-field__input mw-composer__input"
            aria-label="Сообщение Мии"
            placeholder={voice.listening ? 'Слушаю…' : 'Напишите Мии…'}
            rows={1}
            maxLength={16000}
            value={text}
            onChange={(event) => {
              dictated.current = false
              setText(event.target.value)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                submit(text, false)
              }
            }}
          />
          {voice.supported && (
            <button
              type="button"
              className="mia-icon-btn mia-field__mic"
              aria-pressed={voice.listening}
              aria-label={voice.listening ? 'Остановить диктовку' : 'Голосовой ввод'}
              onClick={mic}
            >
              {voice.listening ? <MicOff size={20} /> : <Mic size={20} />}
            </button>
          )}
          {chat.busy ? (
            <button
              type="button"
              className="mia-icon-btn"
              aria-label="Остановить ответ"
              onClick={chat.stop}
            >
              <Square size={18} />
            </button>
          ) : (
            <button
              type="submit"
              className="mia-icon-btn mw-send"
              aria-label="Отправить сообщение"
              disabled={!text.trim()}
            >
              <ArrowUp size={20} />
            </button>
          )}
        </form>
        <div className="mw-chat__tools">
          {!voice.supported && (
            <p className="mw-muted">
              Диктовка недоступна в этом браузере. Используйте клавиатуру или Chrome/Safari.
            </p>
          )}
          <button
            className="mw-link"
            aria-expanded={options}
            aria-controls="voice-options"
            onClick={() => setOptions((open) => !open)}
          >
            <Settings2 size={14} /> Голос
          </button>
        </div>
        {options && (
          <fieldset id="voice-options" className="mia-glass mw-options">
            <legend>Голос</legend>
            {voice.supported && (
              <label className="mw-switch">
                <input
                  type="checkbox"
                  checked={prefs.autoSend}
                  onChange={(event) => chat.setPrefs({ autoSend: event.target.checked })}
                />
                <span>Отправлять сразу после диктовки</span>
              </label>
            )}
            {voice.canSpeak ? (
              <label className="mw-switch">
                <input
                  type="checkbox"
                  checked={prefs.alwaysSpeak}
                  onChange={(event) => {
                    chat.setPrefs({ alwaysSpeak: event.target.checked })
                    if (!event.target.checked) voice.stopSpeaking()
                  }}
                />
                <span>Всегда озвучивать ответы</span>
              </label>
            ) : (
              <p className="mw-muted">Озвучка не поддерживается этим браузером.</p>
            )}
            <p className="mw-muted">
              Если вопрос надиктован, ответ озвучивается сам. Браузер может передавать аудио своему
              сервису распознавания.
            </p>
          </fieldset>
        )}
      </div>
    </section>
  )
}

function MessageView({ message }: { message: ChatMessage }) {
  const chat = useChat()
  const { voice } = chat
  if (message.role === 'user')
    return (
      <div className="mia-bubble mia-bubble--user mia-glass mia-rise" data-role="user">
        {message.text}
      </div>
    )
  const display = message.display
  const cardLike = display && display.kind !== 'text'
  const bubbleText = cardLike ? (message.speech ?? '') : message.text
  const speaking = voice.speaking === message.key
  const empty = !message.text && !message.steps.length && !message.approvals.length
  return (
    <div className="mw-reply mia-rise" data-role="assistant">
      <ToolSteps steps={message.steps} />
      {message.approvals.map((approval) => (
        <ApprovalCard key={approval.id} approval={approval} onDecide={chat.decide} compact />
      ))}
      {message.pending && empty && (
        <div className="mia-bubble mia-bubble--assistant mia-glass" role="status">
          <LoaderCircle size={16} className="spin" /> Мия думает…
        </div>
      )}
      {message.pending && !empty && !message.text && (
        <p className="mw-muted" role="status">
          <LoaderCircle size={14} className="spin" /> Работаю…
        </p>
      )}
      {(bubbleText || (!cardLike && message.text)) && (
        <div className="mia-bubble mia-bubble--assistant mia-glass">
          <DisplayView display={cardLike ? null : display} text={bubbleText} />
        </div>
      )}
      {cardLike && <DisplayView display={display} text={message.text} />}
      {message.notes.map((note) => (
        <p key={note} className="mw-muted">
          {note}
        </p>
      ))}
      {message.error && (
        <p className="mw-error" role="alert">
          {message.error}
        </p>
      )}
      {!message.pending && message.text && voice.canSpeak && (
        <button
          className="mw-link mw-speak"
          aria-label={speaking ? 'Остановить озвучку' : 'Озвучить ответ'}
          onClick={() => voice.speak(speechOf(message), message.key)}
        >
          {speaking ? <Square size={14} /> : <Volume2 size={14} />}{' '}
          {speaking ? 'Остановить' : 'Озвучить'}
        </button>
      )}
    </div>
  )
}
