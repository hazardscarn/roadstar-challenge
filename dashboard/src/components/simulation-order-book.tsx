import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { supabase } from '@/lib/supabase'
import type { SimTrip } from '@/lib/simulation-api'
import { formatSimTime } from '@/lib/utils'

// Real user feedback: the Simulation Showcase needs "the orderbook on the left and the selection
// candidates" -- order book status colors (completed=green, lost opportunity=red, ongoing=amber,
// yet to start=grey), and the candidate panel should auto-advance to the newest order as playback
// proceeds ("this order came in -> N candidates found -> best candidate selected and assigned"),
// not require a manual click every time. Candidates are read directly from the `simulation`
// schema (sim/sql/038), the run's own real persisted rows, not client-side JSON already held in
// memory -- proof the "table logging" the user asked to see is real, not an animation.

interface QuoteRow {
  quote_id: string
  origin_location_id: number
  dest_location_id: number
  requested_at: string
  requested_pickup_at: string
  weight_lbs: number
  pallets: number
  load_type: string
  service_type: string
  status: string
}

interface CandidateRow {
  driver_id: number
  truck_number: string
  hos_remaining_hours: number | null
  truck_breakdown_risk: number | null
  deadhead_miles: number | null
  score: number | null
  rank: number | null
  was_assigned: boolean
}

type OrderStatus = 'completed' | 'ongoing' | 'lost' | 'pending'

const STATUS_STYLE: Record<OrderStatus, { dot: string; label: string }> = {
  completed: { dot: 'bg-status-green-500', label: 'completed' },
  ongoing: { dot: 'bg-status-amber-500', label: 'ongoing' },
  lost: { dot: 'bg-status-red-500', label: 'lost opportunity' },
  pending: { dot: 'bg-ink-300', label: 'yet to start' },
}

function statusOf(order: QuoteRow, trip: SimTrip | undefined, cursorSeconds: number): OrderStatus {
  if (!trip) return order.status === 'expired' ? 'lost' : 'pending'
  if (cursorSeconds >= trip.completed_at_s) return 'completed'
  if (cursorSeconds >= trip.assigned_at_s) return 'ongoing'
  return 'pending'
}

export function SimulationOrderBook({
  runId, cursorSeconds, simStart, trips, playing, onSelectOrder, onViewStory,
}: {
  runId: string
  cursorSeconds: number
  simStart: string
  trips: SimTrip[]
  playing: boolean
  /** Fires whenever an order becomes selected (auto-advance or manual click). `manual` is false
   * for the auto-narration advance (fires continuously through playback as new orders are
   * revealed) and true only for a real click -- real bug found directly: the parent was using
   * this to pan/zoom the map (`focusFitTo`), so the auto-advance alone caused the exact "auto
   * zoom and drop" the map-bounds fix further up was supposed to have already killed. The parent
   * now only moves the map on a genuine `manual` selection. */
  onSelectOrder?: (quoteId: string, manual: boolean) => void
  /** Opens the full "Order Story" drill-in for the given order. */
  onViewStory?: (quoteId: string) => void
}) {
  const [orders, setOrders] = React.useState<QuoteRow[]>([])
  const [labels, setLabels] = React.useState<Record<number, string>>({})
  const [selectedId, setSelectedId] = React.useState<string | null>(null)
  const [manualSelect, setManualSelect] = React.useState(false)
  const [candidates, setCandidates] = React.useState<CandidateRow[]>([])
  const [loadingCandidates, setLoadingCandidates] = React.useState(false)
  // Real bug found from a screenshot: "Run AI Dispatch" replays sim/live/ai_dispatch_replay.py,
  // which never writes simulation.quote_requests/quote_candidate_snapshots -- those tables are
  // only ever populated by the older per-quote showcase simulator. So this panel's own query
  // legitimately comes back empty for every AI-dispatch run, and the book showed "0 of 0" forever
  // instead of the real trips already sitting in the `trips` prop. Falls back to building the
  // book directly from that real, already-loaded trip data instead of leaving it blank.
  const [usingTripFallback, setUsingTripFallback] = React.useState(false)
  const listRef = React.useRef<HTMLDivElement>(null)

  // Real bug found while wiring the fallback below: every AI-dispatch trip is persisted with
  // quote_id = "" (dashboard/server/main.py's _ai_dispatch_trip_row_to_sim_trip hardcodes it --
  // that table has no quote concept at all). An empty string is not a real per-trip key, so it
  // collapses every trip onto one map entry; trip_id (a real per-trip UUID in both flows) is used
  // instead whenever quote_id is blank.
  const tripByQuoteId = React.useMemo(() => new Map(trips.map((t) => [t.quote_id || t.trip_id, t])), [trips])

  React.useEffect(() => {
    ;(async () => {
      const { data } = await supabase
        .schema('simulation')
        .from('quote_requests')
        .select('quote_id,origin_location_id,dest_location_id,requested_at,requested_pickup_at,weight_lbs,pallets,load_type,service_type,status')
        .eq('run_id', runId)
        .order('requested_at', { ascending: true })
      let rows = (data as unknown as QuoteRow[]) ?? []

      if (rows.length === 0 && trips.length > 0) {
        // Real AI-dispatch trip -- everything the book needs (origin/dest, weight, pallets,
        // load_type) is already on the trip itself. There's no separate "requested_at" moment for
        // a whole-day plan built in advance, so the order's own assignment time (the same instant
        // its truck starts driving to it on the map) stands in as the reveal point.
        rows = [...trips]
          .sort((a, b) => a.assigned_at_s - b.assigned_at_s)
          .map((t): QuoteRow => ({
            quote_id: t.quote_id || t.trip_id,
            origin_location_id: t.origin_location_id,
            dest_location_id: t.dest_location_id,
            requested_at: new Date(new Date(simStart).getTime() + t.assigned_at_s * 1000).toISOString(),
            requested_pickup_at: new Date(new Date(simStart).getTime() + (t.arr_pickup_at_s ?? t.assigned_at_s) * 1000).toISOString(),
            weight_lbs: t.weight_lbs,
            pallets: t.pallets,
            load_type: t.load_type,
            service_type: 'FTL', // real -- the dispatch board / CP-SAT solver only ever assigns FTL orders
            status: 'assigned',
          }))
        setUsingTripFallback(true)
      } else {
        setUsingTripFallback(false)
      }
      setOrders(rows)

      const locIds = Array.from(new Set(rows.flatMap((r) => [r.origin_location_id, r.dest_location_id])))
      if (locIds.length) {
        const { data: locs } = await supabase.schema('reference').from('locations').select('location_id,label').in('location_id', locIds)
        if (locs) setLabels(Object.fromEntries(locs.map((l) => [l.location_id, l.label as string])))
      }
    })()
  }, [runId, trips, simStart])

  const cursorTime = React.useMemo(() => new Date(new Date(simStart).getTime() + cursorSeconds * 1000), [simStart, cursorSeconds])
  const visibleOrders = orders.filter((o) => new Date(o.requested_at) <= cursorTime)

  const selectOrder = React.useCallback(async (quoteId: string, manual: boolean) => {
    setSelectedId(quoteId)
    onSelectOrder?.(quoteId, manual)
    // AI-dispatch trips have no quote_candidate_snapshots rows at all (see the fallback note
    // above) -- skip the query entirely rather than firing a request that's known to come back empty.
    if (usingTripFallback) {
      setCandidates([])
      return
    }
    setLoadingCandidates(true)
    const { data } = await supabase
      .schema('simulation')
      .from('quote_candidate_snapshots')
      .select('driver_id,truck_number,hos_remaining_hours,truck_breakdown_risk,deadhead_miles,score,rank,was_assigned')
      .eq('quote_id', quoteId)
      .order('score', { ascending: false })
      .limit(8)
    setCandidates((data as unknown as CandidateRow[]) ?? [])
    setLoadingCandidates(false)
  }, [onSelectOrder, usingTripFallback])

  // "This order came in -> N candidates found -> best candidate selected" -- auto-advances to the
  // NEWEST order as playback reveals it, so the demo narrates itself while playing. A manual click
  // takes over until the next new order arrives (so a presenter can pause and inspect one without
  // fighting the auto-advance).
  React.useEffect(() => {
    if (visibleOrders.length === 0) return
    const newest = visibleOrders[visibleOrders.length - 1]
    if (!manualSelect || selectedId == null) {
      if (selectedId !== newest.quote_id) selectOrder(newest.quote_id, false)
    }
  }, [visibleOrders.length]) // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {
    if (!playing) return
    setManualSelect(false)
  }, [playing])

  React.useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [visibleOrders.length])

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-ink-200 px-3 py-2">
        <h3 className="font-display text-sm font-semibold text-ink-900">Order book</h3>
        <p className="text-[11px] text-ink-400">
          {visibleOrders.length} of {orders.length} orders so far —{' '}
          {usingTripFallback ? "real orders from this run's AI-assigned trip plan" : 'real rows from simulation.quote_requests'}
        </p>
        <div className="mt-1.5 flex flex-wrap gap-2 text-[10px] text-ink-500">
          {(Object.keys(STATUS_STYLE) as OrderStatus[]).map((s) => (
            <span key={s} className="flex items-center gap-1"><span className={`size-1.5 rounded-full ${STATUS_STYLE[s].dot}`} />{STATUS_STYLE[s].label}</span>
          ))}
        </div>
      </div>
      <div ref={listRef} className="flex-1 overflow-auto">
        {visibleOrders.map((o) => {
          const trip = tripByQuoteId.get(o.quote_id)
          const status = statusOf(o, trip, cursorSeconds)
          return (
            <button
              key={o.quote_id}
              onClick={() => { setManualSelect(true); selectOrder(o.quote_id, true) }}
              className={`block w-full border-b border-ink-100 px-3 py-2 text-left text-xs hover:bg-ink-50 ${selectedId === o.quote_id ? 'bg-brand-50' : ''}`}
            >
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 font-medium text-ink-800">
                  <span className={`size-1.5 shrink-0 rounded-full ${STATUS_STYLE[status].dot}`} />
                  {labels[o.origin_location_id] ?? o.origin_location_id} → {labels[o.dest_location_id] ?? o.dest_location_id}
                </span>
                {trip?.reload_immediate && trip.deadhead_saved > 0 && status === 'completed' && <Badge tone="green">+${trip.deadhead_saved.toFixed(0)} saved</Badge>}
              </div>
              <div className="mt-0.5 text-ink-400">
                Quote {formatSimTime(o.requested_at, 'MMM d, HH:mm')} · Pickup {formatSimTime(o.requested_pickup_at, 'MMM d, HH:mm')}
              </div>
              <div className="flex items-center gap-1.5 text-ink-400">
                <span>{o.weight_lbs.toLocaleString()} lbs · {o.pallets} plt · {o.load_type}</span>
                <Badge tone={o.service_type === 'LTL' ? 'amber' : 'blue'}>{o.service_type}</Badge>
              </div>
            </button>
          )
        })}
        {visibleOrders.length === 0 && <p className="p-3 text-xs text-ink-400">Waiting for the first order to arrive…</p>}
      </div>
      {selectedId && (
        <div className="max-h-72 overflow-auto border-t border-ink-200 bg-ink-50 p-3">
          <div className="mb-2 flex items-center justify-between">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">
              {usingTripFallback ? 'Assignment — Google OR-Tools CP-SAT' : 'Candidates scored — simulation.quote_candidate_snapshots'}
            </p>
            {/* "View full story" reads simulation.quote_requests by this exact id -- a real
                per-quote row that only the older showcase simulator ever writes, so it would 404
                for an AI-dispatch order regardless of what key is used here. Hidden in fallback
                mode rather than pointing at a drill-in that can't resolve. */}
            {onViewStory && !usingTripFallback && (
              <button onClick={() => onViewStory(selectedId)} className="text-[11px] font-semibold text-brand-600 hover:underline">
                View full story →
              </button>
            )}
          </div>
          <p className="mb-2 text-[11px] text-ink-400">
            {usingTripFallback
              ? "This order was assigned once, as part of the whole day's plan the CP-SAT solver optimized jointly across every truck and driver — not ranked against other candidates one at a time."
              : "Ranked by the model's total value score (immediate value + expected future positioning value) — the top-ranked candidate here is always the one assigned."}
          </p>
          {usingTripFallback ? null : loadingCandidates ? (
            <p className="text-xs text-ink-400">Loading…</p>
          ) : candidates.length === 0 ? (
            <p className="text-xs text-ink-400">No candidates recorded (order still pending or none feasible).</p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {candidates.map((c) => (
                <div
                  key={c.driver_id}
                  className={`rounded-md border p-2 text-[11px] ${c.was_assigned ? 'border-brand-400 bg-brand-50' : 'border-ink-200 bg-white'}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-ink-800">Driver {c.driver_id} · Truck {c.truck_number}</span>
                    {c.was_assigned && <Badge tone="green">assigned</Badge>}
                  </div>
                  <div className="mt-0.5 text-ink-500">
                    score {c.score?.toFixed(1)} · {c.deadhead_miles?.toFixed(1)} mi deadhead · {c.hos_remaining_hours?.toFixed(1)}h HOS left
                    {c.truck_breakdown_risk != null && ` · ${(c.truck_breakdown_risk * 100).toFixed(0)}% breakdown risk`}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
