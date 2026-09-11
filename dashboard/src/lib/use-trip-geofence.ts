import * as React from 'react'
import type { GeofenceCircle } from '@/components/fleet-map'
import { supabase } from '@/lib/supabase'

export interface GeofenceSummaryRow {
  location_id: number
  label: string
  lat: number
  lon: number
  radius_m: number
  arrived_at: string | null
  departed_at: string | null
}

// Real user feedback: a selected trip needs to visibly show its geofences (the brief's actual
// "critical requirement") -- radius circle around each real stop, with real arrival/departure
// timestamps, not just a database row nobody sees. Reads live.geofence_events + reference.locations
// directly (both RLS-exposed for the manager role, sim/sql/032/034/035) -- no backend round-trip.
export function useTripGeofence(tripId: string | null) {
  const [rows, setRows] = React.useState<GeofenceSummaryRow[]>([])

  React.useEffect(() => {
    if (!tripId) {
      setRows([])
      return
    }
    let cancelled = false
    ;(async () => {
      const { data: events } = await supabase
        .schema('live')
        .from('geofence_events')
        .select('event_type,occurred_at,location_id')
        .eq('trip_id', tripId)
        .order('occurred_at', { ascending: true })
      if (!events || events.length === 0) {
        if (!cancelled) setRows([])
        return
      }
      const locIds = Array.from(new Set(events.map((e) => e.location_id as number)))
      const { data: locs } = await supabase
        .schema('reference')
        .from('locations')
        .select('location_id,label,radius_m,lat,lon')
        .in('location_id', locIds)
      const byId = new Map((locs ?? []).map((l) => [l.location_id, l]))

      const summary: GeofenceSummaryRow[] = []
      for (const locId of locIds) {
        const loc = byId.get(locId)
        if (!loc) continue
        const arrival = events.find((e) => e.location_id === locId && e.event_type === 'arrival')
        const departure = events.find((e) => e.location_id === locId && e.event_type === 'departure')
        summary.push({
          location_id: locId,
          label: loc.label as string,
          lat: loc.lat as number,
          lon: loc.lon as number,
          radius_m: (loc.radius_m as number) ?? 120,
          arrived_at: arrival?.occurred_at ?? null,
          departed_at: departure?.occurred_at ?? null,
        })
      }
      if (!cancelled) setRows(summary)
    })()
    return () => {
      cancelled = true
    }
  }, [tripId])

  const circles: GeofenceCircle[] = rows
    .filter((r) => r.lat != null && r.lon != null)
    .map((r) => ({ lat: r.lat, lon: r.lon, radiusM: r.radius_m, label: r.label }))

  return { rows, circles }
}
