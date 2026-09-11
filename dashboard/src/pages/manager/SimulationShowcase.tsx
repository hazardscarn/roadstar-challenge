import { format } from 'date-fns'
import {
  AlertTriangle, ChevronDown, ChevronsRight, Loader2, Maximize2, Minimize2, Pause, Play, PlayCircle, RotateCcw, Satellite,
} from 'lucide-react'
import * as React from 'react'
import { FleetMap, type RouteSegment } from '@/components/fleet-map'
import { KpiBand, KpiTile } from '@/components/kpi-band'
import { PageHeader } from '@/components/page-header'
import { SimulationActiveDrivers } from '@/components/simulation-active-drivers'
import { SimulationDataTables, SimulationDriverSpotlight } from '@/components/simulation-data-tables'
import { SimulationFleetDashboard } from '@/components/simulation-fleet-dashboard'
import { SimulationFleetMetricsExpander } from '@/components/simulation-fleet-metrics'
import { SimulationOrderBook } from '@/components/simulation-order-book'
import { SimulationOrderStory } from '@/components/simulation-order-story'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import type { FleetDriver } from '@/lib/api'
import {
  buildTimeline, listSimulationRuns, loadSimulationRun, positionAt, runWeekSimulation,
  type SimulationRunListItem, type SimulationRunResult, type SimTrip, type TimelineEvent,
} from '@/lib/simulation-api'

const SPEED_OPTIONS = [
  { label: '1,000×', value: 1000 },
  { label: '4,000×', value: 4000 },
  { label: '10,000×', value: 10000 },
]

// Differentiable per-truck route/marker colors -- real user feedback: with many trucks moving at
// once, one flat color made simultaneous trips indistinguishable. Keyed by driver_id (stable
// across a driver's multiple trips over the week, not per-trip) so the same truck reads as the
// same color the whole run. Red is reserved for a real breakdown, not reused here.
const TRIP_PALETTE = ['#2a5cdb', '#7c3aed', '#0d9488', '#ea580c', '#db2777', '#4f46e5', '#0891b2', '#65a30d', '#c026d3', '#b45309']
function colorForDriver(driverId: number): string {
  return TRIP_PALETTE[driverId % TRIP_PALETTE.length]
}

function statusAt(trip: SimTrip, t: number): string {
  if (trip.arr_pickup_at_s != null && t >= trip.arr_pickup_at_s && (trip.dep_pickup_at_s == null || t < trip.dep_pickup_at_s)) {
    return 'on_duty_not_driving'
  }
  if (trip.arr_delivery_at_s != null && t >= trip.arr_delivery_at_s) return 'on_duty_not_driving'
  return 'driving'
}

function fmtClock(seconds: number): string {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return d > 0 ? `Day ${d + 1}, ${h}h ${m.toString().padStart(2, '0')}m` : `${h}h ${m.toString().padStart(2, '0')}m`
}

const EVENT_LABEL: Record<TimelineEvent['kind'], string> = {
  assigned: 'Assigned',
  arr_pickup: 'Arrived at pickup',
  dep_pickup: 'Departed pickup — loaded',
  arr_delivery: 'Arrived at delivery',
  completed: 'Delivered',
}

export default function SimulationShowcase() {
  const [loading, setLoading] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [result, setResult] = React.useState<SimulationRunResult | null>(null)
  const [cursor, setCursor] = React.useState(0)
  const [playing, setPlaying] = React.useState(false)
  const [speed, setSpeed] = React.useState(1000)
  const [rightTab, setRightTab] = React.useState<'feed' | 'tables'>('feed')
  const [runsList, setRunsList] = React.useState<SimulationRunListItem[] | null>(null)
  const [showRunsMenu, setShowRunsMenu] = React.useState(false)
  const [loadingRunId, setLoadingRunId] = React.useState<string | null>(null)
  const [focusedQuoteId, setFocusedQuoteId] = React.useState<string | null>(null)
  const [storyQuoteId, setStoryQuoteId] = React.useState<string | null>(null)
  const [asideExpanded, setAsideExpanded] = React.useState(false)
  // Real user feedback: "another view alternate in sim page ... kpi dashboards ... driver
  // dashboard and truck dashboard." A page-level mode next to Playback -- reuses the same loaded
  // `result.run_id`, no separate fetch/run needed to switch into it.
  const [viewMode, setViewMode] = React.useState<'playback' | 'fleet'>('playback')
  // Real user feedback: "satellite toggle is not available in the simulation showcase" -- Live
  // Ops, Dispatch's candidate map, and the Order Story route map all already had one; this was
  // the one map in the app still hardcoded to street view with no way to flip it.
  const [satellite, setSatellite] = React.useState(false)
  const feedRef = React.useRef<HTMLDivElement>(null)

  const timeline = React.useMemo(() => (result ? buildTimeline(result.trips) : []), [result])

  async function toggleRunsMenu() {
    if (!showRunsMenu && runsList === null) {
      try {
        setRunsList(await listSimulationRuns())
      } catch {
        setRunsList([])
      }
    }
    setShowRunsMenu((v) => !v)
  }

  async function handleLoadRun(runId: string) {
    setShowRunsMenu(false)
    setLoadingRunId(runId)
    setError(null)
    try {
      const res = await loadSimulationRun(runId)
      setResult(res)
      setCursor(res.duration_seconds) // a reloaded run is shown fully played-out -- scrub back to replay any part of it
      setPlaying(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load that run')
    } finally {
      setLoadingRunId(null)
    }
  }

  function skipToNextEvent() {
    const next = timeline.find((e) => e.t > cursor + 1)
    if (next) setCursor(next.t)
    else if (result) setCursor(result.duration_seconds)
  }

  async function handleRun() {
    setLoading(true)
    setError(null)
    setResult(null)
    setCursor(0)
    try {
      const res = await runWeekSimulation()
      setResult(res)
      setPlaying(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Simulation run failed')
    } finally {
      setLoading(false)
    }
  }

  // Real user feedback: a genuinely sparse real order book leaves multi-hour gaps with nothing
  // assigned/moving -- ticking the clock second-by-second through a dead stretch just shows an
  // idle number with nothing on the map. Jump straight to the next event whenever nothing is
  // currently active; once landed, normal animation resumes for whatever just started moving.
  React.useEffect(() => {
    if (!playing || !result) return
    let raf: number
    let last = performance.now()
    const tick = (now: number) => {
      const deltaS = ((now - last) / 1000) * speed
      last = now
      setCursor((c) => {
        const active = result.trips.some((t) => t.assigned_at_s <= c && t.completed_at_s > c)
        if (!active) {
          const nextEventT = timeline.find((e) => e.t > c + 0.5)?.t
          if (nextEventT != null) {
            if (nextEventT >= result.duration_seconds) {
              setPlaying(false)
              return result.duration_seconds
            }
            return nextEventT
          }
        }
        const next = c + deltaS
        if (next >= result.duration_seconds) {
          setPlaying(false)
          return result.duration_seconds
        }
        return next
      })
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [playing, speed, result, timeline])

  React.useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: 'smooth' })
  }, [cursor])

  const visibleEvents = timeline.filter((e) => e.t <= cursor)
  const completedSoFar = result?.trips.filter((t) => t.completed_at_s <= cursor) ?? []
  const activeTrips = result?.trips.filter((t) => t.assigned_at_s <= cursor && t.completed_at_s > cursor) ?? []
  const dispatchedSoFar = result?.trips.filter((t) => t.assigned_at_s <= cursor) ?? []

  const drivers: FleetDriver[] = activeTrips
    .map((t): FleetDriver | null => {
      const pos = positionAt(t, cursor)
      if (!pos) return null
      return {
        driver_id: t.driver_id, truck_number: t.truck_number, duty_status: statusAt(t, cursor),
        hos_remaining_hours: 8, current_trip_id: t.trip_id, last_location_id: null,
        speed_mph: null, odometer_km: null, fuel_pct: null, updated_at: null,
        lat: pos.lat, lon: pos.lon, trip_status: null, eta: null,
        origin_location_id: t.origin_location_id, dest_location_id: t.dest_location_id,
        origin_lat: null, origin_lon: null, dest_lat: null, dest_lon: null, inspection_ok: true,
      }
    })
    .filter((d): d is FleetDriver => d !== null)

  const routes: RouteSegment[] = activeTrips.map((t) => ({
    coords: t.trajectory.map(([, lat, lon]) => [lon, lat] as [number, number]),
    color: t.had_breakdown ? '#d9342b' : colorForDriver(t.driver_id),
  }))

  // Real user feedback: clicking an order in the order book should show that order's trip on the
  // map -- highlighted regardless of the playback cursor (an order picked from earlier/later in
  // the week than "now" still needs to be visible), and the map pans/zooms to it.
  const focusedTrip = result?.trips.find((t) => t.quote_id === focusedQuoteId) ?? null
  if (focusedTrip && !activeTrips.some((t) => t.trip_id === focusedTrip.trip_id)) {
    routes.push({
      coords: focusedTrip.trajectory.map(([, lat, lon]) => [lon, lat] as [number, number]),
      color: '#14171c',
    })
  }
  const focusFitTo: [number, number][] | undefined = focusedTrip
    ? focusedTrip.trajectory.map(([, lat, lon]) => [lat, lon] as [number, number])
    : undefined

  const totalRevenue = completedSoFar.reduce((s, t) => s + t.order_revenue, 0)
  const deadheadAvoidedCount = completedSoFar.filter((t) => t.reload_immediate).length
  const deadheadAvoidedValue = completedSoFar.reduce((s, t) => s + t.deadhead_saved, 0)
  const totalDetention = completedSoFar.reduce((s, t) => s + t.detention_amount, 0)
  const totalInvoiced = completedSoFar.reduce((s, t) => s + t.invoice_total, 0)
  const finished = result != null && cursor >= result.duration_seconds
  const driverIds = result ? Array.from(new Set(result.trips.map((t) => t.driver_id))).sort((a, b) => a - b) : []
  const activeDriverIds = Array.from(new Set(activeTrips.map((t) => t.driver_id))).sort((a, b) => a - b)
  // dispatcher_hours_saved is the FULL-run figure (n_orders_generated x assumed minutes/decision)
  // -- scaled down to "so far" by how many decisions have actually been made by the cursor.
  const dispatcherHoursSavedSoFar = result
    ? (result.summary.dispatcher_hours_saved / Math.max(result.summary.n_orders_generated, 1)) * dispatchedSoFar.length
    : 0

  return (
    <div className="flex h-screen flex-col">
      <PageHeader
        title="Simulation Showcase"
        description="Watch the trained AI model make every dispatch decision for a real week — the value of automated, dynamic-programming-based assignment, end to end."
        actions={
          <div className="flex items-center gap-2">
            {result && (
              <div className="flex rounded-lg bg-ink-100 p-1">
                {(['playback', 'fleet'] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setViewMode(m)}
                    className={`rounded-md px-3 py-1.5 text-sm font-medium ${viewMode === m ? 'bg-white text-ink-900 shadow-sm' : 'text-ink-500'}`}
                  >
                    {m === 'playback' ? 'Playback' : 'Fleet Dashboard'}
                  </button>
                ))}
              </div>
            )}
            <div className="relative">
              <Button variant="outline" onClick={toggleRunsMenu} disabled={loadingRunId !== null}>
                {loadingRunId ? <Loader2 className="size-4 animate-spin" /> : <ChevronDown className="size-4" />}
                Past runs
              </Button>
              {showRunsMenu && (
                <div className="absolute right-0 top-full z-20 mt-1 max-h-80 w-80 overflow-auto rounded-lg border border-ink-200 bg-white shadow-lg">
                  {runsList == null ? (
                    <p className="p-3 text-xs text-ink-400">Loading…</p>
                  ) : runsList.length === 0 ? (
                    <p className="p-3 text-xs text-ink-400">No saved runs yet — run one first.</p>
                  ) : (
                    // runsList comes back newest-first; "Sim 1" is the oldest so numbering stays
                    // stable as new runs get added, not shifting every time you run another one.
                    runsList.map((r, i) => (
                      <button
                        key={r.run_id}
                        onClick={() => handleLoadRun(r.run_id)}
                        className="block w-full border-b border-ink-100 px-3 py-2 text-left text-xs last:border-0 hover:bg-ink-50"
                      >
                        <div className="font-medium text-ink-800">
                          Sim {runsList.length - i} — {format(new Date(r.created_at), 'MMM d, h:mm a')}
                        </div>
                        <div className="text-ink-400">
                          {r.n_completed}/{r.n_orders_generated} completed · ${r.total_revenue.toFixed(0)} revenue ·{' '}
                          {r.n_deadhead_avoided ?? 0} deadhead avoided
                        </div>
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
            <Button onClick={handleRun} disabled={loading}>
              {loading ? <Loader2 className="size-4 animate-spin" /> : <PlayCircle className="size-4" />}
              {loading ? 'Simulating a week…' : 'Run Week Simulation'}
            </Button>
          </div>
        }
      />

      {error && <p className="px-6 pt-3 text-sm text-status-red-500">{error}</p>}

      {!result ? (
        <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-ink-400">
          {loading
            ? 'Generating a real week-long order book and running the trained model against the 30-truck demo fleet…'
            : 'Click Run Week Simulation to watch the trained model manage a real order book — quote to delivery to invoice — over a full simulated week.'}
        </div>
      ) : (
        <>
          <div className="border-b border-ink-200 bg-white px-6 py-3">
            <KpiBand>
              <KpiTile label="Sim clock" value={fmtClock(cursor)} hero />
              <KpiTile label="Decisions made" value={String(dispatchedSoFar.length)} sublabel={`of ${result.summary.n_orders_generated} orders`} />
              <KpiTile label="Dispatcher time saved" value={`${dispatcherHoursSavedSoFar.toFixed(1)}h`} tone="green" />
              <KpiTile label="Completed" value={String(completedSoFar.length)} tone="green" />
              <KpiTile label="Revenue" value={`$${totalRevenue.toFixed(0)}`} tone="green" />
              <KpiTile label="Deadhead avoided" value={String(deadheadAvoidedCount)} sublabel={`~$${deadheadAvoidedValue.toFixed(0)} saved`} tone="green" />
            </KpiBand>
            <p className="mt-2 text-[11px] text-ink-400">
              "Deadhead avoided" = trips where the truck reloaded immediately after delivery (no empty return leg) — valued against
              this run's own average empty-return cost when one WAS needed. Detention billed ${totalDetention.toFixed(0)} · Invoiced ${totalInvoiced.toFixed(0)}.
              {result.summary.n_unassigned > 0 && (
                <span className="ml-1 text-status-amber-500">
                  {result.summary.n_unassigned} orders no truck could take this week, ~${result.summary.lost_opportunity_revenue.toFixed(0)} in missed revenue (a fleet-capacity signal, not a dispatch-quality one).
                </span>
              )}
            </p>
          </div>

          <SimulationFleetMetricsExpander metrics={result.fleet_metrics} />

          {viewMode === 'fleet' ? (
            <SimulationFleetDashboard runId={result.run_id} />
          ) : (
          <div className="flex flex-1 overflow-hidden">
            <div className="w-80 shrink-0 border-r border-ink-200 bg-white">
              <SimulationOrderBook
                runId={result.run_id} cursorSeconds={cursor} simStart={result.sim_start} trips={result.trips} playing={playing}
                onSelectOrder={setFocusedQuoteId} onViewStory={setStoryQuoteId}
              />
            </div>

            <div className="relative flex flex-1 flex-col">
              <div className="relative flex-1">
                <FleetMap drivers={drivers} satellite={satellite} routes={routes} fitTo={focusFitTo} />
                <label className="absolute right-3 top-3 z-[400] flex items-center gap-1.5 rounded-lg border border-ink-200 bg-white/95 px-2.5 py-1.5 text-xs text-ink-600 shadow-lg backdrop-blur">
                  <Satellite className="size-3.5" />
                  Satellite
                  <Switch checked={satellite} onCheckedChange={setSatellite} />
                </label>
                <SimulationActiveDrivers runId={result.run_id} simStart={result.sim_start} cursorSeconds={cursor} activeDriverIds={activeDriverIds} />
              </div>
              <div className="flex items-center gap-3 border-t border-ink-200 bg-white px-4 py-3">
                <Button size="icon" variant="outline" onClick={() => setPlaying((p) => !p)} disabled={finished}>
                  {playing ? <Pause className="size-4" /> : <Play className="size-4" />}
                </Button>
                <Button size="icon" variant="outline" onClick={() => { setCursor(0); setPlaying(true) }}>
                  <RotateCcw className="size-4" />
                </Button>
                <Button size="icon" variant="outline" onClick={skipToNextEvent} disabled={finished} title="Skip to next event">
                  <ChevronsRight className="size-4" />
                </Button>
                <input
                  type="range"
                  min={0}
                  max={result.duration_seconds}
                  value={cursor}
                  onChange={(e) => { setCursor(Number(e.target.value)); setPlaying(false) }}
                  className="flex-1"
                />
                <div className="flex rounded-lg bg-ink-100 p-1">
                  {SPEED_OPTIONS.map((s) => (
                    <button
                      key={s.value}
                      onClick={() => setSpeed(s.value)}
                      className={`rounded-md px-2 py-1 text-xs font-medium ${speed === s.value ? 'bg-white text-ink-900 shadow-sm' : 'text-ink-500'}`}
                    >
                      {s.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <aside className={`flex shrink-0 flex-col overflow-hidden border-l border-ink-200 bg-white transition-all ${asideExpanded ? 'w-[46rem]' : 'w-96'}`}>
              <div className="flex items-center border-b border-ink-200">
                {(['feed', 'tables'] as const).map((t) => (
                  <button
                    key={t}
                    onClick={() => setRightTab(t)}
                    className={`flex-1 px-3 py-2 text-xs font-medium ${rightTab === t ? 'border-b-2 border-brand-500 text-brand-600' : 'text-ink-500'}`}
                  >
                    {t === 'feed' ? 'Feed' : 'Data Tables'}
                  </button>
                ))}
                <button
                  onClick={() => setAsideExpanded((v) => !v)}
                  title={asideExpanded ? 'Collapse' : 'Expand — more room for wide tables'}
                  className="shrink-0 px-2.5 py-2 text-ink-400 hover:text-ink-700"
                >
                  {asideExpanded ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
                </button>
              </div>

              {rightTab === 'tables' ? (
                <div className="flex-1 overflow-hidden">
                  <SimulationDataTables runId={result.run_id} />
                </div>
              ) : finished ? (
                <div className="flex flex-1 flex-col gap-3 overflow-auto p-4">
                  <h2 className="font-display text-base font-bold text-ink-900">This week's results</h2>
                  <div className="rounded-lg border border-ink-200 p-3 text-sm">
                    <div>{result.summary.n_completed} of {result.summary.n_orders_generated} orders completed, {result.summary.n_unassigned} unassigned</div>
                    <div><strong>${result.summary.total_revenue.toFixed(0)} revenue captured</strong></div>
                    <div className="text-status-green-500">
                      {result.summary.n_deadhead_avoided} trip(s) reloaded immediately — ~${result.summary.deadhead_avoided_value.toFixed(0)} in empty-return cost avoided
                    </div>
                    <div>Detention: ${result.summary.total_detention_billed.toFixed(2)} · Invoiced: ${result.summary.total_invoiced.toFixed(2)}</div>
                    <div>On-time: {result.summary.n_on_time} · Late: {result.summary.n_late}</div>
                    {result.summary.n_unassigned > 0 && (
                      <div className="text-status-red-500">Lost opportunity: ~${result.summary.lost_opportunity_revenue.toFixed(0)} in missed revenue</div>
                    )}
                    <div className="mt-1 text-ink-500">
                      {result.summary.n_decisions} dispatch decisions made automatically — an estimated <strong>{result.summary.dispatcher_hours_saved.toFixed(1)} hours</strong> of manual dispatcher time saved
                      (~8 min/decision assumption: checking each candidate's HOS, position, and truck health by hand).
                    </div>
                    {result.summary.n_breakdowns > 0 && (
                      <div className="mt-1 flex items-center gap-1 text-ink-400">
                        <AlertTriangle className="size-3.5" /> {result.summary.n_breakdowns} breakdown(s) this run (fleet maintenance — outside this sim's dispatch-decision scope)
                      </div>
                    )}
                  </div>
                  <SimulationDriverSpotlight runId={result.run_id} driverIds={driverIds} />
                  <p className="rounded-lg bg-ink-50 px-3 py-2 text-xs text-ink-500">
                    This is one fresh, randomly-seeded week — its numbers will vary run to run. For the actual validated
                    real-data result (a separate, honest backtest, not this run): <strong>+$704 CAD across 1,667 real
                    orders</strong>, plus 130/130 real historical undispatched orders found a feasible candidate
                    (~$10.6K recovered). See <code className="text-[11px]">documents/results/real_data_backtest</code>.
                  </p>
                  <Button size="sm" variant="outline" onClick={handleRun}>Run another week</Button>
                </div>
              ) : (
                <div ref={feedRef} className="flex-1 overflow-auto p-3">
                  <div className="flex flex-col gap-2">
                    {visibleEvents.map((e, i) => (
                      // eslint-disable-next-line react/no-array-index-key
                      <div key={i} className="rounded-lg border border-ink-200 p-2.5 text-xs">
                        <div className="mb-0.5 flex items-center justify-between">
                          <span className="flex items-center gap-1.5 font-medium text-ink-800">
                            <span className="inline-block size-2 rounded-full" style={{ backgroundColor: colorForDriver(e.trip.driver_id) }} />
                            Driver {e.trip.driver_id} · Truck {e.trip.truck_number}
                          </span>
                          {e.kind === 'completed' && !e.trip.on_time && <Badge tone="amber">late</Badge>}
                          {e.kind === 'completed' && e.trip.reload_immediate && e.trip.deadhead_saved > 0 && <Badge tone="green">+${e.trip.deadhead_saved.toFixed(0)} saved</Badge>}
                        </div>
                        <div className="text-ink-500">
                          {EVENT_LABEL[e.kind]}
                          {e.kind === 'assigned' && ` — ${e.trip.origin_label ?? 'pickup'} → ${e.trip.dest_label ?? 'delivery'}`}
                          {e.kind === 'completed' && ` — $${e.trip.order_revenue.toFixed(0)} revenue`}
                          {e.kind === 'completed' && e.trip.detention_amount > 0 && ` · detention $${e.trip.detention_amount.toFixed(0)}`}
                          {e.kind === 'completed' && e.trip.reload_immediate && ' · reloaded immediately, no empty return leg'}
                        </div>
                        <div className="mt-0.5 text-[11px] text-ink-400">{fmtClock(e.t)}</div>
                      </div>
                    ))}
                    {visibleEvents.length === 0 && <p className="text-xs text-ink-400">Waiting for the first dispatch decision…</p>}
                  </div>
                </div>
              )}
            </aside>
          </div>
          )}
        </>
      )}

      {storyQuoteId && <SimulationOrderStory quoteId={storyQuoteId} onClose={() => setStoryQuoteId(null)} />}
    </div>
  )
}
