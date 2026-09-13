import { format } from 'date-fns'
import {
  ChevronDown, ChevronsRight, Loader2, Maximize2, Minimize2, Pause, Play, PlayCircle, RotateCcw, Satellite,
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
  buildTimeline, listAiDispatchRuns, loadAiDispatchRun, positionAt, runAiDispatchSimulation,
  type AiDispatchRunListItem, type SimulationRunResult, type SimTrip, type TimelineEvent,
} from '@/lib/simulation-api'
import { formatSimTime } from '@/lib/utils'

// Real user ask: the old default (1,000×) played back so fast it was "hard to notice" -- 200× is
// now the default, slow enough to actually watch a truck move leg to leg.
const SPEED_OPTIONS = [
  { label: '60×', value: 60 },
  { label: '200×', value: 200 },
  { label: '300×', value: 300 },
  { label: '1,000×', value: 1000 },
]

// Real user ask: color each truck by its HOME HUB (Milton/London/Barrie), not by driver identity
// -- the whole point of this replay is showing judges that Milton trucks run near Milton, London
// trucks near London, a real emergent property of the AI's own hub-aware assignment. Matches the
// synthetic duty_status keys added to fleet-map.tsx's STATUS_COLOR.
function hubStatusKey(hubCity: string | null | undefined): string {
  if (!hubCity) return 'off_duty'
  const key = `hub_${hubCity.toLowerCase()}`
  return key in { hub_milton: 1, hub_london: 1, hub_barrie: 1 } ? key : 'off_duty'
}
const HUB_COLOR: Record<string, string> = { Milton: '#2a78d6', London: '#eb6834', Barrie: '#1baf7a' }
function colorForHub(hubCity: string | null | undefined): string {
  return (hubCity && HUB_COLOR[hubCity]) || '#6b7382'
}

function fmtClock(seconds: number): string {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return d > 0 ? `Day ${d + 1}, ${h}h ${m.toString().padStart(2, '0')}m` : `${h}h ${m.toString().padStart(2, '0')}m`
}

// Real user ask: the "Sim clock" KPI read as elapsed playback time (e.g. "1h 39m") -- replaced
// with the actual real-world clock time inside the simulated day (result.sim_start + cursor).
function fmtSimTime(simStart: string, seconds: number): string {
  const t = new Date(new Date(simStart).getTime() + seconds * 1000)
  return formatSimTime(t, 'EEE, MMM d · h:mm a')
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
  const [speed, setSpeed] = React.useState(200)
  const [rightTab, setRightTab] = React.useState<'feed' | 'tables'>('feed')
  const [runsList, setRunsList] = React.useState<AiDispatchRunListItem[] | null>(null)
  const [showRunsMenu, setShowRunsMenu] = React.useState(false)
  const [loadingRunId, setLoadingRunId] = React.useState<string | null>(null)
  const [focusedQuoteId, setFocusedQuoteId] = React.useState<string | null>(null)
  const [storyQuoteId, setStoryQuoteId] = React.useState<string | null>(null)
  const [selectedDriverId, setSelectedDriverId] = React.useState<number | null>(null)
  // Real user ask: "date selected and run" -- defaults to tomorrow (the same "always the day
  // before" convention the Dispatch Board uses) but is a real, editable date now, same pattern as
  // DispatchBoard.tsx's own date picker. Whatever AI-assigned (or manually dispatched) plan is on
  // the board for the chosen date gets replayed -- the backend 400s with a clear message if none
  // exists yet, surfaced below via the existing `error` state.
  const [replayDate, setReplayDate] = React.useState(() => {
    const d = new Date()
    d.setDate(d.getDate() + 1)
    return d.toISOString().slice(0, 10)
  })
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

  // Real bug found from a screenshot: the map "auto focusing up and down a lot." FleetMap refits
  // its view to whatever points it's given whenever they change, and with no explicit `fitTo` it
  // defaults to the fleet's CURRENT positions -- which move every animation frame during playback,
  // so the view was re-fitting itself ~60 times a second as trucks drove. Computed once per loaded
  // run instead (every trajectory point across every trip, so the whole day's coverage area is
  // framed from the start and never needs to move again), deliberately NOT depending on `cursor`.
  const dayBoundsFitTo = React.useMemo((): [number, number][] | undefined => {
    if (!result) return undefined
    let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity
    for (const t of result.trips) {
      for (const [, lat, lon] of t.trajectory) {
        if (lat < minLat) minLat = lat
        if (lat > maxLat) maxLat = lat
        if (lon < minLon) minLon = lon
        if (lon > maxLon) maxLon = lon
      }
    }
    if (!Number.isFinite(minLat)) return undefined
    return [[minLat, minLon], [maxLat, maxLon]]
  }, [result])

  async function toggleRunsMenu() {
    if (!showRunsMenu && runsList === null) {
      try {
        setRunsList(await listAiDispatchRuns())
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
      const res = await loadAiDispatchRun(runId)
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
      const res = await runAiDispatchSimulation(replayDate)
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

  // Real user ask: a real geofence/detention "red light" warning on the map -- this trip's final
  // dwell was deliberately stretched past the 2h free window (sim/live/ai_dispatch_replay.py's
  // one guaranteed demo case); reusing FleetMap's existing "<2h HOS = red" rule as the visual
  // trigger while that window is actually active, rather than adding a whole new status branch.
  function speedFuelAt(t: SimTrip, cursorT: number): { speed: number | null; fuel: number | null } {
    const traj = t.trajectory
    for (let i = 0; i < traj.length; i++) {
      if (traj[i][0] >= cursorT || i === traj.length - 1) {
        return { speed: traj[i][3] ?? null, fuel: traj[i][4] ?? null }
      }
    }
    return { speed: null, fuel: null }
  }

  // Real user ask: "trucks that completed [their] trip and [are] back at hub at final to be
  // shown resting there as grey... currently they all go away". `activeTrips` (assigned_at_s <=
  // cursor < completed_at_s) is right for finding a driver's CURRENT leg, but a driver often has
  // several chained trips -- dropping off the map the instant one leg's own completed_at_s passes
  // made a truck vanish mid-day, between two of its own legs, not just at real end-of-day. Grouped
  // by driver instead: still driving their current leg if one is active; parked (grey) at the
  // FINAL position of their LAST trip once that trip's own completed_at_s has passed (real
  // end-of-day, already back at hub -- ai_dispatch_replay.py's last leg includes the real
  // end-of-day return drive); simply absent before their first trip starts.
  // Real perf bug found directly: this grouping (plus a fresh sort per driver) was being rebuilt
  // from scratch on every render -- during playback that's every animation frame, up to 60fps.
  // Only `cursor` needs to run every frame; which trips belong to which driver, and their order,
  // never changes for a loaded run. Memoized on `result` alone so the grouping/sort happens once
  // per run load, not once per frame.
  const tripsByDriver = React.useMemo(() => {
    const map = new Map<number, SimTrip[]>()
    for (const t of result?.trips ?? []) {
      const arr = map.get(t.driver_id)
      if (arr) arr.push(t)
      else map.set(t.driver_id, [t])
    }
    for (const arr of map.values()) arr.sort((a, b) => a.assigned_at_s - b.assigned_at_s)
    return map
  }, [result])

  const drivers: FleetDriver[] = Array.from(tripsByDriver.entries())
    .map(([driverId, sorted]): FleetDriver | null => {
      const current = sorted.find((t) => t.assigned_at_s <= cursor && t.completed_at_s > cursor)
      if (current) {
        const pos = positionAt(current, cursor)
        if (!pos) return null
        const inDetention = current.is_detention_demo && current.arr_delivery_at_s != null && cursor >= current.arr_delivery_at_s && cursor < current.completed_at_s
        const { speed, fuel } = speedFuelAt(current, cursor)
        return {
          driver_id: driverId, truck_number: current.truck_number,
          duty_status: inDetention ? 'on_duty_not_driving' : hubStatusKey(current.hub_city),
          hos_remaining_hours: inDetention ? 1 : 8, current_trip_id: current.trip_id, last_location_id: null,
          speed_mph: speed, odometer_km: null, fuel_pct: fuel, updated_at: null,
          lat: pos.lat, lon: pos.lon, trip_status: null, eta: null,
          origin_location_id: current.origin_location_id, dest_location_id: current.dest_location_id,
          origin_lat: null, origin_lon: null, dest_lat: null, dest_lon: null, inspection_ok: true,
        }
      }
      const last = sorted[sorted.length - 1]
      if (!last || cursor < last.completed_at_s) return null // hasn't started yet today
      const restPos = last.trajectory[last.trajectory.length - 1]
      if (!restPos) return null
      return {
        driver_id: driverId, truck_number: last.truck_number,
        duty_status: 'off_duty', // grey -- resting at hub, done for the day
        hos_remaining_hours: 8, current_trip_id: last.trip_id, last_location_id: null,
        speed_mph: 0, odometer_km: null, fuel_pct: restPos[4] ?? null, updated_at: null,
        lat: restPos[1], lon: restPos[2], trip_status: null, eta: null,
        origin_location_id: last.origin_location_id, dest_location_id: last.dest_location_id,
        origin_lat: null, origin_lon: null, dest_lat: null, dest_lon: null, inspection_ok: true,
      }
    })
    .filter((d): d is FleetDriver => d !== null)

  const routes: RouteSegment[] = activeTrips.map((t) => ({
    coords: t.trajectory.map(([, lat, lon]) => [lon, lat] as [number, number]),
    color: colorForHub(t.hub_city),
  }))

  // Real user ask: clicking a truck should show "all the trip details of that driver/truck ...
  // and stats like deadhead saved" -- not just whichever single leg happens to be active right
  // now. A resting (grey) truck has no active leg at all, so `selectedTrip` must fall back to
  // that driver's LAST trip (same `sorted`/`last` logic as the `drivers` array above) instead of
  // going blank the moment the panel's own anchor trip completes.
  const selectedDriverTrips = selectedDriverId != null ? (tripsByDriver.get(selectedDriverId) ?? []) : []
  const selectedCurrentTrip = selectedDriverTrips.find((t) => t.assigned_at_s <= cursor && t.completed_at_s > cursor) ?? null
  const selectedLastTrip = selectedDriverTrips.length > 0 ? selectedDriverTrips[selectedDriverTrips.length - 1] : null
  const selectedTrip = selectedCurrentTrip ?? selectedLastTrip
  const selectedIsResting = selectedCurrentTrip == null && selectedLastTrip != null && cursor >= selectedLastTrip.completed_at_s
  const selectedReadout = selectedCurrentTrip ? speedFuelAt(selectedCurrentTrip, cursor) : null
  // Real gap found while wiring this: dashboard/server/main.py hardcodes deadhead_saved=0 for
  // EVERY AI-dispatch trip (that field is only ever computed by the older per-quote scorer's
  // reload_immediate mechanic) -- so for this run type it would always read as a real, permanent
  // $0 rather than an honest "not computed here." Same empty-quote_id signal the order book uses
  // to detect an AI-dispatch run, reused here to hide that one tile instead of showing a fake zero.
  const isAiDispatchRun = (result?.trips.length ?? 0) > 0 && result!.trips.every((t) => !t.quote_id)

  // Real dollar figures already computed server-side per trip (deadhead_avoided_value_for in
  // main.py) -- summed across every trip this driver has run today, not approximated client-side.
  const selectedStats = selectedDriverTrips.length > 0 ? {
    tripCount: selectedDriverTrips.length,
    totalRevenue: selectedDriverTrips.reduce((s, t) => s + t.order_revenue, 0),
    totalDeadheadMiles: selectedDriverTrips.reduce((s, t) => s + t.deadhead_miles, 0),
    totalDeadheadSaved: selectedDriverTrips.reduce((s, t) => s + t.deadhead_saved, 0),
    totalDetention: selectedDriverTrips.reduce((s, t) => s + t.detention_amount, 0),
    onTimeCount: selectedDriverTrips.filter((t) => t.on_time).length,
  } : null

  // Real user feedback: clicking an order in the order book should show that order's trip on the
  // map -- highlighted regardless of the playback cursor (an order picked from earlier/later in
  // the week than "now" still needs to be visible), and the map pans/zooms to it. AI-dispatch
  // trips all share quote_id = "" (see simulation-order-book.tsx's own note on this), so trip_id
  // stands in as the key whenever quote_id is blank -- same fallback on both sides.
  const focusedTrip = result?.trips.find((t) => (t.quote_id || t.trip_id) === focusedQuoteId) ?? null
  if (focusedTrip && !activeTrips.some((t) => t.trip_id === focusedTrip.trip_id)) {
    routes.push({
      coords: focusedTrip.trajectory.map(([, lat, lon]) => [lon, lat] as [number, number]),
      color: '#14171c',
    })
  }
  // A deliberately focused order still overrides the stable whole-day view above -- that pan/zoom
  // is intentional (the user just clicked something specific); everything else stays put.
  const focusFitTo: [number, number][] | undefined = focusedTrip
    ? focusedTrip.trajectory.map(([, lat, lon]) => [lat, lon] as [number, number])
    : dayBoundsFitTo

  const totalRevenue = completedSoFar.reduce((s, t) => s + t.order_revenue, 0)
  const totalDeadheadMilesSoFar = completedSoFar.reduce((s, t) => s + t.deadhead_miles, 0)
  const totalDetention = completedSoFar.reduce((s, t) => s + t.detention_amount, 0)
  const totalInvoiced = completedSoFar.reduce((s, t) => s + t.invoice_total, 0)
  const finished = result != null && cursor >= result.duration_seconds
  const driverIds = result ? Array.from(new Set(result.trips.map((t) => t.driver_id))).sort((a, b) => a - b) : []
  const activeDriverIds = Array.from(new Set(activeTrips.map((t) => t.driver_id))).sort((a, b) => a - b)

  return (
    <div className="flex h-screen flex-col">
      <PageHeader
        title="Simulation Showcase"
        description="Watch the AI (Google OR-Tools CP-SAT) dispatcher's real plan for tomorrow play out on the map — every truck, every hub, every trip, backed by the real Supabase data."
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
                    runsList.map((r) => (
                      <button
                        key={r.run_id}
                        onClick={() => handleLoadRun(r.run_id)}
                        className="block w-full border-b border-ink-100 px-3 py-2 text-left text-xs last:border-0 hover:bg-ink-50"
                      >
                        <div className="font-medium text-ink-800">
                          {r.service_date} — run {format(new Date(r.created_at), 'MMM d, h:mm a')}
                        </div>
                        <div className="text-ink-400">
                          {r.n_completed}/{r.n_orders_generated} completed · ${r.total_revenue.toFixed(0)} revenue ·{' '}
                          ${r.total_detention_billed.toFixed(0)} detention
                        </div>
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
            {/* Real user ask: pick which day to run, instead of always whatever "tomorrow" was
                when the page first loaded -- same date-input pattern as the Dispatch Board. */}
            <input
              type="date" value={replayDate} onChange={(e) => e.target.value && setReplayDate(e.target.value)}
              disabled={loading} className="rounded-md border border-ink-200 px-2 py-1.5 text-sm focus:border-brand-500 focus:outline-none disabled:opacity-50"
            />
            {/* Real user ask: "Replaying XXXX" read like it was replaying something old --
                reframed as what it actually is, a fast simulation run sourced from the real
                Dispatch plan, not a recorded playback. */}
            <Button onClick={handleRun} disabled={loading}>
              {loading ? <Loader2 className="size-4 animate-spin" /> : <PlayCircle className="size-4" />}
              {loading ? 'Running Live Ops Simulation from Dispatch…' : `Run AI Dispatch — ${replayDate}`}
            </Button>
          </div>
        }
      />

      {error && <p className="px-6 pt-3 text-sm text-status-red-500">{error}</p>}

      {!result ? (
        <div className="flex flex-1 items-center justify-center p-6 text-center text-sm text-ink-400">
          {loading
            ? `Running a fast Live Ops Simulation from Dispatch's real AI-assigned plan for ${replayDate} — every truck, every hub, every trip…`
            : `Click "Run AI Dispatch" to run a fast Live Ops Simulation from Dispatch's real AI-assigned plan for ${replayDate} — every truck's route, colored by home hub, on the real map.`}
        </div>
      ) : (
        <>
          <div className="border-b border-ink-200 bg-white px-6 py-3">
            <KpiBand>
              <KpiTile label="Sim clock" value={fmtSimTime(result.sim_start, cursor)} sublabel={fmtClock(cursor)} hero />
              <KpiTile label="Trips dispatched" value={String(dispatchedSoFar.length)} sublabel={`of ${result.summary.n_orders_generated} orders`} />
              <KpiTile label="Trips completed" value={String(completedSoFar.length)} tone="green" />
              <KpiTile label="Revenue" value={`$${totalRevenue.toFixed(0)}`} tone="green" />
              <KpiTile label="Deadhead miles" value={totalDeadheadMilesSoFar.toFixed(0)} />
              <KpiTile
                label="Avg driver HOS used"
                value={result.summary.avg_driver_hours_used != null ? `${result.summary.avg_driver_hours_used.toFixed(1)}h` : '—'}
                sublabel={result.summary.n_drivers_used != null ? `${result.summary.n_drivers_used} drivers` : undefined}
              />
            </KpiBand>
            <p className="mt-2 text-[11px] text-ink-400">
              Every truck/driver/order match here is exactly what sim/dispatch_solver.py (Google OR-Tools CP-SAT) assigned for this real day —
              this is a replay of that plan, not a new decision. Detention billed ${totalDetention.toFixed(0)} · Invoiced ${totalInvoiced.toFixed(0)}.
              {result.summary.n_unassigned > 0 && (
                <span className="ml-1 text-status-amber-500">
                  {result.summary.n_unassigned} order(s) went unassigned this day.
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
                // Real bug found directly: the order book's own auto-narration (advancing to the
                // newest revealed order as playback runs) was firing this on every new order --
                // continuously yanking the map to that order's own route the whole time a sim
                // played. Only a genuine manual click should ever move the map now.
                onSelectOrder={(quoteId, manual) => { if (manual) setFocusedQuoteId(quoteId) }}
                onViewStory={setStoryQuoteId}
              />
            </div>

            <div className="relative flex flex-1 flex-col">
              <div className="relative flex-1">
                <FleetMap
                  drivers={drivers} satellite={satellite} routes={routes} fitTo={focusFitTo}
                  selectedDriverId={selectedDriverId} onSelectDriver={setSelectedDriverId}
                />
                <label className="absolute right-3 top-3 z-[400] flex items-center gap-1.5 rounded-lg border border-ink-200 bg-white/95 px-2.5 py-1.5 text-xs text-ink-600 shadow-lg backdrop-blur">
                  <Satellite className="size-3.5" />
                  Satellite
                  <Switch checked={satellite} onCheckedChange={setSatellite} />
                </label>
                {/* Real user ask: click a truck to see its live log -- speed, fuel, driver/hub
                    detail -- straight from this run's already-loaded trajectory, no extra fetch. */}
                {selectedTrip && (
                  <div className="absolute bottom-3 left-3 z-[400] max-h-[70vh] w-80 overflow-y-auto rounded-lg border border-ink-200 bg-white/95 p-3 text-xs shadow-lg backdrop-blur">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-ink-900">Truck {selectedTrip.truck_number}</span>
                      <button onClick={() => setSelectedDriverId(null)} className="text-ink-400 hover:text-ink-700">✕</button>
                    </div>
                    <div className="mt-1 text-ink-500">Driver #{selectedTrip.driver_id} · {selectedTrip.hub_city ?? '—'} hub</div>
                    {/* Real user ask: resting (grey) trucks stay clickable -- shown as "done for
                        the day" instead of a live speed/fuel readout that no longer applies. */}
                    {selectedIsResting ? (
                      <div className="mt-2 inline-block rounded-md bg-ink-100 px-2 py-1 font-semibold text-ink-500">● Resting at hub — day complete</div>
                    ) : (
                      <>
                        <div className="mt-2 grid grid-cols-2 gap-2">
                          <div><div className="text-[10px] text-ink-400 uppercase">Speed</div><div className="font-semibold">{selectedReadout?.speed != null ? `${selectedReadout.speed.toFixed(0)} mph` : '—'}</div></div>
                          <div><div className="text-[10px] text-ink-400 uppercase">Fuel</div><div className="font-semibold">{selectedReadout?.fuel != null ? `${selectedReadout.fuel.toFixed(0)}%` : '—'}</div></div>
                        </div>
                        {/* Real user ask: GPS coordinates alongside speed/fuel in the live readout. */}
                        {(() => {
                          const pos = positionAt(selectedTrip, cursor)
                          return pos ? (
                            <div className="mt-1"><div className="text-[10px] text-ink-400 uppercase">GPS</div><div className="font-mono text-[11px]">{pos.lat.toFixed(4)}, {pos.lon.toFixed(4)}</div></div>
                          ) : null
                        })()}
                      </>
                    )}
                    <div className="mt-2 text-ink-500">{selectedTrip.origin_label ?? '—'} → {selectedTrip.dest_label ?? '—'}</div>
                    {selectedTrip.is_detention_demo && selectedTrip.arr_delivery_at_s != null && cursor >= selectedTrip.arr_delivery_at_s && cursor < selectedTrip.completed_at_s && (
                      <div className="mt-2 rounded-md bg-status-red-100 px-2 py-1 font-semibold text-status-red-500">⚠ Detention — geofence still active</div>
                    )}
                    {/* Real user ask: "stats like deadhead saved and all" -- real dollar/mile
                        figures already computed server-side per trip, summed across today. */}
                    {selectedStats && (
                      <div className="mt-3 grid grid-cols-2 gap-2 border-t border-ink-200 pt-2">
                        <div><div className="text-[10px] text-ink-400 uppercase">Trips today</div><div className="font-semibold">{selectedStats.tripCount}</div></div>
                        <div><div className="text-[10px] text-ink-400 uppercase">On-time</div><div className="font-semibold">{selectedStats.onTimeCount}/{selectedStats.tripCount}</div></div>
                        <div><div className="text-[10px] text-ink-400 uppercase">Revenue</div><div className="font-semibold">${selectedStats.totalRevenue.toFixed(0)}</div></div>
                        {/* Not computed at all for AI-dispatch runs (see isAiDispatchRun above) --
                            hidden rather than shown as a permanent, misleading $0. */}
                        {!isAiDispatchRun && (
                          <div><div className="text-[10px] text-ink-400 uppercase">Deadhead saved</div><div className="font-semibold text-status-green-500">${selectedStats.totalDeadheadSaved.toFixed(0)}</div></div>
                        )}
                        <div><div className="text-[10px] text-ink-400 uppercase">Deadhead miles</div><div className="font-semibold">{selectedStats.totalDeadheadMiles.toFixed(0)} mi</div></div>
                        {selectedStats.totalDetention > 0 && (
                          <div><div className="text-[10px] text-ink-400 uppercase">Detention</div><div className="font-semibold text-status-amber-500">${selectedStats.totalDetention.toFixed(0)}</div></div>
                        )}
                      </div>
                    )}
                    {/* Real user ask: "show all the trip details of that driver/truck" -- the full
                        day's chain, not just whichever leg is active right now. */}
                    {selectedDriverTrips.length > 0 && (
                      <div className="mt-3 space-y-1.5 border-t border-ink-200 pt-2">
                        <div className="text-[10px] text-ink-400 uppercase">Today's trips</div>
                        {selectedDriverTrips.map((t, i) => (
                          <button
                            key={t.trip_id}
                            onClick={() => setFocusedQuoteId(t.quote_id || t.trip_id)}
                            className={`block w-full rounded-md px-1.5 py-1 text-left hover:bg-ink-100 ${t.trip_id === selectedTrip.trip_id ? 'bg-ink-100' : ''}`}
                          >
                            <div className="flex items-center justify-between">
                              <span className="font-medium text-ink-800">Trip {i + 1}: {t.origin_label ?? '—'} → {t.dest_label ?? '—'}</span>
                              {t.completed_at_s <= cursor && <span className="text-status-green-500">✓</span>}
                            </div>
                            <div className="text-ink-400">${t.order_revenue.toFixed(0)} · {t.on_time ? 'on time' : 'late'}{!isAiDispatchRun && t.deadhead_saved > 0 ? ` · $${t.deadhead_saved.toFixed(0)} saved` : ''}</div>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
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
                  {/* Real bug found directly: this panel crashed the whole page (blank screen) the
                      instant a run finished (or a past run loaded, which starts at cursor ==
                      duration_seconds) -- it was written for the OLD week-long ML/RL batch sim's
                      summary shape (n_deadhead_avoided, total_invoiced, n_on_time, n_late,
                      lost_opportunity_revenue, dispatcher_hours_saved, n_breakdowns), none of
                      which exist on the AI-dispatch-day replay's real summary object (verified
                      directly against the running backend). Rewritten against the fields that
                      actually exist, plus locally-derived on-time/invoiced/hours-saved figures. */}
                  <h2 className="font-display text-base font-bold text-ink-900">This day's results</h2>
                  <div className="rounded-lg border border-ink-200 p-3 text-sm">
                    <div>{result.summary.n_completed} of {result.summary.n_orders_generated} orders completed, {result.summary.n_unassigned} unassigned</div>
                    <div><strong>${result.summary.total_revenue.toFixed(0)} revenue captured</strong> · ${result.summary.net_margin.toFixed(0)} net margin</div>
                    <div className="text-ink-500">
                      Deadhead: ${result.summary.total_deadhead_cost.toFixed(0)} across {(result.summary.total_deadhead_miles ?? totalDeadheadMilesSoFar).toFixed(0)} mi
                    </div>
                    <div>Detention: ${result.summary.total_detention_billed.toFixed(2)} · Invoiced: ${totalInvoiced.toFixed(2)}</div>
                    <div>On-time: {completedSoFar.filter((t) => t.on_time).length} · Late: {completedSoFar.filter((t) => !t.on_time).length}</div>
                    <div className="mt-1 text-ink-500">
                      {result.summary.n_decisions} dispatch decisions made automatically — an estimated{' '}
                      <strong>{((result.summary.n_decisions * 8) / 60).toFixed(1)} hours</strong> of manual dispatcher time saved
                      (~8 min/decision assumption: checking each candidate's HOS, position, and truck health by hand).
                    </div>
                  </div>
                  <SimulationDriverSpotlight runId={result.run_id} driverIds={driverIds} />
                  <p className="rounded-lg bg-ink-50 px-3 py-2 text-xs text-ink-500">
                    This is one fresh, AI-assigned day — its numbers will vary run to run and day to day. For the
                    actual validated real-data result (a separate, honest backtest, not this run): <strong>+$704 CAD
                    across 1,667 real orders</strong>, plus 130/130 real historical undispatched orders found a
                    feasible candidate (~$10.6K recovered). See <code className="text-[11px]">documents/results/real_data_backtest</code>.
                  </p>
                  <Button size="sm" variant="outline" onClick={handleRun}>Run another day</Button>
                </div>
              ) : (
                <div ref={feedRef} className="flex-1 overflow-auto p-3">
                  <div className="flex flex-col gap-2">
                    {visibleEvents.map((e, i) => (
                      // eslint-disable-next-line react/no-array-index-key
                      <div key={i} className="rounded-lg border border-ink-200 p-2.5 text-xs">
                        <div className="mb-0.5 flex items-center justify-between">
                          <span className="flex items-center gap-1.5 font-medium text-ink-800">
                            <span className="inline-block size-2 rounded-full" style={{ backgroundColor: colorForHub(e.trip.hub_city) }} />
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
