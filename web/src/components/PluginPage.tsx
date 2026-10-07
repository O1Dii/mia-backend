import { useEffect, useState } from 'react'
import {
  ArrowLeft,
  ArrowUpRight,
  Check,
  Download,
  MessageSquare,
  Plus,
  ShieldCheck,
  Star,
  Trash2,
} from 'lucide-react'
import { api, webPath, type Reviews } from '../lib/api'
import { useAuth } from '../lib/auth'
import type { Plugin } from '../plugins/catalog'
import { permissions } from '../plugins/catalog'
import { pluginDetails } from '../plugins/details'
import { PluginArt } from './PluginArt'

export function PluginPage({
  plugin,
  installed,
  onInstall,
  onOpen,
}: {
  plugin?: Plugin
  installed: boolean
  onInstall: (plugin: Plugin) => void
  onOpen: (plugin: Plugin) => void
}) {
  const auth = useAuth()
  const [reviews, setReviews] = useState<Reviews | null>(null)
  const [error, setError] = useState('')
  const [rating, setRating] = useState(5)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [shot, setShot] = useState(0)
  useEffect(() => {
    setReviews(null)
    setError('')
    setShot(0)
    setText('')
    if (!plugin || !plugin.id.startsWith('mia.')) return
    let active = true
    api
      .reviews(plugin.id)
      .then((data) => {
        if (active) {
          setReviews(data)
          const mine = data.items.find((r) => r.mine)
          if (mine) {
            setRating(mine.rating)
            setText(mine.text)
          }
        }
      })
      .catch((e) => {
        if (active) setError(e.message)
      })
    return () => {
      active = false
    }
  }, [plugin?.id])
  if (!plugin)
    return (
      <div className="empty-state">
        <h1>Плагин не найден</h1>
        <a href="#store" className="button secondary">
          Вернуться в магазин
        </a>
      </div>
    )
  const details = pluginDetails[plugin.id]
  const screenshots = details
    ? [
        webPath(`/screenshots/${plugin.id.slice(4)}-widget.png`),
        webPath(`/screenshots/${plugin.id.slice(4)}-dashboard.png`),
      ]
    : []
  const author = plugin.manifest?.author ?? 'Команда Мия'
  return (
    <div className="plugin-page">
      <a href="#store" className="back-link">
        <ArrowLeft size={16} /> Магазин плагинов
      </a>
      <section className={`plugin-detail-hero detail-${plugin.color}`}>
        <div className="detail-art">
          <PluginArt kind={plugin.art} />
        </div>
        <div className="detail-intro">
          <span className="eyebrow">
            {plugin.category} · {author}
          </span>
          <h1>{plugin.name}</h1>
          <p>{plugin.tagline}</p>
          <div className="detail-facts">
            <span>
              <Star size={15} />
              {reviews?.count
                ? `${reviews.average?.toFixed(1)} · ${reviews.count} отзывов`
                : 'Пока без оценок'}
            </span>
            <span>Бесплатно</span>
            <span>{plugin.manifest?.version ?? 'Web preview'}</span>
          </div>
          <button
            className="button primary"
            onClick={() => (installed ? onOpen(plugin) : onInstall(plugin))}
          >
            {installed ? <Check size={17} /> : <Plus size={17} />}{' '}
            {installed ? 'Открыть плагин' : 'Добавить в пространство'}
            <ArrowUpRight size={16} />
          </button>
        </div>
      </section>
      <div className="plugin-detail-layout">
        <div className="detail-main">
          {screenshots.length > 0 && (
            <section className="detail-section">
              <div className="section-heading">
                <h2>Как это выглядит</h2>
                <span className="muted">Скриншоты web-версии</span>
              </div>
              <a
                className="screenshot-stage"
                href={screenshots[shot]}
                target="_blank"
                rel="noopener noreferrer"
              >
                <img
                  src={screenshots[shot]}
                  alt={`${plugin.name}: ${shot === 0 ? 'интерфейс плагина' : 'виджет на дашборде'}`}
                />
              </a>
              <div className="screenshot-tabs">
                {['Интерфейс плагина', 'На дашборде'].map((label, index) => (
                  <button
                    className={`chip ${index === shot ? 'selected' : ''}`}
                    key={label}
                    onClick={() => setShot(index)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </section>
          )}
          <section className="detail-section">
            <h2>Меньше рутины в вашем дне</h2>
            <p>{details?.about ?? plugin.description}</p>
            <ul className="feature-list">
              {(details?.features ?? plugin.manifest?.actions.map((a) => a.description) ?? []).map(
                (feature) => (
                  <li key={feature}>
                    <Check size={16} />
                    {feature}
                  </li>
                ),
              )}
            </ul>
          </section>
          {details && (
            <section className="detail-section">
              <h2>Просто попросите Мию</h2>
              <div className="example-commands">
                {details.examples.map((example) => (
                  <div key={example}>
                    <MessageSquare size={16} />
                    <span>«{example}»</span>
                  </div>
                ))}
              </div>
              <p className="field-note">
                Мия передаст запрос вашей выбранной модели с навыками установленного плагина.
              </p>
            </section>
          )}
          <section className="detail-section reviews-section">
            <div className="section-heading">
              <h2>Отзывы пользователей</h2>
              <span className="muted">{reviews?.count ?? 0} отзывов</span>
            </div>
            {!details ? (
              <p>Отзывы появятся после публикации приложения в общем каталоге.</p>
            ) : (
              <>
                {reviews?.items.length === 0 && (
                  <div className="reviews-empty">
                    <MessageSquare size={26} />
                    <h3>Первый опыт — ваш</h3>
                    <p>Здесь пока нет отзывов. Попробуйте плагин и расскажите, что получилось.</p>
                  </div>
                )}
                {reviews?.items.map((review, index) => (
                  <article className="review-card" key={`${review.updated_at}-${index}`}>
                    <header>
                      <strong>
                        {review.author}
                        {review.mine ? ' · ваш отзыв' : ''}
                      </strong>
                      <span aria-label={`${review.rating} из 5`}>
                        {'★'.repeat(review.rating)}
                        {'☆'.repeat(5 - review.rating)}
                      </span>
                    </header>
                    <p>{review.text}</p>
                    <small>{new Date(review.updated_at).toLocaleDateString('ru')}</small>
                    {review.mine && (
                      <button
                        className="text-button"
                        aria-label="Удалить мой отзыв"
                        disabled={busy}
                        onClick={async () => {
                          setBusy(true)
                          try {
                            setReviews(await api.deleteReview(plugin.id))
                            setText('')
                          } catch (e) {
                            setError(e instanceof Error ? e.message : 'Ошибка')
                          } finally {
                            setBusy(false)
                          }
                        }}
                      >
                        <Trash2 size={14} /> Удалить
                      </button>
                    )}
                  </article>
                ))}
                {auth.user && installed ? (
                  <form
                    className="review-form"
                    onSubmit={async (e) => {
                      e.preventDefault()
                      setBusy(true)
                      setError('')
                      try {
                        setReviews(await api.review(plugin.id, rating, text))
                      } catch (e) {
                        setError(e instanceof Error ? e.message : 'Ошибка')
                      } finally {
                        setBusy(false)
                      }
                    }}
                  >
                    <h3>{reviews?.items.some((r) => r.mine) ? 'Изменить отзыв' : 'Ваш отзыв'}</h3>
                    <label>
                      Оценка
                      <select
                        aria-label="Оценка плагина"
                        value={rating}
                        onChange={(e) => setRating(Number(e.target.value))}
                      >
                        {[5, 4, 3, 2, 1].map((n) => (
                          <option key={n} value={n}>
                            {n} из 5
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Что понравилось, а что можно улучшить?
                      <textarea
                        aria-label="Текст отзыва"
                        maxLength={2000}
                        minLength={5}
                        required
                        rows={4}
                        value={text}
                        onChange={(e) => setText(e.target.value)}
                      />
                    </label>
                    <p className="field-note">
                      Отзыв и ваше имя из Google будут видны другим пользователям. Email не
                      публикуется.
                    </p>
                    <button className="button primary" disabled={busy || text.trim().length < 5}>
                      {busy ? 'Сохраняем…' : 'Опубликовать отзыв'}
                    </button>
                  </form>
                ) : (
                  <p className="field-note">
                    {auth.user
                      ? 'Установите плагин, чтобы оставить отзыв.'
                      : 'Отзывы доступны после входа через Google.'}
                  </p>
                )}
              </>
            )}
            {error && (
              <p className="error-message" role="alert">
                {error}
              </p>
            )}
          </section>
        </div>
        <aside className="detail-sidebar">
          <div className="detail-side-card">
            <h3>
              <ShieldCheck size={18} /> Возможности и доступ
            </h3>
            {plugin.permissions.map((permission) => (
              <p key={permission}>{permissions[permission] ?? permission}</p>
            ))}
            <p className="field-note">Ключи провайдера не передаются автору плагина.</p>
          </div>
          <div className="detail-side-card">
            <h3>Перед установкой</h3>
            <p>
              {details?.limitation ??
                'Это локально импортированное приложение автора. Проверьте manifest и разрешения перед установкой.'}
            </p>
            <p>Интерфейс: web · виджет дашборда</p>
            {plugin.id === 'mia.digest' && (
              <a href="#plugin/mia.web" className="text-button">
                Зависимость: Интернет →
              </a>
            )}
            {plugin.id === 'mia.timer' && (
              <a className="button secondary" href={webPath('/api/web/downloads/timer.zip')}>
                <Download size={16} /> Приложение с SDK
              </a>
            )}
            {plugin.manifest?.downloads.map((download) => (
              <a
                key={download.url}
                href={download.url}
                target="_blank"
                rel="noopener noreferrer"
                className="button secondary"
              >
                {download.label} ↗
              </a>
            ))}
          </div>
        </aside>
      </div>
    </div>
  )
}
