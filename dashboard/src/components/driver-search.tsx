import { Search } from 'lucide-react'
import * as React from 'react'
import type { FleetDriver } from '@/lib/api'

/** Real user feedback: Live Ops needed a quick way to jump straight to a driver/truck instead of
 * hunting for their marker on the map. */
export function DriverSearch({ drivers, onSelect }: { drivers: FleetDriver[]; onSelect: (id: number) => void }) {
  const [query, setQuery] = React.useState('')
  const [open, setOpen] = React.useState(false)

  const matches = query.length === 0
    ? []
    : drivers.filter(
        (d) => `driver ${d.driver_id}`.includes(query.toLowerCase()) || d.truck_number.toLowerCase().includes(query.toLowerCase()),
      )

  return (
    <div className="relative w-56">
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-400" />
      <input
        value={query}
        onChange={(e) => {
          setQuery(e.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        placeholder="Find driver or truck…"
        className="h-8 w-full rounded-lg border border-ink-200 bg-white py-1 pr-2 pl-8 text-xs shadow-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
      />
      {open && matches.length > 0 && (
        <div className="absolute top-full z-30 mt-1 w-full overflow-hidden rounded-lg border border-ink-200 bg-white shadow-lg">
          {matches.slice(0, 8).map((d) => (
            <button
              key={d.driver_id}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                onSelect(d.driver_id)
                setQuery('')
                setOpen(false)
              }}
              className="flex w-full items-center justify-between px-3 py-1.5 text-left text-xs hover:bg-ink-50"
            >
              <span className="font-medium text-ink-800">Driver {d.driver_id}</span>
              <span className="text-ink-400">Truck {d.truck_number} · {d.duty_status}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
