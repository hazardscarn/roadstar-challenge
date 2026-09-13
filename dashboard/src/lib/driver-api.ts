import { supabase } from '@/lib/supabase'

// Real user ask, built under a 1-hour time limit: "Driver Assist" reads today's real Dispatch
// Board assignment through dashboard/server/main.py's new /api/driver/* endpoints (not direct
// Supabase table access -- dispatch.* has RLS enabled with zero policies, same architecture as
// the rest of this app). Every call attaches the driver's own real Supabase session token; the
// backend verifies it server-side before running any query, never trusts a client-supplied id.

async function authedReq<T>(path: string, init?: RequestInit): Promise<T> {
  const { data } = await supabase.auth.getSession()
  const token = data.session?.access_token
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init?.headers },
  })
  if (!res.ok) {
    const text = await res.text()
    try {
      throw new Error(JSON.parse(text).detail ?? text)
    } catch {
      throw new Error(text)
    }
  }
  return res.json()
}

export interface DriverTrip {
  order_id: string
  pickup_location_id: number
  dest_location_id: number
  pickup_label: string
  dest_label: string
  weight_lbs: number
  pallets: number
  load_type: string
  rate: number
  pickup_at: string
  delivery_eta: string | null
  accepted_at: string | null
}

export interface DriverToday {
  service_date: string
  day_status: string | null
  truck_number: string | null
  trips: DriverTrip[]
}

export function driverToday(day?: string): Promise<DriverToday> {
  return authedReq(`/driver/today${day ? `?day=${day}` : ''}`)
}

export function acceptOrder(orderId: string): Promise<{ order_id: string; accepted_at: string }> {
  return authedReq('/driver/accept-order', { method: 'POST', body: JSON.stringify({ order_id: orderId }) })
}

export type DutyStatus = 'off_duty' | 'sleeper_berth' | 'driving' | 'on_duty_not_driving'

export interface DutyLogEntry {
  status: DutyStatus
  logged_at: string
  odometer_km: number | null
  note: string | null
}

export function logDutyStatus(status: DutyStatus, odometerKm?: number, note?: string): Promise<DutyLogEntry> {
  return authedReq('/driver/duty-status', { method: 'POST', body: JSON.stringify({ status, odometer_km: odometerKm ?? null, note: note ?? null }) })
}

export function dutyLog(): Promise<{ entries: DutyLogEntry[] }> {
  return authedReq('/driver/duty-log')
}

export interface InspectionForm {
  brakes_ok: boolean
  tires_ok: boolean
  lights_ok: boolean
  fluid_levels_ok: boolean
  coupling_ok: boolean
  trailer_ok: boolean
  odometer_km: number | null
  defects_noted: string | null
}

export function submitInspection(form: InspectionForm): Promise<{ truck_number: string | null; overall_pass: boolean; submitted_at: string }> {
  return authedReq('/driver/inspection', { method: 'POST', body: JSON.stringify(form) })
}

export function lastInspection(): Promise<{ submitted_at: string | null; overall_pass: boolean | null }> {
  return authedReq('/driver/last-inspection')
}
