// Client for the Simulation Showcase's /api/simulation/run -- a full simulated WEEK against the
// same 30-driver demo fleet Live Ops shows, real order book, real trained-model decisions (no
// exploration). Persisted server-side into the dedicated `simulation` schema (sim/sql/038) --
// this client also reads that schema directly (via supabase-js, same as any other live.* query)
// for the "Data tables" tab, proving the run's data is real, not a client-side animation.

export interface SimTrip {
  trip_id: string
  quote_id: string
  driver_id: number
  truck_number: string
  reload_immediate: boolean
  deadhead_saved: number
  assigned_at_s: number
  completed_at_s: number
  arr_pickup_at_s: number | null
  dep_pickup_at_s: number | null
  arr_delivery_at_s: number | null
  origin_location_id: number
  dest_location_id: number
  origin_label: string | null
  dest_label: string | null
  weight_lbs: number
  pallets: number
  load_type: string
  order_revenue: number
  deadhead_cost: number
  lateness_penalty: number
  net_margin: number
  deadhead_miles: number
  on_time: boolean
  had_breakdown: boolean
  detention_amount: number
  invoice_total: number
  trajectory: [number, number, number][] // [t_offset_seconds, lat, lon]
}

export interface SimulationRunSummary {
  n_orders_generated: number
  n_completed: number
  n_unassigned: number
  n_decisions: number
  dispatcher_hours_saved: number
  lost_opportunity_revenue: number
  total_revenue: number
  n_deadhead_avoided: number
  deadhead_avoided_value: number
  total_deadhead_cost: number
  total_lateness_penalty: number
  net_margin: number
  total_detention_billed: number
  total_invoiced: number
  n_breakdowns: number
  n_on_time: number
  n_late: number
  value_vs_baseline: number
  baseline_n_completed: number
  baseline_net_margin: number
}

// Real user feedback: the showcase only ever showed a handful of averages next to the map --
// these are the direct sim-demo equivalents of what real backtesting already showed (sim/backtest/
// real_data_replay.py's run_cycle_analysis()/--cycles), as DISTRIBUTIONS not just means, computed
// server-side once per run (sim/engine/fleet_metrics.py) and stored with the run so a reload shows
// the identical numbers. null only for a run saved before this was added (sim/sql/043).
export interface FleetMetrics {
  driver_pool_size: number
  drivers_used: number
  trips_per_driver: { mean: number; std_dev: number; min: number; max: number; histogram: number[] }
  cycles: {
    n_cycles: number
    avg_trips_per_cycle: number
    avg_revenue_per_cycle: number
    avg_empty_return_miles: number
    empty_return_miles_histogram: number[]
    avg_duration_hours: number
    avg_hos_remaining_at_return: number | null
    n_closed_by_trip: number
    n_closed_by_assumed_empty_return: number
  }
  daily_hos_utilization: {
    n_work_days_observed: number
    avg_utilization_pct: number
    utilization_pct_histogram: number[]
    n_over_13h_limit: number
  }
}

export interface SimulationRunListItem {
  run_id: string
  seed: number
  created_at: string
  week_start: string
  week_end: string
  n_orders_generated: number
  n_completed: number
  n_unassigned: number
  total_revenue: number
  net_margin: number
  n_deadhead_avoided: number
  deadhead_avoided_value: number
}

export async function listSimulationRuns(): Promise<SimulationRunListItem[]> {
  const res = await fetch('/api/simulation/runs')
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

export async function loadSimulationRun(runId: string): Promise<SimulationRunResult> {
  const res = await fetch(`/api/simulation/runs/${runId}`)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

export interface SimulationRunResult {
  run_id: string
  seed: number
  sim_start: string
  duration_seconds: number
  summary: SimulationRunSummary
  fleet_metrics: FleetMetrics | null
  trips: SimTrip[]
}

export async function runWeekSimulation(seed?: number): Promise<SimulationRunResult> {
  const res = await fetch('/api/simulation/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(seed != null ? { seed } : {}),
  })
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

// The "Order Story" drill-in -- real user feedback: "show me the whole story of an order... from
// candidates available to candidate selected, the trip, the savings, whether a geofence happened,
// the subsequent trip if any (was this guy having a deadhead)". Everything here is read directly
// from simulation.* (sim/sql/038-040) -- nothing recomputed or fabricated.

export interface OrderStoryCandidate {
  driver_id: number
  truck_number: string
  location_id: number | null
  hos_remaining_hours: number | null
  truck_breakdown_risk: number | null
  truck_pct_km_interval: number | null
  truck_pct_days_interval: number | null
  deadhead_miles: number | null
  planned_driving_hours: number | null
  planned_duty_hours: number | null
  score: number | null
  rank: number | null
  was_assigned: boolean
  scored_at: string
}

export interface DeadheadVsAvgAlternative {
  chosen_deadhead_miles: number | null
  avg_alternative_deadhead_miles: number
  miles_saved: number
  value_saved: number
  n_other_candidates: number
}

export interface OrderStoryTrip {
  trip_id: string
  driver_id: number
  truck_number: string
  assigned_at: string | null
  completed_at: string | null
  on_time: boolean
  load_fill_ratio: number
  order_revenue: number
  deadhead_cost: number
  deadhead_miles: number
  post_delivery_deadhead_cost: number
  post_delivery_deadhead_miles: number
  reloaded_immediately: boolean
  lateness_penalty: number
  net_margin: number
  had_breakdown: boolean
  geofence_events: { location_id: number; location_label: string | null; event_type: string; occurred_at: string }[]
  detention: { location_id: number; location_label: string | null; arrival_at: string | null; departure_at: string | null; billable_hours: number; amount: number }[]
  invoice: {
    invoice_number: string; linehaul_amount: number; detention_amount: number; fuel_surcharge_amount: number
    subtotal: number; tax_amount: number; total_amount: number; status: string
  } | null
  trajectory: [number, number, number][]
  previous_trip: OrderStoryAdjacentTrip | null
  next_trip: OrderStoryAdjacentTrip | null
}

export interface OrderStoryAdjacentTrip {
  trip_id: string
  assigned_at: string | null
  completed_at: string | null
  origin_label: string | null
  dest_label: string | null
  deadhead_miles: number
  had_deadhead: boolean
  trajectory: [number, number, number][]
}

export interface OrderStory {
  quote: {
    quote_id: string; origin_label: string | null; dest_label: string | null
    requested_at: string; requested_pickup_at: string
    weight_lbs: number; pallets: number; load_type: string; service_type: string; status: string
  }
  candidates: OrderStoryCandidate[]
  chosen_driver_id: number | null
  deadhead_vs_avg_alternative: DeadheadVsAvgAlternative | null
  trip: OrderStoryTrip | null
}

export async function getOrderStory(quoteId: string): Promise<OrderStory> {
  const res = await fetch(`/api/simulation/orders/${quoteId}/story`)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json()
}

/** Linear interpolation along a trip's precomputed [t, lat, lon] samples -- null if the trip
 * isn't active (hasn't started or already finished) at time t. */
export function positionAt(trip: SimTrip, t: number): { lat: number; lon: number } | null {
  if (t < trip.assigned_at_s || t > trip.completed_at_s) return null
  const traj = trip.trajectory
  if (traj.length === 0) return null
  if (t <= traj[0][0]) return { lat: traj[0][1], lon: traj[0][2] }
  for (let i = 0; i < traj.length - 1; i++) {
    const [t0, lat0, lon0] = traj[i]
    const [t1, lat1, lon1] = traj[i + 1]
    if (t >= t0 && t <= t1) {
      const frac = t1 > t0 ? (t - t0) / (t1 - t0) : 0
      return { lat: lat0 + (lat1 - lat0) * frac, lon: lon0 + (lon1 - lon0) * frac }
    }
  }
  const last = traj[traj.length - 1]
  return { lat: last[1], lon: last[2] }
}

export type TimelineEventKind = 'assigned' | 'arr_pickup' | 'dep_pickup' | 'arr_delivery' | 'completed'

export interface TimelineEvent {
  t: number
  kind: TimelineEventKind
  trip: SimTrip
}

export function buildTimeline(trips: SimTrip[]): TimelineEvent[] {
  const events: TimelineEvent[] = []
  for (const trip of trips) {
    events.push({ t: trip.assigned_at_s, kind: 'assigned', trip })
    if (trip.arr_pickup_at_s != null) events.push({ t: trip.arr_pickup_at_s, kind: 'arr_pickup', trip })
    if (trip.dep_pickup_at_s != null) events.push({ t: trip.dep_pickup_at_s, kind: 'dep_pickup', trip })
    if (trip.arr_delivery_at_s != null) events.push({ t: trip.arr_delivery_at_s, kind: 'arr_delivery', trip })
    events.push({ t: trip.completed_at_s, kind: 'completed', trip })
  }
  return events.sort((a, b) => a.t - b.t)
}
