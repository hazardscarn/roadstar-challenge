import { Loader2, MapPinPlus } from 'lucide-react'
import * as React from 'react'
import { Input } from '@/components/ui/input'
import { api, type LocationOption } from '@/lib/api'

export function LocationPicker({
  label,
  value,
  onChange,
}: {
  label: string
  value: LocationOption | null
  onChange: (loc: LocationOption | null) => void
}) {
  const [query, setQuery] = React.useState('')
  const [options, setOptions] = React.useState<LocationOption[]>([])
  const [open, setOpen] = React.useState(false)
  const [geocoding, setGeocoding] = React.useState(false)
  const [geocodeError, setGeocodeError] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (!open || query.length < 2) {
      setOptions([])
      return
    }
    const handle = setTimeout(() => {
      api.locations(query).then(setOptions).catch(() => setOptions([]))
    }, 200)
    return () => clearTimeout(handle)
  }, [query, open])

  async function handleAddNew() {
    setGeocoding(true)
    setGeocodeError(null)
    try {
      const loc = await api.geocode(query)
      onChange(loc)
      setOpen(false)
    } catch (err) {
      setGeocodeError(err instanceof Error ? err.message.replace(/^\d+\s/, '') : 'Could not find that address')
    } finally {
      setGeocoding(false)
    }
  }

  return (
    <div className="relative flex flex-col gap-1.5">
      <label className="text-xs font-medium text-ink-600">{label}</label>
      {value ? (
        <button
          type="button"
          onClick={() => {
            onChange(null)
            setQuery('')
            setOpen(true)
          }}
          className="flex h-9 w-full items-center justify-between rounded-lg border border-ink-200 bg-white px-3 text-left text-sm"
        >
          <span className="truncate">{value.label}</span>
          <span className="text-xs text-ink-400">change</span>
        </button>
      ) : (
        <Input
          placeholder="Search, or type any Southern Ontario address…"
          value={query}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setQuery(e.target.value)
            setGeocodeError(null)
          }}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
        />
      )}
      {open && !value && (options.length > 0 || query.length >= 4) && (
        <div className="absolute top-full z-20 mt-1 max-h-64 w-full overflow-auto rounded-lg border border-ink-200 bg-white shadow-lg">
          {options.map((opt) => (
            <button
              key={opt.location_id}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                onChange(opt)
                setOpen(false)
              }}
              className="flex w-full flex-col items-start px-3 py-2 text-left text-sm hover:bg-ink-50"
            >
              <span className="truncate font-medium text-ink-800">{opt.label}</span>
              <span className="text-xs text-ink-400">
                {opt.city} {opt.tier === 'terminal_hub' && '· Terminal Hub'}
              </span>
            </button>
          ))}
          {query.length >= 4 && (
            <button
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={handleAddNew}
              disabled={geocoding}
              className="flex w-full items-center gap-2 border-t border-ink-100 px-3 py-2 text-left text-sm text-brand-600 hover:bg-brand-50"
            >
              {geocoding ? <Loader2 className="size-4 animate-spin" /> : <MapPinPlus className="size-4" />}
              Use "{query}" as a new address
            </button>
          )}
          {geocodeError && <p className="border-t border-ink-100 px-3 py-2 text-xs text-status-red-500">{geocodeError}</p>}
        </div>
      )}
    </div>
  )
}
