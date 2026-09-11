// Real user feedback: a fleet manager needs to know what the map colors mean without guessing.
const TRUCK_ITEMS = [
  { color: '#1f9d55', label: 'Driving' },
  { color: '#e0940f', label: 'At dock (loading/unloading)' },
  { color: '#6b7382', label: 'Idle' },
  { color: '#d9342b', label: 'HOS critical / inspection issue' },
]

export function MapLegend() {
  return (
    <div className="absolute bottom-3 left-3 z-[400] rounded-lg border border-ink-200 bg-white/95 px-3 py-2 shadow-sm backdrop-blur">
      <div className="flex flex-col gap-1">
        {TRUCK_ITEMS.map((it) => (
          <div key={it.label} className="flex items-center gap-2 text-[11px] text-ink-600">
            <span className="inline-block size-2.5 rounded-full" style={{ background: it.color }} />
            {it.label}
          </div>
        ))}
      </div>
    </div>
  )
}

const QUOTE_ITEMS: { color: string; label: string; tone: string }[] = [
  { color: '#2a5cdb', label: 'Open — awaiting assignment', tone: 'blue' },
  { color: '#1f9d55', label: 'Assigned', tone: 'green' },
  { color: '#6b7382', label: 'Expired', tone: 'gray' },
]

export function QuoteStatusLegend() {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 border-b border-ink-100 px-1 pb-2 text-[11px] text-ink-500">
      {QUOTE_ITEMS.map((it) => (
        <div key={it.label} className="flex items-center gap-1.5">
          <span className="inline-block size-2 rounded-full" style={{ background: it.color }} />
          {it.label}
        </div>
      ))}
    </div>
  )
}
