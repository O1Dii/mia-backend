import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Link2, LoaderCircle, Star, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import {
  api,
  type CatalogDetail,
  type Installation,
  type Consent,
  type ReviewList,
  type UpdateInfo,
} from '../lib/api'
import { messageOf } from '../lib/http'
import { Link } from '../lib/router'
import { ConsentDialog } from './StoreConsent'
import { ConsentSummary, PluginMark, Stars, categoryTitles, connectAccount } from './StoreShared'

export function PluginPage({ pluginId }: { pluginId: string }) {
  const [detail, setDetail] = useState<CatalogDetail | null>(null)
  const [installation, setInstallation] = useState<Installation | null>(null)
  const [update, setUpdate] = useState<UpdateInfo | null>(null)
  const [reviews, setReviews] = useState<ReviewList | null>(null)
  const [error, setError] = useState('')
  const [dialog, setDialog] = useState<'install' | 'update' | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const [entry, installed, list] = await Promise.all([
        api.plugin(pluginId),
        api.installations(),
        api.reviews(pluginId),
      ])
      const mine = installed.items.find((i) => i.plugin_id === pluginId) ?? null
      setDetail(entry)
      setInstallation(mine)
      setReviews(list)
      setUpdate(
        mine && mine.source === 'catalog' ? await api.updateInfo(mine.id).catch(() => null) : null,
      )
      setError('')
    } catch (e) {
      setError(messageOf(e, 'Не удалось загрузить плагин.'))
    }
  }, [pluginId])
  useEffect(() => {
    void load()
  }, [load])

  async function install(consent: Consent) {
    const created = await api.install(pluginId, consent)
    setDialog(null)
    if (consent.requires_connection) {
      await connectAccount(created.id, `/store/connected?plugin=${encodeURIComponent(pluginId)}`)
      return
    }
    toast.success('Плагин установлен')
    await load()
  }

  async function upgrade(consent: Consent) {
    if (!installation) return
    await api.upgrade(installation, consent)
    setDialog(null)
    toast.success('Плагин обновлён')
    await load()
  }

  async function connect() {
    if (!installation) return
    setBusy(true)
    try {
      await connectAccount(
        installation.id,
        `/store/connected?plugin=${encodeURIComponent(pluginId)}`,
      )
    } catch (e) {
      toast.error(messageOf(e))
      setBusy(false)
    }
  }

  if (error)
    return (
      <section className="mw-page">
        <Link to="/store" className="mw-back">
          <ArrowLeft size={16} /> Магазин
        </Link>
        <p className="mw-error" role="alert">
          {error}
        </p>
      </section>
    )
  if (!detail)
    return (
      <section className="mw-page" role="status">
        <LoaderCircle className="spin" /> Загружаем…
      </section>
    )
  const consent = detail.consent
  return (
    <section className="mw-page" aria-labelledby="plugin-title">
      <Link to="/store" className="mw-back">
        <ArrowLeft size={16} /> Магазин
      </Link>
      <header className="mw-plugin-head">
        <PluginMark entry={detail} size="hero" />
        <div>
          <h1 id="plugin-title">{detail.name}</h1>
          <p className="mw-muted">
            {detail.author ?? 'Автор не указан'} · версия {detail.version} ·{' '}
            {categoryTitles[detail.category] ?? detail.category}
          </p>
          <p>
            <Stars average={detail.rating.average} count={detail.rating.count} /> ·{' '}
            <span className="mw-muted">установок: {detail.installs}</span>
          </p>
          <div className="mw-actions">
            {!installation && (
              <button className="mia-btn mia-btn--primary" onClick={() => setDialog('install')}>
                Установить
              </button>
            )}
            {installation && (
              <span className="mia-chip mia-chip--success" data-testid="installed-chip">
                Установлено · {installation.version}
              </span>
            )}
            {installation?.status === 'needs_connection' && (
              <button
                className="mia-btn mia-btn--primary"
                disabled={busy}
                onClick={() => void connect()}
              >
                <Link2 size={16} /> Подключить аккаунт
              </button>
            )}
            {update?.update_available && (
              <button className="mia-btn mia-btn--secondary" onClick={() => setDialog('update')}>
                Обновить до {update.latest_version}
              </button>
            )}
            {installation && (
              <Link to="/installed" className="mia-btn mia-btn--ghost">
                Управление
              </Link>
            )}
          </div>
        </div>
      </header>

      <p className="mw-lead">{detail.description}</p>

      {detail.screenshots.length > 0 && (
        <ul className="mw-shots" aria-label="Скриншоты">
          {detail.screenshots.map((src) => (
            <li key={src}>
              <img src={src} alt={`${detail.name}: скриншот`} loading="lazy" />
            </li>
          ))}
        </ul>
      )}

      <div className="mia-glass mw-block">
        <ConsentSummary consent={consent} />
      </div>

      <Reviews
        pluginId={pluginId}
        reviews={reviews}
        installed={Boolean(installation)}
        onChange={load}
      />

      {dialog === 'install' && (
        <ConsentDialog
          title={`Установить «${detail.name}»`}
          description="Проверьте, что сможет делать плагин. Вы можете удалить его в любой момент."
          consent={consent}
          action="Установить"
          onConfirm={() => install(consent)}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog === 'update' && update?.consent && (
        <ConsentDialog
          title={`Обновить «${detail.name}»`}
          description={`Версия ${update.installed_version} → ${update.latest_version}. Новые разрешения отмечены.`}
          consent={update.consent}
          added={update.added_grants}
          action="Обновить"
          onConfirm={() => upgrade(update.consent!)}
          onClose={() => setDialog(null)}
        />
      )}
    </section>
  )
}

function Reviews({
  pluginId,
  reviews,
  installed,
  onChange,
}: {
  pluginId: string
  reviews: ReviewList | null
  installed: boolean
  onChange: () => Promise<void>
}) {
  const mine = reviews?.items.find((r) => r.mine)
  const [rating, setRating] = useState(mine?.rating ?? 0)
  const [text, setText] = useState(mine?.text ?? '')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    setRating(mine?.rating ?? 0)
    setText(mine?.text ?? '')
  }, [mine?.rating, mine?.text])
  async function save() {
    setBusy(true)
    try {
      await api.putReview(pluginId, rating, text.trim())
      toast.success('Отзыв сохранён')
      await onChange()
    } catch (e) {
      toast.error(messageOf(e))
    } finally {
      setBusy(false)
    }
  }
  async function remove() {
    setBusy(true)
    try {
      await api.deleteReview(pluginId)
      setRating(0)
      setText('')
      await onChange()
    } catch (e) {
      toast.error(messageOf(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="mw-block">
      <h2 className="mw-section">Отзывы</h2>
      {reviews?.items.length === 0 && (
        <p className="mw-muted">Пока нет отзывов. Первым может быть ваш.</p>
      )}
      <ul className="mw-reviews">
        {reviews?.items.map((review) => (
          <li key={review.id} className="mia-glass">
            <p>
              <strong>{review.author}</strong> · {'★'.repeat(review.rating)}
              {'☆'.repeat(5 - review.rating)}
            </p>
            {review.text && <p>{review.text}</p>}
          </li>
        ))}
      </ul>
      {installed && (
        <form
          className="mia-glass mw-review-form"
          onSubmit={(event) => {
            event.preventDefault()
            if (rating) void save()
          }}
        >
          <fieldset>
            <legend>Ваша оценка</legend>
            <div className="mw-rating" role="radiogroup" aria-label="Оценка">
              {[1, 2, 3, 4, 5].map((value) => (
                <button
                  type="button"
                  key={value}
                  role="radio"
                  aria-checked={rating === value}
                  aria-label={`${value} из 5`}
                  className={rating >= value ? 'is-on' : ''}
                  onClick={() => setRating(value)}
                >
                  <Star size={20} />
                </button>
              ))}
            </div>
          </fieldset>
          <textarea
            aria-label="Текст отзыва"
            placeholder="Что понравилось или не хватило?"
            maxLength={2000}
            rows={3}
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="mw-actions">
            <button className="mia-btn mia-btn--secondary" disabled={busy || !rating}>
              {mine ? 'Обновить отзыв' : 'Отправить отзыв'}
            </button>
            {mine && (
              <button
                type="button"
                className="mia-btn mia-btn--ghost"
                disabled={busy}
                onClick={() => void remove()}
              >
                <Trash2 size={16} /> Удалить
              </button>
            )}
          </div>
        </form>
      )}
      {!installed && <p className="mw-muted">Отзыв можно оставить после установки.</p>}
    </div>
  )
}
