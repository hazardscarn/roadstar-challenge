import 'leaflet/dist/leaflet.css'
import * as L from 'leaflet'
import * as React from 'react'
import { Circle, CircleMarker, MapContainer, Marker, Polyline, Popup, TileLayer, Tooltip, useMap } from 'react-leaflet'
import type { FleetDriver } from '@/lib/api'
import { truckIcon } from '@/lib/truck-icon'

// Southern Ontario coverage bounding box (documents/1788654151601_Hackathon_Project_Brief.pdf
// Section 2: Barrie/Peterborough/Pickering/London/Niagara Falls) -- default view before any
// fleet data loads.
const SOUTHERN_ONTARIO_CENTER: [number, number] = [43.55, -80.0]
const SOUTHERN_ONTARIO_ZOOM = 8

// Real ELD-app-derived status color convention (see plan's UI design grounding / map-legend.tsx).
const STATUS_COLOR: Record<string, string> = {
  driving: '#1f9d55',
  on_duty_not_driving: '#e0940f',
  off_duty: '#6b7382',
  out_of_service: '#d9342b',
  breakdown: '#d9342b',
  // Synthetic-only status, never written by real fleet data -- the Simulation Trip demo sets this
  // on its one client-side driver marker the instant the geofence trigger confirms an arrival, so
  // the truck visibly changes color the moment the real buffered mechanism fires (not just when
  // it happens to be stationary).
  geofence_triggered: '#7c3aed',
}

function statusColor(driver: FleetDriver) {
  if (!driver.inspection_ok) return '#d9342b'
  if (driver.hos_remaining_hours < 2) return '#d9342b'
  return STATUS_COLOR[driver.duty_status] ?? '#6b7382'
}

/** Real user feedback: several idle drivers can share the exact same seeded/real position (a
 * yard, a hub) -- without this they render as one marker stacked on top of another. Spreads
 * exact-coincident points into a small deterministic circle around the shared point, same
 * approach real fleet-map products use for depot clustering. */
function jitter(drivers: FleetDriver[]): Map<number, [number, number]> {
  const groups = new Map<string, FleetDriver[]>()
  for (const d of drivers) {
    if (d.lat == null || d.lon == null) continue
    const key = `${d.lat.toFixed(4)},${d.lon.toFixed(4)}`
    ;(groups.get(key) ?? groups.set(key, []).get(key)!).push(d)
  }
  const out = new Map<number, [number, number]>()
  for (const group of groups.values()) {
    const n = group.length
    group.forEach((d, i) => {
      if (n === 1) {
        out.set(d.driver_id, [d.lat!, d.lon!])
        return
      }
      const angle = (2 * Math.PI * i) / n
      const radiusDeg = 0.004 // ~400m at this latitude -- enough to visually separate, not enough to mislead position
      out.set(d.driver_id, [d.lat! + radiusDeg * Math.sin(angle), d.lon! + radiusDeg * Math.cos(angle)])
    })
  }
  return out
}

function FitToMarkers({ points, maxZoom = 12 }: { points: [number, number][]; maxZoom?: number }) {
  const map = useMap()
  const key = points.map((p) => p.join(',')).join('|')
  React.useEffect(() => {
    if (points.length === 0) return
    if (points.length === 1) {
      map.setView(points[0], maxZoom)
      return
    }
    map.fitBounds(L.latLngBounds(points), { padding: [60, 60], maxZoom })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, map, maxZoom])
  return null
}

export interface RouteSegment {
  coords: [number, number][] // [lon, lat] pairs, GeoJSON order (from /api/route)
  color: string
  dashed?: boolean
  /** Line weight in px -- defaults to 4. Bump for a segment that needs to stand out (e.g. the
   * Order Story's "this order" leg among adjacent previous/next-trip context lines). */
  weight?: number
}

export interface MapPin {
  lat: number
  lon: number
  color: string
  label?: string
  /** A short tag (e.g. "P1", "D2") shown ALWAYS next to the pin, not just on click -- for a map
   * with several same-colored stops (Order Story's cycle view) where a reader needs to tell which
   * dot is which stop at a glance. Separate from `label` (still click-to-reveal, for longer text). */
  permanentLabel?: string
}

export interface GeofenceCircle {
  lat: number
  lon: number
  radiusM: number
  label: string
  /** Highlights the circle (filled, brighter) -- e.g. the Simulation Trip demo sets this the
   * instant the buffered geofence trigger confirms the truck is actually inside, so "entered the
   * geofence" is visible on the map itself, not just in a text alert. */
  active?: boolean
}

export interface FleetMapProps {
  drivers: FleetDriver[]
  selectedDriverId?: number | null
  onSelectDriver?: (id: number) => void
  satellite: boolean
  /** Route polylines to draw -- e.g. a gray dashed deadhead leg + a blue solid loaded leg. */
  routes?: RouteSegment[]
  /** Extra standalone markers beyond the fleet (e.g. a quote's origin/destination pins). */
  pins?: MapPin[]
  /** Geofence radius circles (reference.locations.radius_m) around a stop, with dwell info in the popup. */
  geofences?: GeofenceCircle[]
  /** Refit the view to these points when they change (defaults to all driver positions). */
  fitTo?: [number, number][]
  /** Zoom level (or max zoom, when fitting multiple points) used when refitting to `fitTo` --
   * defaults to 12. Pass something tighter (15-17) to focus in on a small area, e.g. a geofence
   * the Simulation Trip demo wants visibly filling the frame during arrival/departure. */
  focusZoom?: number
  onMapClick?: (lat: number, lon: number) => void
}

function ClickCapture({ onMapClick }: { onMapClick?: (lat: number, lon: number) => void }) {
  const map = useMap()
  React.useEffect(() => {
    if (!onMapClick) return
    const handler = (e: L.LeafletMouseEvent) => onMapClick(e.latlng.lat, e.latlng.lng)
    map.on('click', handler)
    return () => {
      map.off('click', handler)
    }
  }, [map, onMapClick])
  return null
}

export function FleetMap({ drivers, selectedDriverId, onSelectDriver, satellite, routes, pins, geofences, fitTo, focusZoom, onMapClick }: FleetMapProps) {
  const positions = React.useMemo(() => jitter(drivers), [drivers])
  const driverPoints = Array.from(positions.values())
  const fitPoints = fitTo ?? driverPoints

  return (
    <MapContainer center={SOUTHERN_ONTARIO_CENTER} zoom={SOUTHERN_ONTARIO_ZOOM} className="h-full w-full" zoomControl={false}>
      {satellite ? (
        // Esri World Imagery -- free, keyless satellite tiles (no Mapbox/Google key in .env,
        // per the confirmed "Leaflet + free tiles" decision).
        <TileLayer
          attribution="Tiles &copy; Esri"
          url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
        />
      ) : (
        // Esri World Street Map -- free, keyless, real road-map cartography (labeled highway
        // shields, road-class hierarchy) -- same provider/pattern already proven working for the
        // satellite layer below, just its street-map service instead of imagery. (CARTO's
        // raster tiles started requiring an API key -- caught live, a watermarked map is worse
        // than the plain OSM default it was meant to improve on, so switched providers rather
        // than accept that regression.)
        <TileLayer
          attribution="Tiles &copy; Esri"
          url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
          maxZoom={19}
        />
      )}

      <FitToMarkers points={fitPoints} maxZoom={focusZoom} />
      <ClickCapture onMapClick={onMapClick} />

      {drivers.map((d) => {
        const pos = positions.get(d.driver_id)
        if (!pos) return null
        return (
          <Marker
            key={d.driver_id}
            position={pos}
            icon={truckIcon(statusColor(d), d.driver_id === selectedDriverId)}
            eventHandlers={onSelectDriver ? { click: () => onSelectDriver(d.driver_id) } : undefined}
          >
            <Popup>
              <div className="text-xs font-medium">Driver #{d.driver_id}</div>
              <div className="text-xs text-ink-500">Truck {d.truck_number} · {d.duty_status}</div>
            </Popup>
          </Marker>
        )
      })}

      {pins?.map((pin, i) => (
        <CircleMarker
          // eslint-disable-next-line react/no-array-index-key
          key={i}
          center={[pin.lat, pin.lon]}
          radius={7}
          pathOptions={{ color: '#fff', weight: 2, fillColor: pin.color, fillOpacity: 1 }}
        >
          {pin.label && <Popup>{pin.label}</Popup>}
          {pin.permanentLabel && (
            <Tooltip permanent direction="top" offset={[0, -6]} className="!rounded !border-0 !bg-ink-900/90 !px-1.5 !py-0.5 !text-[10px] !font-bold !text-white !shadow">
              {pin.permanentLabel}
            </Tooltip>
          )}
        </CircleMarker>
      ))}

      {geofences?.map((g, i) => (
        <Circle
          // eslint-disable-next-line react/no-array-index-key
          key={i}
          center={[g.lat, g.lon]}
          radius={g.radiusM}
          pathOptions={
            g.active
              ? { color: '#7c3aed', weight: 2.5, fillColor: '#7c3aed', fillOpacity: 0.28 }
              : { color: '#2a5cdb', weight: 1.5, fillOpacity: 0.08, dashArray: '4 4' }
          }
        >
          <Popup>{g.label}</Popup>
        </Circle>
      ))}

      {routes?.map((route, i) =>
        route.coords.length > 1 ? (
          <Polyline
            // eslint-disable-next-line react/no-array-index-key
            key={i}
            positions={route.coords.map(([lon, lat]) => [lat, lon])}
            pathOptions={{ color: route.color, weight: route.weight ?? 4, opacity: 0.9, dashArray: route.dashed ? '8 8' : undefined }}
          />
        ) : null,
      )}
    </MapContainer>
  )
}
