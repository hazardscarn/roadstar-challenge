import { format, formatDistanceToNow } from 'date-fns'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { supabase } from '@/lib/supabase'

// Real user feedback: the feed needs to visibly connect to the map -- who's assigned, what truck,
// what that truck's CURRENT status is (same color convention as the map legend), not just an
// abstract "assigned" pill. Reads live.quote_requests + live.trips + live.driver_status directly
// via supabase-js (RLS-exposed for the manager role, sim/sql/009/032/034/035) -- no backend
// round-trip needed for reads the client is already allowed.
interface QuoteRow {
  quote_id: string
  requested_at: string
  requested_pickup_at: string | null
  origin_location_id: number | null
  dest_location_id: number | null
  weight_lbs: number | null
  pallets: number | null
  load_type: string | null
  status: string
}

interface TripInfo {
  driver_id: number
  truck_number: string
  duty_status: string
}

const POLL_MS = 5000
const STATUS_TONE: Record<string, 'blue' | 'green' | 'gray'> = { open: 'blue', assigned: 'green', expired: 'gray' }
// Same convention as fleet-map.tsx's STATUS_COLOR / map-legend.tsx -- one truck-status vocabulary
// used everywhere in the app, not a second one invented for the feed.
const TRUCK_STATUS_TONE: Record<string, 'green' | 'amber' | 'gray'> = { driving: 'green', on_duty_not_driving: 'amber', off_duty: 'gray' }

export function DispatchFeed({ onSelectDriver }: { onSelectDriver?: (driverId: number) => void }) {
  const [quotes, setQuotes] = React.useState<QuoteRow[]>([])
  const [labels, setLabels] = React.useState<Record<number, string>>({})
  const [tripByQuote, setTripByQuote] = React.useState<Record<string, TripInfo>>({})
  const [onlyAssigned, setOnlyAssigned] = React.useState(false)

  const refresh = React.useCallback(async () => {
    const { data, error } = await supabase
      .schema('live')
      .from('quote_requests')
      .select('quote_id,requested_at,requested_pickup_at,origin_location_id,dest_location_id,weight_lbs,pallets,load_type,status')
      .order('requested_at', { ascending: false })
      .limit(25)
    if (error) {
      console.error('dispatch feed poll failed', error)
      return
    }
    const rows = data as QuoteRow[]
    setQuotes(rows)

    const ids = Array.from(new Set(rows.flatMap((q) => [q.origin_location_id, q.dest_location_id]).filter((x): x is number => x != null)))
    const missing = ids.filter((id) => !(id in labels))
    if (missing.length > 0) {
      const { data: locs } = await supabase.schema('reference').from('locations').select('location_id,label').in('location_id', missing)
      if (locs) setLabels((prev) => ({ ...prev, ...Object.fromEntries(locs.map((l) => [l.location_id, l.label as string])) }))
    }

    const assignedQuoteIds = rows.filter((q) => q.status === 'assigned').map((q) => q.quote_id)
    if (assignedQuoteIds.length > 0) {
      const { data: trips } = await supabase.schema('live').from('trips').select('quote_id,driver_id').in('quote_id', assignedQuoteIds)
      if (trips && trips.length > 0) {
        const driverIds = Array.from(new Set(trips.map((t) => t.driver_id as number)))
        const { data: statuses } = await supabase.schema('live').from('driver_status').select('driver_id,truck_number,duty_status').in('driver_id', driverIds)
        const statusByDriver = new Map((statuses ?? []).map((s) => [s.driver_id, s]))
        const next: Record<string, TripInfo> = {}
        for (const t of trips) {
          const s = statusByDriver.get(t.driver_id as number)
          if (s) next[t.quote_id as string] = { driver_id: t.driver_id as number, truck_number: s.truck_number as string, duty_status: s.duty_status as string }
        }
        setTripByQuote(next)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [labels])

  React.useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function handleClick(q: QuoteRow) {
    const trip = tripByQuote[q.quote_id]
    if (trip && onSelectDriver) onSelectDriver(trip.driver_id)
  }

  const visible = onlyAssigned ? quotes.filter((q) => q.status === 'assigned') : quotes

  return (
    <div className="flex flex-col gap-2 p-1">
      <label className="flex items-center gap-1.5 px-2 text-[11px] text-ink-500">
        <input type="checkbox" checked={onlyAssigned} onChange={(e) => setOnlyAssigned(e.target.checked)} />
        Only show assigned (on the map)
      </label>
      {visible.length === 0 && <p className="p-3 text-sm text-ink-400">Nothing here yet — submit a quote from Dispatch.</p>}
      {visible.map((q) => {
        const trip = tripByQuote[q.quote_id]
        return (
          <button
            key={q.quote_id}
            type="button"
            onClick={() => handleClick(q)}
            disabled={!trip}
            className="rounded-lg border border-ink-200 p-3 text-left text-xs enabled:cursor-pointer enabled:hover:border-brand-300 enabled:hover:bg-brand-50/40 disabled:cursor-default"
          >
            <div className="mb-1 flex items-center justify-between">
              <span className="font-medium text-ink-800">
                {q.weight_lbs?.toLocaleString()} lbs · {q.pallets} plt · {q.load_type}
              </span>
              <Badge tone={STATUS_TONE[q.status] ?? 'gray'}>{q.status}</Badge>
            </div>
            <div className="text-ink-500">
              {labels[q.origin_location_id ?? -1] ?? '…'} → {labels[q.dest_location_id ?? -1] ?? '…'}
            </div>
            {trip && (
              <div className="mt-1 flex items-center gap-1.5">
                <span className="font-medium text-ink-700">Driver {trip.driver_id}</span>
                <span className="text-ink-400">· Truck {trip.truck_number}</span>
                <Badge tone={TRUCK_STATUS_TONE[trip.duty_status] ?? 'gray'}>{trip.duty_status}</Badge>
              </div>
            )}
            <div className="mt-1 flex items-center justify-between text-[11px] text-ink-400">
              <span>
                Requested {format(new Date(q.requested_at), 'MMM d, h:mm a')} ({formatDistanceToNow(new Date(q.requested_at), { addSuffix: true })})
              </span>
              {trip && <span className="text-brand-600">View on map →</span>}
            </div>
            {q.requested_pickup_at && (
              <div className="text-[11px] text-ink-400">Pickup requested for {format(new Date(q.requested_pickup_at), 'MMM d, h:mm a')}</div>
            )}
          </button>
        )
      })}
    </div>
  )
}
