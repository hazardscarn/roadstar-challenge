import { format } from 'date-fns'
import { CheckCircle2, Clock, Fuel, Gauge, Loader2, LogIn, LogOut, MapPin, PenLine, Radar, Receipt, Timer } from 'lucide-react'
import * as React from 'react'
import { FleetMap, type RouteSegment } from '@/components/fleet-map'
import { GeofenceEditDialog } from '@/components/geofence-map'
import { LocationPicker } from '@/components/location-picker'
import { PageHeader } from '@/components/page-header'
import { SimulationOrderStory } from '@/components/simulation-order-story'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { api, type FleetDriver, type LocationOption } from '@/lib/api'
import { getOrderStory } from '@/lib/simulation-api'
import {
  beginTripDemo, getTripDemoLog, startTripDemo, type TripDemoDetention, type TripDemoGeofenceEvent,
  type TripDemoScenarioKey, type TripDemoTelemetryRow,
} from '@/lib/trip-demo-api'

/** Live log rows are written every sim-MINUTE now (real user ask: the map should move on real
 * GPS-density positions) -- but the readable text list should still only show entries this far
 * apart, so it stays a legible "every couple minutes" log rather than a wall of near-duplicate
 * rows. Keeps the FIRST and LAST row always (start/most-recent state), thinning only the middle. */
const LOG_DISPLAY_GAP_MINUTES = 2

function thinForDisplay(rows: TripDemoTelemetryRow[]): TripDemoTelemetryRow[] {
  if (rows.length === 0) return rows
  const kept: TripDemoTelemetryRow[] = [rows[0]]
  let lastKeptMs = new Date(rows[0].recorded_at).getTime()
  for (let i = 1; i < rows.length - 1; i++) {
    const t = new Date(rows[i].recorded_at).getTime()
    if (t - lastKeptMs >= LOG_DISPLAY_GAP_MINUTES * 60_000) {
      kept.push(rows[i])
      lastKeptMs = t
    }
  }
  if (rows.length > 1) kept.push(rows[rows.length - 1])
  return kept
}

// Real user ask: showcase the brief's "critical requirement" (geofence-triggered detention
// billing) by running the SAME lane twice -- once with normal dock time, once past the free 2h
// window -- through the exact same buffered arrival/departure trigger (sim/sql/044, a retargeted
// copy of the real live.process_position_tick(), sim/sql/008), so the with/without-detention
// outcome is a real, database-recorded difference, not a scripted animation.

const SCENARIO_META: Record<TripDemoScenarioKey, { title: string; hint: string; color: string }> = {
  baseline: { title: 'Normal dock time', hint: 'Inside the free 2h window — no detention', color: '#1f9d55' },
  detention: { title: 'Extended dock time', hint: 'Past the free 2h window — detention billed', color: '#dc2626' },
}

interface RunState {
  trip_id: string
  quote_id: string
  driver_id: number
  truck_number: string
  status: string
  last_event: string
  telemetry: TripDemoTelemetryRow[]
  geofence_events: TripDemoGeofenceEvent[]
  detention: TripDemoDetention[]
  invoice: { linehaul_amount: number; detention_amount: number; fuel_surcharge_amount: number; total_amount: number } | null
  /** Full pickup->delivery road geometry, returned directly by /api/trip-demo/start -- the exact
   * same coordinates the backend ticks against (not a second, independent /api/route fetch; see
   * DemoTripHandle.route_coords for why that used to be a real risk). The "yet to go" light-blue
   * line; the "already driven" dark-blue line is drawn straight from the accumulated telemetry
   * points instead (they're real samples along this same route). */
  routeCoords: [number, number][] | null
  error: string | null
}

type RunsByScenario = Record<TripDemoScenarioKey, RunState | null>

function phaseLabel(phase: string | null): string {
  switch (phase) {
    case 'dwell_pickup': return 'Loading dock — pickup'
    case 'to_delivery': return 'En route to delivery'
    case 'dwell_delivery': return 'At delivery dock'
    case 'departed': return 'Pulling away from dock'
    default: return phase ?? '—'
  }
}

function formatClock(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60)
  const s = totalSeconds % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

export default function SimulationTrip() {
  const [origin, setOrigin] = React.useState<LocationOption | null>(null)
  const [dest, setDest] = React.useState<LocationOption | null>(null)
  const [runs, setRuns] = React.useState<RunsByScenario>({ baseline: null, detention: null })
  // Real user ask: the two scenarios' invoices need to be clearly told apart when opened, not
  // just distinguishable by numbers buried inside an otherwise-identical dialog.
  const [storyContext, setStoryContext] = React.useState<{ quoteId: string; label: string } | null>(null)
  const [launching, setLaunching] = React.useState(false)
  const [beginning, setBeginning] = React.useState(false)
  // Real user ask: "first I should have the option to edit geofence, then after I set it it
  // should start" -- setting up the trip (real trip_ids, needed since geofence overrides are
  // keyed by trip_id) and actually STARTING it moving are now two separate steps. `runs` existing
  // means the trip was created; `simStarted` means Start Simulation was clicked. Geofence editing
  // is only meaningful in the gap between the two -- editing after the truck is already moving
  // toward/through a stop it's about to pass doesn't do much.
  const [simStarted, setSimStarted] = React.useState(false)
  const [elapsedSec, setElapsedSec] = React.useState(0)
  const runsRef = React.useRef(runs)
  React.useEffect(() => {
    runsRef.current = runs
  }, [runs])
  const sinceIdRef = React.useRef<Record<TripDemoScenarioKey, number>>({ baseline: 0, detention: 0 })

  // Real user ask: one button, both scenarios at once -- they start at the same real moment, so
  // one shared stopwatch (not a per-panel clock) is all that's meaningful; keeps ticking, visible
  // proof the demo is alive, through the long stationary dock-dwell stretch that otherwise "feels
  // stuck" even though the trigger is genuinely counting down toward the 2h line underneath it.
  const bothDone = runs.baseline?.status === 'completed' && runs.detention?.status === 'completed'
  React.useEffect(() => {
    if (!simStarted) return  // the clock represents how long the RUN has been going, not setup time
    if (bothDone) return
    const id = setInterval(() => setElapsedSec((s) => s + 1), 1000)
    return () => clearInterval(id)
  }, [simStarted, bothDone])

  // Step 1: create both trips (real trip_ids, driver/truck assigned) but DON'T start them moving
  // yet -- see simStarted's own comment above for why.
  async function setUpBoth() {
    if (!origin || !dest) return
    setLaunching(true)
    setElapsedSec(0)
    setSimStarted(false)
    sinceIdRef.current = { baseline: 0, detention: 0 }
    try {
      const [baseline, detention] = await Promise.all(
        (['baseline', 'detention'] as const).map((scenario) =>
          startTripDemo({ origin_location_id: origin.location_id, dest_location_id: dest.location_id, scenario })
            .then((res) => ({ scenario, res, error: null as string | null }))
            .catch((err: unknown) => ({ scenario, res: null, error: err instanceof Error ? err.message : 'Could not start' })),
        ),
      )
      setRuns({
        baseline: toRunState(baseline),
        detention: toRunState(detention),
      })
    } finally {
      setLaunching(false)
    }
  }

  // Step 2: release both parked trips to actually start ticking -- called once the manager is
  // done (or has deliberately skipped) setting up custom geofences against the real trip_ids.
  async function beginBoth() {
    const ids = [runs.baseline?.trip_id, runs.detention?.trip_id].filter((id): id is string => !!id)
    if (ids.length === 0) return
    setBeginning(true)
    try {
      await Promise.all(ids.map((id) => beginTripDemo(id).catch(() => null)))
      setSimStarted(true)
    } finally {
      setBeginning(false)
    }
  }

  function toRunState(
    started: { scenario: TripDemoScenarioKey; res: Awaited<ReturnType<typeof startTripDemo>> | null; error: string | null },
  ): RunState | null {
    if (started.error || !started.res) {
      return { trip_id: '', quote_id: '', driver_id: 0, truck_number: '', status: 'error', last_event: '',
        telemetry: [], geofence_events: [], detention: [], invoice: null, routeCoords: null, error: started.error }
    }
    const res = started.res
    return {
      trip_id: res.trip_id, quote_id: res.quote_id, driver_id: res.driver_id, truck_number: res.truck_number,
      status: 'assigned', last_event: 'ASSIGNED', telemetry: [], geofence_events: [], detention: [], invoice: null,
      routeCoords: res.route_coords, error: null,
    }
  }

  // Poll both active runs every 1.5s -- fast enough to feel live against a TIME_SCALE-accelerated
  // trip that finishes in roughly 3-5 real minutes (sim/live/trip_demo_simulator.py). A ref
  // mirror of `runs` keeps this interval's closure from going stale without recreating it on
  // every telemetry update. Gated on simStarted -- nothing to poll before Start Simulation is
  // clicked, since the trip is created but parked (see beginBoth()'s own comment).
  React.useEffect(() => {
    if (!simStarted) return
    const id = setInterval(() => {
      (['baseline', 'detention'] as const).forEach(async (key) => {
        const run = runsRef.current[key]
        if (!run || run.status === 'completed' || run.error) return
        try {
          const log = await getTripDemoLog(run.trip_id, sinceIdRef.current[key])
          if (log.telemetry.length > 0) sinceIdRef.current[key] = log.telemetry[log.telemetry.length - 1].id
          setRuns((prev) => {
            const prevRun = prev[key]
            if (!prevRun || prevRun.trip_id !== run.trip_id) return prev
            return {
              ...prev,
              [key]: {
                ...prevRun,
                status: log.status, last_event: log.last_event,
                telemetry: [...prevRun.telemetry, ...log.telemetry],
                geofence_events: log.geofence_events, detention: log.detention,
              },
            }
          })
        } catch {
          // transient poll failure -- next tick retries, no need to surface a one-off blip
        }
      })
    }, 1500)
    return () => clearInterval(id)
  }, [simStarted])

  // Once a run's status flips to 'completed', fetch its invoice via the same Order Story endpoint
  // the batch Showcase uses (no separate endpoint needed -- sim/live/trip_demo_simulator.py writes
  // the exact same simulation.invoices/trip_log shape a batch run does).
  React.useEffect(() => {
    (['baseline', 'detention'] as const).forEach(async (key) => {
      const run = runs[key]
      if (!run || run.status !== 'completed' || run.invoice) return
      try {
        const story = await getOrderStory(run.quote_id)
        if (story.trip?.invoice) {
          setRuns((prev) => (prev[key]?.trip_id === run.trip_id ? { ...prev, [key]: { ...prev[key]!, invoice: story.trip!.invoice } } : prev))
        }
      } catch {
        // invoice fetch is best-effort polish for the comparison card below -- the live log/alerts
        // above already tell the real story even if this fails
      }
    })
  }, [runs.baseline?.status, runs.detention?.status]) // eslint-disable-line react-hooks/exhaustive-deps

  const anyActive = (['baseline', 'detention'] as const).some(
    (k) => runs[k] && runs[k]!.status !== 'completed' && runs[k]!.status !== 'error',
  )
  const tripsReady = !!(runs.baseline?.trip_id || runs.detention?.trip_id)

  return (
    <div className="flex h-full flex-col overflow-auto bg-ink-50">
      <PageHeader
        title="Simulation Trip"
        description="One lane, run twice at once, through the real geofence trigger — normal dock time vs. detention-billed dock time."
      />

      <div className="flex flex-col gap-4 p-6">
        <div className="flex flex-wrap items-end gap-3 rounded-xl border border-ink-200 bg-white p-4">
          <div className="w-64"><LocationPicker label="Pickup" value={origin} onChange={setOrigin} /></div>
          <div className="w-64"><LocationPicker label="Delivery" value={dest} onChange={setDest} /></div>
          {/* Real user ask: "first I should have the option to edit geofence, then after I set it
              it should start" -- Set Up Trip creates real trip_ids (so geofence overrides have
              something to key against) WITHOUT the truck moving yet; Start Simulation is a
              separate, deliberate second step. */}
          <Button
            variant={tripsReady ? 'outline' : 'default'}
            disabled={!origin || !dest || launching || anyActive}
            onClick={() => void setUpBoth()}
          >
            {launching ? <Loader2 className="size-4 animate-spin" /> : <Radar className="size-4" />}
            {tripsReady ? 'Set Up New Trip' : 'Set Up Trip'}
          </Button>
          {tripsReady && !simStarted && (
            <Button onClick={() => void beginBoth()} disabled={beginning}>
              {beginning ? <Loader2 className="size-4 animate-spin" /> : <Radar className="size-4" />}
              Start Simulation
            </Button>
          )}
          {simStarted && (
            <div className="flex items-center gap-1.5 rounded-lg border border-ink-200 bg-ink-50 px-3 py-2 text-sm font-medium text-ink-700">
              <Clock className="size-4 text-ink-400" />
              <span className="tabular-nums">{formatClock(elapsedSec)}</span>
              <span className="text-xs font-normal text-ink-400">{bothDone ? 'total' : 'elapsed'}</span>
            </div>
          )}
          {!origin || !dest ? (
            <p className="text-xs text-ink-400">Pick a pickup and delivery location to set up the demo.</p>
          ) : tripsReady && !simStarted ? (
            <p className="text-xs text-ink-400">Trip created — edit geofences below if you want, then Start Simulation when ready.</p>
          ) : null}
        </div>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {(['baseline', 'detention'] as const).map((key) => (
            <ScenarioPanel
              key={key} scenarioKey={key} run={runs[key]} origin={origin} dest={dest} simStarted={simStarted}
              onOpenStory={(quoteId) => setStoryContext({ quoteId, label: SCENARIO_META[key].title })}
            />
          ))}
        </div>

        {bothDone && runs.baseline?.invoice && runs.detention?.invoice && (
          <ComparisonCard baseline={runs.baseline.invoice} detention={runs.detention.invoice} />
        )}
      </div>

      {storyContext && (
        <SimulationOrderStory quoteId={storyContext.quoteId} contextLabel={storyContext.label} onClose={() => setStoryContext(null)} />
      )}
    </div>
  )
}

function ScenarioPanel({
  scenarioKey, run, origin, dest, simStarted, onOpenStory,
}: {
  scenarioKey: TripDemoScenarioKey
  run: RunState | null
  origin: LocationOption | null
  dest: LocationOption | null
  simStarted: boolean
  onOpenStory: (quoteId: string) => void
}) {
  // Real user ask: the same pickup+dropoff custom geofence drawing the real dispatch trip detail
  // page has, here too -- previously this demo only ever showed a fixed 150m dropoff circle.
  const [editingStop, setEditingStop] = React.useState<'pickup' | 'dropoff' | null>(null)
  // Real bug found directly: the REAL arrival/departure trigger (simulation.process_position_
  // tick()) already checked live.trip_geofence_overrides for a saved custom shape and used it
  // correctly -- but the LIVE map here never knew that shape existed at all, so it kept drawing
  // the default radius circle regardless of what was actually saved. Fetched independently of
  // GeofenceEditDialog's own internal state (which disappears the moment that dialog closes) so
  // the live map can show the real saved shape once editing is done.
  const [pickupShape, setPickupShape] = React.useState<[number, number][] | null>(null)
  const [dropoffShape, setDropoffShape] = React.useState<[number, number][] | null>(null)
  const tripId = run?.trip_id || null

  const refreshGeofenceShapes = React.useCallback(async () => {
    if (!tripId) return
    const [pickupStatus, dropoffStatus] = await Promise.all([
      origin ? api.getTripGeofence(tripId, origin.location_id).catch(() => null) : Promise.resolve(null),
      dest ? api.getTripGeofence(tripId, dest.location_id).catch(() => null) : Promise.resolve(null),
    ])
    // GeoJSON rings are [lon, lat] -- Leaflet wants [lat, lon] (same convention geofence-map.tsx's
    // toLatLngs() already uses for the edit dialog's own map).
    setPickupShape(pickupStatus?.manual_geometry?.coordinates[0]?.map(([lon, lat]) => [lat, lon]) ?? null)
    setDropoffShape(dropoffStatus?.manual_geometry?.coordinates[0]?.map(([lon, lat]) => [lat, lon]) ?? null)
  }, [tripId, origin, dest])

  React.useEffect(() => {
    void refreshGeofenceShapes()
  }, [refreshGeofenceShapes])

  const meta = SCENARIO_META[scenarioKey]
  const latest = run?.telemetry[run.telemetry.length - 1] ?? null
  const arrival = run?.geofence_events.find((e) => e.event_type === 'arrival' && e.location_id === dest?.location_id)
  const departure = run?.geofence_events.find((e) => e.event_type === 'departure' && e.location_id === dest?.location_id)
  const detentionRow = run?.detention.find((d) => d.location_id === dest?.location_id)
  // Inside the geofence right now, per the trigger itself -- not a guess from phase/distance:
  // confirmed arrival, no confirmed departure yet.
  const insideGeofence = !!arrival && !departure

  const driverForMap: FleetDriver[] = latest?.lat != null && latest.lon != null && run
    ? [{
        driver_id: run.driver_id, truck_number: run.truck_number,
        duty_status: insideGeofence ? 'geofence_triggered' : latest.phase === 'to_delivery' || latest.phase === 'departed' ? 'driving' : 'on_duty_not_driving',
        hos_remaining_hours: 10, current_trip_id: run.trip_id, last_location_id: null,
        speed_mph: latest.speed_mph, odometer_km: latest.odometer_km, fuel_pct: latest.fuel_pct,
        updated_at: latest.recorded_at, lat: latest.lat, lon: latest.lon, trip_status: run.status,
        eta: null, origin_location_id: null, dest_location_id: dest?.location_id ?? null,
        origin_lat: null, origin_lon: null, dest_lat: dest?.location_id ? latest.lat : null, dest_lon: null,
        inspection_ok: true,
      }]
    : []

  // "Already driven" (dark blue) is literal telemetry samples along the to_delivery leg -- real
  // positions the truck actually reported, not a fraction-of-route guess. "Yet to go" (light blue)
  // is the full fetched route, drawn first so the dark leg paints over its own already-covered part.
  const traveledCoords: [number, number][] = (run?.telemetry ?? [])
    .filter((t) => t.phase === 'to_delivery' && t.lat != null && t.lon != null)
    .map((t) => [t.lon as number, t.lat as number])
  const routes: RouteSegment[] = []
  if (run?.routeCoords) routes.push({ coords: run.routeCoords, color: '#93c5fd', weight: 5 })
  if (traveledCoords.length > 1) routes.push({ coords: traveledCoords, color: '#1e40af', weight: 5 })

  // The geofence circle belongs FIXED on the real delivery facility, not wherever the truck
  // happens to be right now -- the route's own last point (exact OSRM destination coordinate) is
  // the one client-side source of that, since LocationOption carries no lat/lon of its own. Falls
  // back to the truck's current position only if the backend's own route fetch failed (map
  // polish, never blocks the run -- see start_trip_demo/route_coords server-side).
  const routeStart = run?.routeCoords && run.routeCoords.length > 0 ? run.routeCoords[0] : null
  const routeEnd = run?.routeCoords && run.routeCoords.length > 0 ? run.routeCoords[run.routeCoords.length - 1] : null
  const pickupCenter: [number, number] | null = routeStart ? [routeStart[1], routeStart[0]] : null
  const geofenceCenter: [number, number] | null = routeEnd
    ? [routeEnd[1], routeEnd[0]] // [lon, lat] -> [lat, lon]
    : latest?.lat != null && latest.lon != null ? [latest.lat, latest.lon] : null

  // Zoom in tight on the geofence itself while the truck is at/near a dock -- real user ask: see
  // the entry happen, not just a dot on a zoomed-out regional map.
  const nearFacility = insideGeofence || latest?.phase === 'dwell_pickup' || latest?.phase === 'dwell_delivery' || latest?.phase === 'departed'
  const focusZoom = nearFacility ? 16 : 11

  return (
    <div className="flex flex-col overflow-hidden rounded-xl border border-ink-200 bg-white">
      <div className="flex items-center justify-between border-b border-ink-200 px-4 py-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="size-2.5 rounded-full" style={{ background: meta.color }} />
            <h3 className="font-display text-sm font-bold text-ink-900">{meta.title}</h3>
          </div>
          <p className="text-xs text-ink-400">{meta.hint}</p>
        </div>
        {run && (
          <div className="flex items-center gap-2">
            {origin && (
              <Button size="sm" variant="outline" onClick={() => setEditingStop('pickup')}>
                <PenLine className="size-3.5" /> Pickup Geofence
              </Button>
            )}
            {dest && (
              <Button size="sm" variant="outline" onClick={() => setEditingStop('dropoff')}>
                <PenLine className="size-3.5" /> Dropoff Geofence
              </Button>
            )}
            <StatusBadge status={run.status} simStarted={simStarted} />
          </div>
        )}
      </div>

      {run && origin && editingStop === 'pickup' && pickupCenter && (
        <GeofenceEditDialog
          open onOpenChange={(v) => !v && setEditingStop(null)}
          tripId={run.trip_id} locationId={origin.location_id} label={`Pickup — ${origin.label}`}
          lat={pickupCenter[0]} lon={pickupCenter[1]} color="#1baf7a"
          routeCoords={run.routeCoords} onSaved={() => void refreshGeofenceShapes()}
          otherStop={geofenceCenter ? { label: `Dropoff — ${dest?.label ?? ''}`, lat: geofenceCenter[0], lon: geofenceCenter[1] } : null}
        />
      )}
      {run && dest && editingStop === 'dropoff' && geofenceCenter && (
        <GeofenceEditDialog
          open onOpenChange={(v) => !v && setEditingStop(null)}
          tripId={run.trip_id} locationId={dest.location_id} label={`Dropoff — ${dest.label}`}
          lat={geofenceCenter[0]} lon={geofenceCenter[1]} color="#eb6834"
          routeCoords={run.routeCoords} onSaved={() => void refreshGeofenceShapes()}
          otherStop={pickupCenter ? { label: `Pickup — ${origin?.label ?? ''}`, lat: pickupCenter[0], lon: pickupCenter[1] } : null}
        />
      )}

      {!run ? (
        <div className="flex h-72 items-center justify-center text-sm text-ink-400">Not started</div>
      ) : run.error ? (
        <div className="p-4 text-sm text-status-red-500">{run.error}</div>
      ) : (
        <>
          <div className="h-[420px] border-b border-ink-200">
            <FleetMap
              drivers={driverForMap}
              satellite={false}
              routes={routes}
              geofences={[
                ...(pickupCenter
                  ? [{ lat: pickupCenter[0], lon: pickupCenter[1], radiusM: 120, label: 'Pickup geofence', polygon: pickupShape ?? undefined }]
                  : []),
                ...(geofenceCenter
                  ? [{ lat: geofenceCenter[0], lon: geofenceCenter[1], radiusM: 150, label: 'Delivery geofence', active: insideGeofence, polygon: dropoffShape ?? undefined }]
                  : []),
              ]}
              fitTo={latest?.lat != null && latest.lon != null ? [[latest.lat, latest.lon]] : undefined}
              focusZoom={focusZoom}
            />
          </div>

          <div className="grid grid-cols-4 gap-2 border-b border-ink-200 p-3 text-center text-xs">
            <Readout icon={Gauge} label="Speed" value={latest?.speed_mph != null ? `${latest.speed_mph.toFixed(0)} mph` : '—'} />
            <Readout icon={Fuel} label="Fuel" value={latest?.fuel_pct != null ? `${latest.fuel_pct.toFixed(0)}%` : '—'} />
            <Readout icon={MapPin} label="Odometer" value={latest?.odometer_km != null ? `${latest.odometer_km.toFixed(0)} km` : '—'} />
            <Readout icon={Timer} label="Phase" value={phaseLabel(latest?.phase ?? null)} small />
          </div>

          <div className="flex flex-col gap-1.5 border-b border-ink-200 p-3">
            {arrival && (
              <AlertRow icon={LogIn} tone="blue" text={`Entered geofence — arrival recorded ${format(new Date(arrival.occurred_at), 'HH:mm:ss')}`} />
            )}
            {departure && (
              <AlertRow icon={LogOut} tone="blue" text={`Exited geofence — departure recorded ${format(new Date(departure.occurred_at), 'HH:mm:ss')}`} />
            )}
            {detentionRow && detentionRow.amount > 0 && (
              <AlertRow icon={Timer} tone="red" text={`Detention threshold crossed — ${detentionRow.billable_hours.toFixed(1)}h billable, $${detentionRow.amount.toFixed(2)}`} />
            )}
            {detentionRow && detentionRow.departure_at && detentionRow.amount === 0 && (
              <AlertRow icon={CheckCircle2} tone="green" text="Departed within the free 2h window — no detention" />
            )}
            {!arrival && run.status !== 'completed' && (
              <p className="text-xs text-ink-400">
                {simStarted ? 'Watching for geofence arrival…' : 'Not started yet — edit geofences above, then Start Simulation.'}
              </p>
            )}
          </div>

          <div className="max-h-40 overflow-auto p-3">
            <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-ink-400">Live log — every {LOG_DISPLAY_GAP_MINUTES} sim-minutes</p>
            <div className="flex flex-col gap-1">
              {/* Real user ask: the every-2-min log should show GPS coordinates, speed, and fuel
                  in addition to what's already there -- all real fields this row already carries
                  (trip-demo-api.ts's TripDemoTelemetryRow), just not previously surfaced here. */}
              {[...thinForDisplay(run.telemetry)].reverse().map((row) => (
                <div key={row.id} className="flex items-center gap-2 text-[11px] text-ink-500">
                  <span className="w-16 shrink-0 tabular-nums text-ink-400">{format(new Date(row.recorded_at), 'HH:mm:ss')}</span>
                  <span className="flex-1 truncate">{row.note ?? phaseLabel(row.phase)}</span>
                  {row.lat != null && row.lon != null && (
                    <span className="shrink-0 tabular-nums text-ink-400">{row.lat.toFixed(4)}, {row.lon.toFixed(4)}</span>
                  )}
                  {row.speed_mph != null && <span className="shrink-0 tabular-nums">{row.speed_mph.toFixed(0)} mph</span>}
                  {row.fuel_pct != null && <span className="shrink-0 tabular-nums text-ink-400">{row.fuel_pct.toFixed(0)}% fuel</span>}
                </div>
              ))}
              {run.telemetry.length === 0 && <p className="text-xs text-ink-400">Waiting for the first tick…</p>}
            </div>
          </div>

          {run.status === 'completed' && (
            <div className="border-t border-ink-200 p-3">
              <Button variant="outline" size="sm" onClick={() => onOpenStory(run.quote_id)}>
                <Receipt className="size-3.5" /> View full trip story & invoice
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function StatusBadge({ status, simStarted }: { status: string; simStarted: boolean }) {
  if (status === 'completed') return <Badge tone="green">completed</Badge>
  if (status === 'error') return <Badge tone="red">error</Badge>
  // 'assigned' covers BOTH "created, parked, waiting for Start Simulation" and the brief instant
  // right after it's released before the first tick lands -- simStarted tells them apart.
  if (status === 'assigned') return <Badge tone={simStarted ? 'gray' : 'amber'}>{simStarted ? 'starting…' : 'ready — not started'}</Badge>
  return <Badge tone="blue">running</Badge>
}

function Readout({ icon: Icon, label, value, small }: { icon: React.ComponentType<{ className?: string }>; label: string; value: string; small?: boolean }) {
  return (
    <div className="flex flex-col items-center gap-0.5">
      <Icon className="size-3.5 text-ink-400" />
      <span className={small ? 'text-[10px] font-medium text-ink-700' : 'font-display text-sm font-bold text-ink-900'}>{value}</span>
      <span className="text-[10px] text-ink-400">{label}</span>
    </div>
  )
}

function AlertRow({ icon: Icon, tone, text }: { icon: React.ComponentType<{ className?: string }>; tone: 'blue' | 'green' | 'red'; text: string }) {
  const toneClasses = { blue: 'bg-status-blue-100 text-status-blue-500', green: 'bg-status-green-100 text-status-green-500', red: 'bg-status-red-100 text-status-red-500' }
  return (
    <div className={`flex items-center gap-2 rounded-md px-2.5 py-1.5 text-xs font-medium ${toneClasses[tone]}`}>
      <Icon className="size-3.5 shrink-0" />
      {text}
    </div>
  )
}

function ComparisonCard({
  baseline, detention,
}: {
  baseline: { linehaul_amount: number; detention_amount: number; fuel_surcharge_amount: number; total_amount: number }
  detention: { linehaul_amount: number; detention_amount: number; fuel_surcharge_amount: number; total_amount: number }
}) {
  const rows: [string, number, number][] = [
    ['Linehaul', baseline.linehaul_amount, detention.linehaul_amount],
    ['Fuel surcharge', baseline.fuel_surcharge_amount, detention.fuel_surcharge_amount],
    ['Detention', baseline.detention_amount, detention.detention_amount],
    ['Total (incl. HST)', baseline.total_amount, detention.total_amount],
  ]
  return (
    <div className="rounded-xl border border-ink-200 bg-white p-4">
      <h3 className="mb-1 font-display text-sm font-bold text-ink-900">Same lane, same trigger — the billing difference</h3>
      <p className="mb-3 text-xs text-ink-400">
        Both runs used the identical geofence arrival/departure mechanism; only the delivery dock dwell differed. The detention line
        item exists only because the buffered trigger genuinely recorded a departure past the 2h free window.
      </p>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-ink-200 text-left text-xs text-ink-400">
            <th className="py-1.5 font-medium">Line item</th>
            <th className="py-1.5 font-medium">Normal dock time</th>
            <th className="py-1.5 font-medium">Extended dock time</th>
            <th className="py-1.5 font-medium">Difference</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, a, b]) => (
            <tr key={label} className="border-b border-ink-100 last:border-0">
              <td className="py-1.5 text-ink-700">{label}</td>
              <td className="py-1.5 tabular-nums text-ink-600">${a.toFixed(2)}</td>
              <td className="py-1.5 tabular-nums text-ink-600">${b.toFixed(2)}</td>
              <td className={`py-1.5 tabular-nums font-semibold ${b - a > 0 ? 'text-status-red-500' : 'text-ink-400'}`}>
                {b - a > 0 ? `+$${(b - a).toFixed(2)}` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
