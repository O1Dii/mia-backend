import { Bell, FileText, Sparkles } from 'lucide-react'

export function PluginArt({ kind, compact = false }: { kind: string; compact?: boolean }) {
  return (
    <div className={`plugin-art art-${kind} ${compact ? 'compact-art' : ''}`} aria-hidden="true">
      <div className="art-grain" />
      {kind === 'timer' && (
        <div className="timer-object">
          <div className="timer-dial">
            <span>12:00</span>
            <small>take your time</small>
          </div>
          <i />
        </div>
      )}
      {kind === 'clock' && (
        <>
          <div className="globe-orbit orbit-one" />
          <div className="globe-orbit orbit-two" />
          <div className="globe">
            <div />
            <i />
            <span />
          </div>
          <span className="art-caption">ANYWHERE, TOGETHER</span>
        </>
      )}
      {kind === 'digest' && (
        <div className="digest-stack">
          <div className="paper paper-back" />
          <div className="paper paper-front">
            <span className="paper-badge">
              <Sparkles size={14} /> DAILY BRIEF
            </span>
            <strong>
              Мир.
              <br />В двух словах.
            </strong>
            <i />
            <i />
            <i />
          </div>
          <div className="digest-star">✦</div>
        </div>
      )}
      {kind === 'notes' && (
        <div className="notes-stack">
          <div className="sticky sticky-back" />
          <div className="sticky sticky-front">
            <span>idea no. 042</span>
            <strong>
              А что,
              <br />
              если…
            </strong>
            <i />
          </div>
          <div className="pencil" />
        </div>
      )}
      {kind === 'focus' && (
        <div className="focus-object">
          <div className="focus-ring" />
          <span>
            25<small>MINUTES FOR YOU</small>
          </span>
          <div className="focus-shadow" />
        </div>
      )}
      {kind === 'alarm' && (
        <>
          <div className="alarm-orbit" />
          <div className="alarm-object">
            <Bell size={72} strokeWidth={1.2} />
            <span>07:30</span>
          </div>
          <span className="art-caption">A FRESH START</span>
        </>
      )}
      {kind === 'recipe' && (
        <div className="recipe-art">
          <FileText size={70} strokeWidth={1} />
          <Sparkles size={28} />
        </div>
      )}
    </div>
  )
}
