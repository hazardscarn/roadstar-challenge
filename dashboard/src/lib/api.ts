// Client for dashboard/server (local FastAPI, proxied at /api by vite.config.ts). This is the
// ONLY thing that calls score-quote/assign/route -- the frontend never talks to Postgres
// directly for these, matching the plan's architecture (backend uses the direct SUPABASE_DB_URL
// connection, bypassing RLS as a trusted local process; the browser only ever holds the anon key).

export interface FleetDriver {
  driver_id: number
  truck_number: string
  duty_status: string
  hos_remaining_hours: number
  current_trip_id: string | null
  last_location_id: number | null
  speed_mph: number | null
  odometer_km: number | null
  fuel_pct: number | null
  updated_at: string | null
  lat: number | null
  lon: number | null
  trip_status: string | null
  eta: string | null
  origin_location_id: number | null
  dest_location_id: number | null
  origin_lat: number | null
  origin_lon: number | null
  dest_lat: number | null
  dest_lon: number | null
  inspection_ok: boolean
}

export interface LocationOption {
  location_id: number
  label: string
  city: string
  tier: string
}

export interface ScoreQuoteRow {
  rank: number | null
  driver_id: number
  truck_number: string
  score: number
  is_mid_route_candidate: boolean
  deadhead_miles: number
  eta_pickup: string
  order_revenue: number
  deadhead_cost: number
  opportunity_cost_penalty: number
  hos_stranding_risk_penalty: number
  maintenance_risk_penalty: number
  expected_lateness_penalty: number
  immediate_reward: number
}

export interface ScoreQuoteResult {
  quote_id: string
  top_n: ScoreQuoteRow[]
  all_scored: ScoreQuoteRow[]
  summary: {
    n_candidates: number
    n_feasible: number
    n_idle_candidates: number
    n_mid_route_candidates: number
    best_score: number | null
    worst_score: number | null
    median_score: number | null
    score_spread: number | null
    top_pick_is_mid_route: boolean | null
    decision_time: string
    requested_pickup_at: string
    n_excluded_for_inspection: number
    loaded_miles?: number
    load_fill_ratio?: number
    rate_per_mile?: number
    linehaul_amount?: number
    fuel_surcharge_amount?: number
    estimated_total_charge?: number
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    // FastAPI's HTTPException body is `{"detail": "message"}` -- surface just the message where
    // present (the Dispatch Board's toasts need a clean sentence, not a raw JSON blob).
    //
    // Real bug found directly: a non-JSON error body used to fall through to the RAW response
    // text verbatim -- if Vercel's own edge ever serves its standalone error page instead of
    // proxying through to the backend (a cold-start timeout on the Railway backend, a deploy in
    // progress, any edge-level hiccup), that's a full HTML document, and this threw it as the
    // Error's own message. Whatever caught that error then rendered that raw HTML text wherever
    // it normally shows a one-line error -- exactly "the page looks broken," when the real
    // backend never even saw the request. Falls back to a short, generic, always-safe message
    // instead of ever putting an unknown response body on screen.
    let detail: string | null = null
    try {
      const parsed = JSON.parse(text)
      if (typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      // not JSON -- this did NOT come from our own FastAPI backend (which always returns JSON
      // errors), so never show its raw body -- could be an edge/proxy error page, an HTML 500
      // from an unrelated layer, anything.
    }
    throw new Error(detail ?? `Request failed (${res.status}) -- please try again`)
  }
  return res.json() as Promise<T>
}

export interface OrderDetail {
  quote: {
    quote_id: string
    origin_location_id: number
    dest_location_id: number
    origin_location_id_label: string | null
    dest_location_id_label: string | null
    requested_at: string
    requested_pickup_at: string
    weight_lbs: number
    pallets: number
    load_type: string
    service_type: string
    status: string
  }
  trip: {
    trip_id: string
    status: string
    driver_id: number
    truck_number: string | null
    duty_status: string | null
  } | null
}

// Manual day-ahead Dispatch Board (dashboard/server/main.py's /api/dispatch/* -- backed by
// sim/live/dispatch_board.py). `date` is an ISO date string or the literal "tomorrow" (resolved
// server-side -- "a fleet manager always plans tomorrow's work today" is the product's own
// stated assumption, not a UI nicety).
export interface DispatchTruck {
  truck_number: string
  available: boolean
  unavailable_reason: string | null
  truck_type: string
  capacity_lbs: number
  capacity_pallets: number
  length_ft: number
  inside_height_ft: number
  width_in: number
  hub: string
}

export interface DispatchDriver {
  driver_id: number
  available: boolean
  unavailable_reason: string | null
  hos_driving_hours_remaining: number
  hos_duty_hours_remaining: number
  hos_cycle1_hours_remaining: number
  hos_cycle2_hours_remaining: number
  hub: string
}

export interface DispatchOrder {
  order_id: string
  pickup_city: string
  dest_city: string
  weight_lbs: number
  pallets: number
  load_type: string
  pickup_at: string
  delivery_eta: string
  rate: number
  pickup_location_id: number
  dest_location_id: number
  // Only set once Finalize Dispatch has created the real live.trips row -- null in draft.
  trip_id: string | null
  pickup_geofence_source: 'manual' | 'default'
  dropoff_geofence_source: 'manual' | 'default'
  // Real user ask: "how does the dispatch know the driver have accepted trips" -- set by the
  // driver's own Accept button in Driver Assist (POST /api/driver/accept-order), null until then.
  accepted_at: string | null
  // Real, computed diagnosis for why THIS order couldn't be matched (equipment type, capacity,
  // driver-hub reach, or a scheduling trade-off) -- null while the order is actually assigned.
  unassigned_reason: string | null
}

export interface TripGeofenceStop {
  location_id: number
  label: string
  city: string
  lat: number
  lon: number
  default_radius_m: number
  geofence_source: 'manual' | 'default'
  manual_geometry: { type: 'Polygon'; coordinates: [number, number][][] } | null
}

export interface TripDetail {
  trip_id: string
  driver_id: number
  status: string
  eta: string | null
  created_at: string | null
  planned_completion_at: string | null
  truck_number: string | null
  truck_type: string | null
  capacity_lbs: number | null
  capacity_pallets: number | null
  weight_lbs: number | null
  pallets: number | null
  load_type: string | null
  pickup: TripGeofenceStop
  dropoff: TripGeofenceStop
}

export interface DispatchAssignment {
  driver_id: number | null
  order_ids: string[]
}

export interface DispatchSetupResult {
  requested_hub_counts: Record<string, number>
  requested_type_shares: Record<string, number>
  requested_num_orders: number
  actual_hub_counts: Record<string, number> | null
  actual_type_counts: Record<string, number> | null
  actual_num_orders: number | null
}

export interface DispatchBoardData {
  day_id: string
  service_date: string
  status: 'draft' | 'finalized'
  trucks: DispatchTruck[]
  drivers: DispatchDriver[]
  orders: DispatchOrder[]
  assignments: Record<string, DispatchAssignment>
  // Only present on the payload returned right after a Simulate call, not on a plain board load.
  setup?: DispatchSetupResult
}

// Every mutation returns just the truck(s) that actually changed (the target, plus a source truck
// when an order/driver was re-dragged off it) -- the frontend merges these into local state
// directly instead of refetching the whole board. Real user feedback: a full load_board() refetch
// after every drop (several sequential queries against a REMOTE Supabase instance) made each drag
// feel like a ~2s delay.
export interface DispatchAssignmentRow extends DispatchAssignment {
  truck_number: string
}

// Live Ops "selected truck" full picture -- current trip already comes from api.fleet()
// (driver_status.current_trip_id), this covers past (completed) and future (queued 'scheduled')
// trips for that one driver.
export interface PastTrip {
  trip_id: string
  completed_at: string | null
  on_time: boolean | null
  origin_city: string | null
  dest_city: string | null
  weight_lbs: number | null
  pallets: number | null
  load_type: string | null
}

export interface FutureTrip {
  trip_id: string
  status: string
  eta: string | null
  planned_completion_at: string | null
  weight_lbs: number | null
  pallets: number | null
  load_type: string | null
  origin_city: string | null
  dest_city: string | null
}

export interface DriverTripHistory {
  past: PastTrip[]
  future: FutureTrip[]
}

export const api = {
  fleet: () => req<FleetDriver[]>('/fleet'),
  locations: (q: string) => req<LocationOption[]>(`/locations?q=${encodeURIComponent(q)}`),
  geocode: (query: string) => req<LocationOption>('/geocode', { method: 'POST', body: JSON.stringify({ query }) }),
  route: (fromId: number, toId: number) =>
    req<{ coordinates: [number, number][]; fallback?: boolean }>(`/route?from_location_id=${fromId}&to_location_id=${toId}`),
  scoreQuote: (body: {
    origin_location_id: number
    dest_location_id: number
    weight_lbs: number
    pallets: number
    load_type: string
    requested_pickup_at: string
    service_type?: string
  }) => req<ScoreQuoteResult>('/score-quote', { method: 'POST', body: JSON.stringify(body) }),
  assign: (quoteId: string, driverId: number) =>
    req<{ trip_id: string; driver_id: number; status: string }>('/assign', {
      method: 'POST',
      body: JSON.stringify({ quote_id: quoteId, driver_id: driverId }),
    }),
  // Order management (Dispatch page's "Manage Order" tab) -- look up, edit + re-score + reassign,
  // or cancel an existing order. Operates purely on live.*.
  getOrder: (quoteId: string) => req<OrderDetail>(`/orders/${quoteId}`),
  rescoreOrder: (quoteId: string, body: {
    requested_pickup_at?: string
    weight_lbs?: number
    pallets?: number
    load_type?: string
  }) => req<ScoreQuoteResult>(`/orders/${quoteId}/rescore`, { method: 'POST', body: JSON.stringify(body) }),
  cancelOrder: (quoteId: string) =>
    req<{ quote_id: string; status: string }>(`/orders/${quoteId}/cancel`, { method: 'POST' }),

  dispatchBoard: (date = 'tomorrow') => req<DispatchBoardData>(`/dispatch/${date}`),
  // Real user ask: no more silent auto-generation on first open -- a 404 here means "no day yet,
  // show the Setup panel" (not an error), so this reads status directly instead of throwing.
  dispatchBoardIfExists: async (date = 'tomorrow'): Promise<DispatchBoardData | null> => {
    const res = await fetch(`/api/dispatch/${date}`, { headers: { 'Content-Type': 'application/json' } })
    // Real bug found directly: our own FastAPI backend always returns JSON, including its 404s
    // (`{"detail": "..."}`) -- but if Vercel's edge itself can't reach the backend at all (a
    // redeploy mid-rollout, a cold-start timeout) it serves ITS OWN html "NOT_FOUND" page as a
    // 404, which this used to treat identically to a real "no dispatch day yet" 404 -- silently
    // showing the Setup panel instead of a real error. Content-Type distinguishes the two.
    const isRealAppResponse = (res.headers.get('content-type') ?? '').includes('application/json')
    if (res.status === 404 && isRealAppResponse) return null
    if (!res.ok) {
      const text = await res.text()
      let detail: string | null = null
      if (isRealAppResponse) {
        try {
          const parsed = JSON.parse(text)
          if (typeof parsed.detail === 'string') detail = parsed.detail
        } catch {
          // fall through -- treat as an unknown error below
        }
      }
      // Never show a non-JSON response body verbatim -- see req()'s own version of this fix.
      throw new Error(detail ?? `Request failed (${res.status}) -- please try again`)
    }
    return res.json() as Promise<DispatchBoardData>
  },
  // The Setup panel's "Simulate" button -- picks fleet size/hub mix/type mix/order count directly
  // instead of the app's old fixed 20-truck/55-30-15/calibrated-volume default.
  dispatchSimulate: (date: string, body: { hub_counts: Record<string, number>; type_shares: Record<string, number>; num_orders: number }) =>
    req<DispatchBoardData>(`/dispatch/${date}/simulate`, { method: 'POST', body: JSON.stringify(body) }),
  dispatchAssignOrder: (date: string, truckNumber: string, orderId: string) =>
    req<{ assignments: DispatchAssignmentRow[] }>(`/dispatch/${date}/assign-order`, {
      method: 'POST', body: JSON.stringify({ truck_number: truckNumber, order_id: orderId }),
    }),
  dispatchUnassignOrder: (date: string, truckNumber: string, orderId: string) =>
    req<{ assignments: DispatchAssignmentRow[] }>(`/dispatch/${date}/unassign-order`, {
      method: 'POST', body: JSON.stringify({ truck_number: truckNumber, order_id: orderId }),
    }),
  dispatchAssignDriver: (date: string, truckNumber: string, driverId: number) =>
    req<{ assignments: DispatchAssignmentRow[] }>(`/dispatch/${date}/assign-driver`, {
      method: 'POST', body: JSON.stringify({ truck_number: truckNumber, driver_id: driverId }),
    }),
  dispatchUnassignDriver: (date: string, truckNumber: string) =>
    req<{ assignments: DispatchAssignmentRow[] }>(`/dispatch/${date}/unassign-driver`, {
      method: 'POST', body: JSON.stringify({ truck_number: truckNumber }),
    }),
  dispatchAiAssign: (date: string) =>
    req<{
      assignments: Record<string, DispatchAssignment>
      num_assigned_orders: number
      num_unassigned_orders: number
      total_net_revenue: number
      deadhead_miles_total: number
      has_timeout: boolean
    }>(`/dispatch/${date}/ai-assign`, { method: 'POST' }),
  dispatchFinalize: (date: string) => req<{ status: string }>(`/dispatch/${date}/finalize`, { method: 'POST' }),
  dispatchReopen: (date: string) => req<{ status: string }>(`/dispatch/${date}/reopen`, { method: 'POST' }),
  dispatchReset: (date: string) =>
    req<{ assignments: Record<string, DispatchAssignment> }>(`/dispatch/${date}/reset`, { method: 'POST' }),
  // DEMO-ONLY: swaps in a genuinely different random order book for the same day.
  dispatchRegenerateOrders: (date: string) => req<DispatchBoardData>(`/dispatch/${date}/regenerate-orders`, { method: 'POST' }),

  driverTrips: (driverId: number) => req<DriverTripHistory>(`/drivers/${driverId}/trips`),

  tripDetail: (tripId: string) => req<TripDetail>(`/trips/${tripId}`),
  getTripGeofence: (tripId: string, locationId: number) =>
    req<{ default_radius_m: number; geofence_source: 'manual' | 'default'; manual_geometry: TripGeofenceStop['manual_geometry'] }>(
      `/trips/${tripId}/geofence?location_id=${locationId}`,
    ),
  saveTripGeofence: (tripId: string, locationId: number, points: [number, number][]) =>
    req<{ status: string }>(`/trips/${tripId}/geofence`, {
      method: 'POST', body: JSON.stringify({ location_id: locationId, points }),
    }),
  clearTripGeofence: (tripId: string, locationId: number) =>
    req<{ status: string }>(`/trips/${tripId}/geofence?location_id=${locationId}`, { method: 'DELETE' }),
}
