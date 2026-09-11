import { format } from 'date-fns'
import { Satellite } from 'lucide-react'
import * as React from 'react'
import { FleetMap, type RouteSegment } from '@/components/fleet-map'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { api, type FleetDriver, type LocationOption, type ScoreQuoteRow } from '@/lib/api'

// Real user feedback: picking a candidate should show "the whole plan" -- where they are now, if
// they're already mid-route where that trip ends, the reposition path from there to this job's
// pickup (new color), and the loaded path from pickup to delivery. Three real, distinct legs:
// 1. Current active leg remaining (mid-route only) -- straight line between live positions
//    (no location_id exists for "wherever they are right now mid-road" to route through OSRM).
// 2. Deadhead reposition: (mid-route landing spot, or idle position) -> this job's pickup --
//    real OSRM geometry via /api/route, same candidate-building logic score_quote.py itself uses.
// 3. Loaded: pickup -> delivery -- real OSRM geometry, identical for every candidate on this quote.
export function CandidateDetail({
  candidate,
  origin,
  dest,
  driver,
}: {
  candidate: ScoreQuoteRow
  origin: LocationOption
  dest: LocationOption
  driver: FleetDriver | undefined
}) {
  const [routes, setRoutes] = React.useState<RouteSegment[]>([])
  // Real user feedback / hackathon brief: "Satellite View toggle so dispatchers can visually
  // inspect dock layouts, driveway entrances, yard space" -- Live Ops already has this switch on
  // its own fleet map; a manager reviewing a QUOTE's candidates needs the same option here, to
  // check the pickup/delivery facility's real layout before committing to an assignment.
  const [satellite, setSatellite] = React.useState(false)

  const isMidRoute = driver?.current_trip_id != null
  const repositionFromId = isMidRoute ? driver?.dest_location_id : driver?.last_location_id

  React.useEffect(() => {
    let cancelled = false
    async function load() {
      const segments: RouteSegment[] = []
      if (repositionFromId != null) {
        try {
          const r = await api.route(repositionFromId, origin.location_id)
          segments.push({ coords: r.coordinates as [number, number][], color: '#6b7382', dashed: true })
        } catch {
          /* leave this leg undrawn if routing fails -- deadhead_miles/ETA figures are still real */
        }
      }
      try {
        const r = await api.route(origin.location_id, dest.location_id)
        segments.push({ coords: r.coordinates as [number, number][], color: '#2a5cdb' })
      } catch {
        /* same */
      }
      if (!cancelled) setRoutes(segments)
    }
    load()
    return () => {
      cancelled = true
    }
  }, [candidate.driver_id, origin.location_id, dest.location_id, repositionFromId])

  const currentLegLine: RouteSegment[] =
    driver?.current_trip_id && driver.lat != null && driver.lon != null && driver.dest_lat != null && driver.dest_lon != null
      ? [{ coords: [[driver.lon, driver.lat], [driver.dest_lon, driver.dest_lat]], color: '#97a0ad' }]
      : []

  const pins =
    driver?.dest_lat != null && driver?.dest_lon != null && driver.current_trip_id
      ? [{ lat: driver.dest_lat, lon: driver.dest_lon, color: '#97a0ad', label: "Driver's current stop" }]
      : []

  const allRoutes = [...currentLegLine, ...routes]
  const fitTo: [number, number][] = [
    ...(driver?.lat != null && driver?.lon != null ? [[driver.lat, driver.lon] as [number, number]] : []),
    ...allRoutes.flatMap((r) => r.coords.map(([lon, lat]) => [lat, lon] as [number, number])),
  ]

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-end">
        <label className="flex items-center gap-2 text-xs text-ink-600">
          <Satellite className="size-3.5" />
          Satellite — inspect dock/yard layout
          <Switch checked={satellite} onCheckedChange={setSatellite} />
        </label>
      </div>
      <div className="h-64 overflow-hidden rounded-lg border border-ink-200">
        <FleetMap drivers={driver ? [driver] : []} satellite={satellite} routes={allRoutes} pins={pins} fitTo={fitTo} />
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="rounded-lg border border-ink-200 p-2.5">
          <div className="text-ink-400">Current status</div>
          <div className="font-medium text-ink-800">{driver?.duty_status ?? '—'}</div>
        </div>
        <div className="rounded-lg border border-ink-200 p-2.5">
          <div className="text-ink-400">HOS remaining</div>
          <div className="font-medium text-ink-800">{driver?.hos_remaining_hours?.toFixed(1) ?? '—'}h</div>
        </div>
        <div className="rounded-lg border border-ink-200 p-2.5">
          <div className="text-ink-400">Deadhead to pickup</div>
          <div className="font-medium text-ink-800">{candidate.deadhead_miles} mi</div>
        </div>
        <div className="rounded-lg border border-ink-200 p-2.5">
          <div className="text-ink-400">ETA at pickup</div>
          <div className="font-medium text-ink-800">{format(new Date(candidate.eta_pickup), 'MMM d, h:mm a')}</div>
        </div>
      </div>

      {candidate.is_mid_route_candidate && (
        <p className="rounded-lg bg-ink-50 px-3 py-2 text-[11px] text-ink-500">
          <Badge tone="blue">mid-route</Badge> This driver is still finishing another trip (gray line above) — the
          gray dashed line is their reposition drive from that drop-off to this job's pickup, and the blue line is
          the loaded delivery itself.
        </p>
      )}
    </div>
  )
}
