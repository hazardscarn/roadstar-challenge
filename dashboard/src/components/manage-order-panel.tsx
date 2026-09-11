import { format } from 'date-fns'
import { Loader2, Search, X } from 'lucide-react'
import * as React from 'react'
import { CandidateDetail } from '@/components/candidate-detail'
import { QuoteCandidateCard } from '@/components/quote-candidate-card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { api, type FleetDriver, type LocationOption, type OrderDetail, type ScoreQuoteResult, type ScoreQuoteRow } from '@/lib/api'

const LOAD_TYPES = ['Dry Van', 'Reefer', 'Flatbed']
const STATUS_TONE: Record<string, 'gray' | 'blue' | 'green' | 'amber' | 'red'> = {
  open: 'blue', assigned: 'green', expired: 'gray', cancelled: 'red',
  in_transit: 'green', at_pickup: 'amber', at_delivery: 'amber', completed: 'gray',
}

// Real user feedback: the manager needs a way to edit an existing order (pickup time/weight/
// pallets/load type changed), re-run the real scoring pipeline, and reassign -- or cancel it
// outright -- from inside Dispatch, not a separate page. Either action frees the driver/truck
// (main.py's _release_assignment(), the same "don't clobber a newer commitment" guard the sim
// engine's own event loop uses) and finds a fresh candidate the normal way.
export function ManageOrderPanel({ drivers, initialId }: { drivers: FleetDriver[]; initialId?: string }) {
  const [idInput, setIdInput] = React.useState(initialId ?? '')
  const [loading, setLoading] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [order, setOrder] = React.useState<OrderDetail | null>(null)

  const [pickupAt, setPickupAt] = React.useState('')
  const [weight, setWeight] = React.useState('')
  const [pallets, setPallets] = React.useState('')
  const [loadType, setLoadType] = React.useState('Dry Van')

  const [rescoring, setRescoring] = React.useState(false)
  const [cancelling, setCancelling] = React.useState(false)
  const [result, setResult] = React.useState<ScoreQuoteResult | null>(null)
  const [activeCandidate, setActiveCandidate] = React.useState<ScoreQuoteRow | null>(null)
  const [assigningDriverId, setAssigningDriverId] = React.useState<number | null>(null)

  async function lookup(id: string) {
    if (!id.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    setActiveCandidate(null)
    try {
      const detail = await api.getOrder(id.trim())
      setOrder(detail)
      setPickupAt(format(new Date(detail.quote.requested_pickup_at), "yyyy-MM-dd'T'HH:mm"))
      setWeight(String(detail.quote.weight_lbs))
      setPallets(String(detail.quote.pallets))
      setLoadType(detail.quote.load_type)
    } catch (err) {
      setOrder(null)
      setError(err instanceof Error ? err.message : 'Order not found')
    } finally {
      setLoading(false)
    }
  }

  function handleLookup(e: React.FormEvent) {
    e.preventDefault()
    lookup(idInput)
  }

  // Arrived here via Orders' "Edit" link (?manage=<quote_id>) -- run the lookup immediately
  // instead of making the manager click Search on an ID that's already filled in.
  React.useEffect(() => {
    if (initialId) lookup(initialId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialId])

  async function handleRescore() {
    if (!order) return
    setRescoring(true)
    setError(null)
    try {
      const res = await api.rescoreOrder(order.quote.quote_id, {
        requested_pickup_at: new Date(pickupAt).toISOString(),
        weight_lbs: Number(weight),
        pallets: Number(pallets),
        load_type: loadType,
      })
      setResult(res)
      if (res.top_n[0]) setActiveCandidate(res.top_n[0])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Re-score failed')
    } finally {
      setRescoring(false)
    }
  }

  async function handleCancel() {
    if (!order) return
    setCancelling(true)
    setError(null)
    try {
      await api.cancelOrder(order.quote.quote_id)
      setOrder({ ...order, quote: { ...order.quote, status: 'cancelled' }, trip: order.trip ? { ...order.trip, status: 'cancelled' } : null })
      setResult(null)
      setActiveCandidate(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Cancel failed')
    } finally {
      setCancelling(false)
    }
  }

  async function handleAssign(driverId: number) {
    if (!result) return
    setAssigningDriverId(driverId)
    try {
      await api.assign(result.quote_id, driverId)
      const detail = await api.getOrder(result.quote_id)
      setOrder(detail)
      setResult(null)
      setActiveCandidate(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Assign failed')
    } finally {
      setAssigningDriverId(null)
    }
  }

  const originOpt: LocationOption | null = order
    ? { location_id: order.quote.origin_location_id, label: order.quote.origin_location_id_label ?? '—', city: '', tier: '' }
    : null
  const destOpt: LocationOption | null = order
    ? { location_id: order.quote.dest_location_id, label: order.quote.dest_location_id_label ?? '—', city: '', tier: '' }
    : null
  const isTerminal = order?.quote.status === 'cancelled' || order?.trip?.status === 'completed'

  return (
    <div className="flex flex-1 overflow-hidden">
      <div className="flex w-[420px] shrink-0 flex-col overflow-auto border-r border-ink-200 bg-white p-4">
        <form onSubmit={handleLookup} className="flex gap-2">
          <Input placeholder="Quote ID or Trip ID" value={idInput} onChange={(e) => setIdInput(e.target.value)} />
          <Button type="submit" disabled={loading} size="icon" variant="outline">
            {loading ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
          </Button>
        </form>
        {error && <p className="mt-2 text-xs text-status-red-500">{error}</p>}

        {order && (
          <div className="mt-4 flex flex-col gap-3">
            <div className="rounded-lg border border-ink-200 p-3 text-xs">
              <div className="mb-1.5 flex items-center gap-2">
                <span className="font-mono text-[11px] text-ink-400">{order.quote.quote_id.slice(0, 8)}</span>
                <Badge tone={order.quote.service_type === 'LTL' ? 'amber' : 'blue'}>{order.quote.service_type}</Badge>
                <Badge tone={STATUS_TONE[order.quote.status] ?? 'gray'}>{order.quote.status}</Badge>
                {order.trip && <Badge tone={STATUS_TONE[order.trip.status] ?? 'gray'}>trip: {order.trip.status}</Badge>}
              </div>
              <div className="text-ink-600">{order.quote.origin_location_id_label} → {order.quote.dest_location_id_label}</div>
              <div className="mt-1 text-ink-400">Quote time: {format(new Date(order.quote.requested_at), 'MMM d, h:mm a')}</div>
              {order.trip && (
                <div className="mt-1 text-ink-400">
                  Driver {order.trip.driver_id} · Truck {order.trip.truck_number ?? '—'} · {order.trip.duty_status ?? '—'}
                </div>
              )}
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1.5">
                <Label>Weight (lbs)</Label>
                <Input type="number" value={weight} onChange={(e) => setWeight(e.target.value)} disabled={isTerminal} />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Pallets</Label>
                <Input type="number" value={pallets} onChange={(e) => setPallets(e.target.value)} disabled={isTerminal} />
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>Load type</Label>
              <Select value={loadType} onValueChange={setLoadType} disabled={isTerminal}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {LOAD_TYPES.map((lt) => (
                    <SelectItem key={lt} value={lt}>{lt}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>Requested pickup</Label>
              <Input type="datetime-local" value={pickupAt} onChange={(e) => setPickupAt(e.target.value)} disabled={isTerminal} />
            </div>

            {isTerminal ? (
              <p className="rounded-md bg-ink-50 px-3 py-2 text-xs text-ink-400">
                This order is {order.quote.status === 'cancelled' ? 'cancelled' : 'already completed'} — nothing left to edit.
              </p>
            ) : (
              <div className="flex gap-2">
                <Button className="flex-1" disabled={rescoring} onClick={handleRescore}>
                  {rescoring ? <Loader2 className="size-4 animate-spin" /> : 'Re-score & reassign'}
                </Button>
                <Button variant="outline" disabled={cancelling} onClick={handleCancel}>
                  {cancelling ? <Loader2 className="size-4 animate-spin" /> : <X className="size-4" />}
                  Cancel
                </Button>
              </div>
            )}

            {result && (
              <div className="mt-1 flex flex-col gap-2">
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-ink-500">
                  <span>{result.summary.n_feasible} feasible</span>
                  <span>·</span>
                  <span>{result.summary.n_mid_route_candidates} mid-route competing</span>
                </div>
                {result.summary.estimated_total_charge != null && (
                  <div className="rounded-lg border border-ink-200 bg-ink-50 p-3 text-xs">
                    <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                      Updated quote — {result.summary.loaded_miles} mi
                    </p>
                    <span className="font-semibold text-brand-600">${result.summary.estimated_total_charge?.toFixed(2)}</span> est. total (pre-tax)
                  </div>
                )}
                {result.top_n.map((row) => (
                  <QuoteCandidateCard
                    key={row.driver_id}
                    row={row}
                    requestedPickupAt={result.summary.requested_pickup_at}
                    assigning={assigningDriverId !== null}
                    isAssigningThis={assigningDriverId === row.driver_id}
                    onAssign={() => handleAssign(row.driver_id)}
                    selected={activeCandidate?.driver_id === row.driver_id}
                    onSelect={() => setActiveCandidate(row)}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-auto p-4">
        {!activeCandidate || !originOpt || !destOpt ? (
          <div className="flex h-full items-center justify-center text-sm text-ink-400">
            Look up an order, then re-score to see the new candidate plan here.
          </div>
        ) : (
          <CandidateDetail
            candidate={activeCandidate}
            origin={originOpt}
            dest={destOpt}
            driver={drivers.find((d) => d.driver_id === activeCandidate.driver_id)}
          />
        )}
      </div>
    </div>
  )
}
