// Client for the "Simulation Trip" page's backend (sim/live/trip_demo_simulator.py +
// dashboard/server/main.py's /api/trip-demo/* routes) -- a single trip ticked live, twice on the
// same lane (normal vs. extended dock time), to showcase the geofence arrival/departure trigger
// and detention billing end to end. Separate from simulation-api.ts's week-long batch Showcase --
// this polls a run IN PROGRESS rather than loading one already-finished week.

export type TripDemoScenarioKey = 'baseline' | 'detention'

export interface TripDemoScenario {
  label: string
  delivery_dwell_hours: number
}

export interface TripDemoStartResult {
  run_id: string
  trip_id: string
  quote_id: string
  driver_id: number
  truck_number: string
  scenario: TripDemoScenarioKey
  /** The exact route geometry the backend ticks against -- draw this directly, don't re-fetch
   * /api/route independently (see sim/live/trip_demo_simulator.DemoTripHandle's own comment on
   * why two independent fetches of the same lane is a real risk, not just redundant). [lon, lat]
   * GeoJSON order, same as /api/route. */
  route_coords: [number, number][]
}

export interface TripDemoTelemetryRow {
  id: number
  recorded_at: string
  phase: string | null
  lat: number | null
  lon: number | null
  speed_mph: number | null
  fuel_pct: number | null
  odometer_km: number | null
  note: string | null
}

export interface TripDemoGeofenceEvent {
  location_id: number
  event_type: 'arrival' | 'departure'
  occurred_at: string
}

export interface TripDemoDetention {
  location_id: number
  arrival_at: string | null
  departure_at: string | null
  billable_hours: number
  amount: number
}

export interface TripDemoLog {
  status: string
  last_event: string
  quote_id: string | null
  telemetry: TripDemoTelemetryRow[]
  geofence_events: TripDemoGeofenceEvent[]
  detention: TripDemoDetention[]
}

export async function getTripDemoScenarios(): Promise<Record<TripDemoScenarioKey, TripDemoScenario>> {
  const res = await fetch('/api/trip-demo/scenarios')
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

export async function startTripDemo(body: {
  origin_location_id: number
  dest_location_id: number
  scenario: TripDemoScenarioKey
}): Promise<TripDemoStartResult> {
  const res = await fetch('/api/trip-demo/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

// Real user ask: geofence editing needs to happen against a real trip_id BEFORE the truck starts
// moving, not layered on top of an already-ticking simulation. startTripDemo() now only creates
// the trip (parked, not yet moving); this releases it to actually begin ticking.
export async function beginTripDemo(tripId: string): Promise<{ status: string }> {
  const res = await fetch(`/api/trip-demo/${tripId}/begin`, { method: 'POST' })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

export async function getTripDemoLog(tripId: string, sinceId = 0): Promise<TripDemoLog> {
  const res = await fetch(`/api/trip-demo/${tripId}/log?since_id=${sinceId}`)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}
