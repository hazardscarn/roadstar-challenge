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
    throw new Error(`${res.status} ${text}`)
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
}
