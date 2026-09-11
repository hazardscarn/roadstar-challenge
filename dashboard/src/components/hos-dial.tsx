// Real ELD app convention (Motive/Transflo HOS clocks, see plan's UI design grounding): a center
// dial/ring, green above ~4h, amber under 2h, red and pulsing under 30min. Our live schema only
// carries ONE collapsed hos_remaining_hours number (score_quote.py's documented simplification --
// it's the already-binding minimum across the 5 real HOS clocks, conservative not approximate),
// so the dial shows that single real number honestly, not five fabricated sub-clocks.
const MAX_DRIVING_HOURS = 13 // Canadian HOS daily driving limit -- documents/1788654151601 Section 4

export function HosDial({ hoursRemaining }: { hoursRemaining: number }) {
  const pct = Math.max(0, Math.min(1, hoursRemaining / MAX_DRIVING_HOURS))
  const critical = hoursRemaining < 0.5
  const color = hoursRemaining < 0.5 ? '#d9342b' : hoursRemaining < 2 ? '#e0940f' : '#1f9d55'
  const r = 70
  const circumference = 2 * Math.PI * r

  return (
    <div className="relative flex items-center justify-center">
      <svg width="180" height="180" viewBox="0 0 180 180" className={critical ? 'animate-pulse' : ''}>
        <circle cx="90" cy="90" r={r} fill="none" stroke="var(--color-ink-100)" strokeWidth="14" />
        <circle
          cx="90"
          cy="90"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - pct)}
          transform="rotate(-90 90 90)"
        />
      </svg>
      <div className="absolute flex flex-col items-center">
        <span className="font-display text-3xl font-bold text-ink-900">{hoursRemaining.toFixed(1)}h</span>
        <span className="text-xs text-ink-400">driving left today</span>
      </div>
    </div>
  )
}
