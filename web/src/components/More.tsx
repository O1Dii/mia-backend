import { ChevronRight, Cpu, Layers3, ShieldHalf } from 'lucide-react'
import { Link } from '../lib/router'

const items = [
  {
    to: '/installed',
    icon: Layers3,
    title: 'Мои плагины',
    text: 'Установленные плагины, аккаунты и обновления.',
  },
  {
    to: '/models',
    icon: Cpu,
    title: 'Модели',
    text: 'Ключи и подключения моделей, которыми отвечает Мия.',
  },
  {
    to: '/data',
    icon: ShieldHalf,
    title: 'Где ваши данные',
    text: 'Что хранится, что видит модель и как всё удалить.',
  },
]

export function More() {
  return (
    <section className="mw-page" aria-labelledby="more-title">
      <header className="mw-page__head">
        <h1 id="more-title">Ещё</h1>
        <p>Настройки и данные аккаунта.</p>
      </header>
      <ul className="mw-stack">
        {items.map(({ to, icon: Icon, title, text }) => (
          <li key={to}>
            <Link to={to} className="mia-glass mw-data-row mw-more-link">
              <span className="mw-data-row__icon" aria-hidden="true">
                <Icon size={20} />
              </span>
              <span className="mw-data-row__body">
                <strong>{title}</strong>
                <span className="mw-muted">{text}</span>
              </span>
              <ChevronRight size={18} aria-hidden="true" />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}
