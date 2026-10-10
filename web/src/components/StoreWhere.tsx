import { Cloud, Smartphone } from 'lucide-react'
import type { Consent, DeviceCapability } from '../lib/api'

const kindTitle: Record<DeviceCapability['kind'], string> = {
  read: 'читает данные телефона',
  action: 'действие на телефоне',
  external: 'затрагивает других людей',
}

/** What the plugin may ask of the phone: each capability with a line, never without consent. */
export function DeviceCapabilities({ consent }: { consent: Consent }) {
  const capabilities = consent.device_capabilities ?? []
  if (capabilities.length === 0) return null
  return (
    <section className="mw-device" aria-label="Возможности устройства">
      <h3>Возможности устройства</h3>
      <p className="mw-muted">
        Плагин только просит; телефон спросит у вас разрешения, и вы можете отключить любую
        возможность в настройках приложения.
      </p>
      <ul className="mw-list">
        {capabilities.map((item) => (
          <li key={item.capability} data-capability={item.capability}>
            <Smartphone size={14} aria-hidden="true" />
            <strong>{item.label}</strong>
            <span className={`mia-chip ${item.kind === 'external' ? 'mia-chip--warning' : ''}`}>
              {kindTitle[item.kind]}
            </span>
            <span className="mia-chip">{item.optional ? 'по желанию' : 'нужно для работы'}</span>
            <small>{item.description}</small>
            <small>Модель получит: {item.model_sees}.</small>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** «Где работает»: the cloud part and the part that may run on the phone, by request. */
export function WhereItRuns({ consent }: { consent: Consent }) {
  const where = consent.where_it_runs
  if (!where) return null
  return (
    <section className="mia-glass mw-block mw-where" aria-labelledby="where-title">
      <h2 id="where-title" className="mw-section">
        Где работает
      </h2>
      <ul className="mw-list mw-where__list">
        <li data-where="cloud">
          <Cloud size={16} aria-hidden="true" />
          <strong>В облаке Mia</strong>
          <small>{where.cloud}</small>
        </li>
        <li data-where="device">
          <Smartphone size={16} aria-hidden="true" />
          <strong>На телефоне — по запросу</strong>
          <small>
            {where.device ?? 'Телефон не участвует: плагин ничего не делает на устройстве.'}
          </small>
        </li>
      </ul>
      <DeviceCapabilities consent={consent} />
    </section>
  )
}
