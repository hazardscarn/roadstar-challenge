import { format } from 'date-fns'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { api, type DispatchBoardData } from '@/lib/api'

// Real pivot (Dispatch Board build): this feed used to read live.quote_requests directly -- that
// whole live-quote-scoring flow is retired (quote_requests is cleared, nothing writes to it
// anymore), so this now shows the SAME day-ahead order book the Dispatch/Orders pages use
// (api.dispatchBoard()), filtered to orders that already have a truck+driver -- "what's assigned/
// dispatched," matching this component's original job of connecting the feed to the map.
interface FeedRow {
  order_id: string
  pickup_city: string
  dest_city: string
  weight_lbs: number
  pallets: number
  load_type: string
  pickup_at: string
  rate: number
  truck_number: string
  driver_id: number
  dispatched: boolean
}

const POLL_MS = 5000

function buildRows(board: DispatchBoardData): FeedRow[] {
  const rows: FeedRow[] = []
  for (const [truckNumber, a] of Object.entries(board.assignments)) {
    if (a.driver_id == null) continue
    for (const orderId of a.order_ids) {
      const order = board.orders.find((o) => o.order_id === orderId)
      if (!order) continue
      rows.push({ ...order, truck_number: truckNumber, driver_id: a.driver_id, dispatched: board.status === 'finalized' })
    }
  }
  return rows.sort((a, b) => a.pickup_at.localeCompare(b.pickup_at))
}

export function DispatchFeed({ onSelectDriver }: { onSelectDriver?: (driverId: number) => void }) {
  const [board, setBoard] = React.useState<DispatchBoardData | null>(null)

  const refresh = React.useCallback(() => {
    api.dispatchBoard().then(setBoard).catch((e) => console.error('dispatch feed poll failed', e))
  }, [])

  React.useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh])

  const rows = board ? buildRows(board) : []

  return (
    <div className="flex flex-col gap-2 p-1">
      {rows.length === 0 && (
        <p className="p-3 text-sm text-ink-400">No orders assigned yet — build tomorrow's plan on the Dispatch page.</p>
      )}
      {rows.map((r) => (
        <button
          key={r.order_id}
          type="button"
          onClick={() => onSelectDriver?.(r.driver_id)}
          className="cursor-pointer rounded-lg border border-ink-200 p-3 text-left text-xs hover:border-brand-300 hover:bg-brand-50/40"
        >
          <div className="mb-1 flex items-center justify-between">
            <span className="font-medium text-ink-800">
              {r.weight_lbs.toLocaleString()} lbs · {r.pallets} plt · {r.load_type}
            </span>
            <Badge tone={r.dispatched ? 'green' : 'amber'}>{r.dispatched ? 'dispatched' : 'assigned'}</Badge>
          </div>
          <div className="text-ink-500">{r.pickup_city} → {r.dest_city}</div>
          <div className="mt-1 flex items-center gap-1.5">
            <span className="font-medium text-ink-700">Driver {r.driver_id}</span>
            <span className="text-ink-400">· Truck {r.truck_number}</span>
          </div>
          <div className="mt-1 flex items-center justify-between text-[11px] text-ink-400">
            <span>Pickup {format(new Date(r.pickup_at), 'MMM d, HH:mm')}</span>
            <span className="text-brand-600">View on map →</span>
          </div>
        </button>
      ))}
    </div>
  )
}
