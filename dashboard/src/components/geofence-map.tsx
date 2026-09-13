import 'leaflet/dist/leaflet.css'
import 'leaflet-draw/dist/leaflet.draw.css'
import * as L from 'leaflet'
// Real crash found and fixed directly (browser console, production build only: "Cannot read
// properties of undefined (reading 'Event')" thrown from inside this file's own DrawControl,
// at `L.Draw.Event.CREATED` -- reproduced on multiple browsers, not touch-specific, so the
// touchleave/touchExtend fix above was a real but SEPARATE bug, not this one). leaflet-draw is a
// legacy UMD package whose fallback path mutates a global `L` it expects to find -- see
// lib/leaflet-window-bridge.ts for exactly why this has to be a SEPARATE imported module rather
// than an inline statement here (a first attempt at that inline version was verified, against the
// real built bundle, to run 100,000+ characters too late to matter -- import hoisting).
import '@/lib/leaflet-window-bridge'
import 'leaflet-draw'
import { AlertTriangle, PenLine, Satellite, Undo2 } from 'lucide-react'
import * as React from 'react'
import { Circle, MapContainer, Marker, Polygon, Polyline, TileLayer, Tooltip, useMap } from 'react-leaflet'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Switch } from '@/components/ui/switch'

// Real crash found and fixed directly (user report: whole page went blank in the deployed app,
// worked fine locally): importing leaflet-draw globally registers `L.Map.TouchExtend` as a
// DEFAULT option (`L.Map.mergeOptions({ touchExtend: true })`) on the Leaflet Map class itself --
// affecting every <MapContainer> in the whole app the moment this module loads, not just this
// dialog's own map. That handler binds a `"touchleave"` DOM listener, which isn't a real browser
// event; Leaflet 1.9.x's own event validation throws `wrong event specified: touchleave` the
// instant it tries. This only fires on a TOUCH-CAPABLE browser/device (a touchscreen laptop,
// mobile, or DevTools device emulation) -- explains "works locally" (a mouse-only desktop never
// hits this code path) vs. crashing on whatever touch-capable setup hit the deployed app. This is
// a known, unmaintained leaflet-draw/modern-Leaflet incompatibility (leaflet-draw hasn't shipped a
// fix since 2022); the feature it powers (legacy touch vertex-dragging) is obsolete now that
// Leaflet core handles touch natively, so disabling it costs nothing real.
L.Map.mergeOptions({ touchExtend: false })

// Real user ask: a manager clicks "Edit Geofence" on a trip, the map lets them zoom in, then pick
// a pencil/draw tool from a side toolbar and draw the exact shape they want checked -- overwriting
// the default radius circle for that one trip's pickup or dropoff. Built on leaflet-draw (the
// standard Leaflet drawing-toolbar plugin) restricted to its polygon tool only -- closest standard
// equivalent to "pencil drawing" without inventing a new geometry model the backend (ST_Covers on
// a stored polygon, sim/sql/053) doesn't already support.

export interface GeofenceStop {
  location_id: number
  label: string
  lat: number
  lon: number
  default_radius_m: number
  geofence_source: 'manual' | 'default'
  manual_geometry: { type: 'Polygon'; coordinates: [number, number][][] } | null
}

function toLatLngs(stop: GeofenceStop): [number, number][] {
  // GeoJSON rings are [lon, lat] -- Leaflet/react-leaflet want [lat, lon].
  return (stop.manual_geometry?.coordinates[0] ?? []).map(([lon, lat]) => [lat, lon])
}

const pinIcon = (color: string) =>
  L.divIcon({
    className: '',
    html: `<div style="width:14px;height:14px;border-radius:50%;background:${color};border:2px solid white;box-shadow:0 0 0 1px rgba(0,0,0,.25)"></div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  })

/** Mounts leaflet-draw's own toolbar + FeatureGroup on the map, scoped to editing ONE stop at a
 * time. Not a react-leaflet component itself (leaflet-draw has no official React wrapper) -- reads
 * the live map instance via useMap() the same way react-leaflet's own internals do. */
function DrawControl({ active, onDrawn }: { active: boolean; onDrawn: (points: [number, number][]) => void }) {
  const map = useMap()
  const groupRef = React.useRef<L.FeatureGroup | null>(null)
  const controlRef = React.useRef<L.Control.Draw | null>(null)
  // Real bug found directly: `onDrawn` used to sit in this effect's dependency array. Every
  // caller passes a React state setter (stable by itself), but ANY parent re-render that recreates
  // the prop as a fresh inline function -- or React 18 StrictMode's dev-only double-invoke of
  // effects -- tore down this ENTIRE draw toolbar (removeControl/removeLayer) and rebuilt a brand
  // new empty one mid-draw, silently discarding whatever shape was already placed. That's exactly
  // "I drew it and it just went back to default" from the user's seat, with no error anywhere
  // because nothing actually failed -- the in-progress shape was correctly wiped by this effect's
  // OWN cleanup. A ref always calls the LATEST onDrawn without it being a dependency, so this
  // effect only tears down/rebuilds when `active` or the map instance itself actually change.
  const onDrawnRef = React.useRef(onDrawn)
  onDrawnRef.current = onDrawn

  React.useEffect(() => {
    if (!active) return
    const group = new L.FeatureGroup()
    map.addLayer(group)
    groupRef.current = group

    const control = new L.Control.Draw({
      position: 'topright',
      draw: {
        // allowIntersection: true -- real bug found directly: leaflet-draw SILENTLY refuses to add
        // a new vertex if the false setting would make the in-progress shape self-intersect, which
        // is very easy to trigger by accident on a small, precisely-placed geofence while zoomed
        // in -- looked exactly like "it only lets me draw 3 points." A geofence doesn't need to be
        // a strictly simple polygon to be useful as a rough outline; letting the manager keep
        // placing points (5+) matters more here than rejecting a technically self-intersecting one.
        polygon: { allowIntersection: true, showArea: true, shapeOptions: { color: '#d97757' } },
        polyline: false, rectangle: false, circle: false, circlemarker: false, marker: false,
      },
      edit: { featureGroup: group, remove: true },
    })
    map.addControl(control)
    controlRef.current = control

    const handleCreated = (e: L.LeafletEvent) => {
      group.clearLayers()
      const layer = (e as L.DrawEvents.Created).layer as L.Polygon
      group.addLayer(layer)
      const latlngs = (layer.getLatLngs()[0] as L.LatLng[]).map((p): [number, number] => [p.lat, p.lng])
      onDrawnRef.current(latlngs)
    }
    const handleEdited = (e: L.LeafletEvent) => {
      const layers = (e as L.DrawEvents.Edited).layers
      layers.eachLayer((layer) => {
        const latlngs = ((layer as L.Polygon).getLatLngs()[0] as L.LatLng[]).map((p): [number, number] => [p.lat, p.lng])
        onDrawnRef.current(latlngs)
      })
    }
    map.on(L.Draw.Event.CREATED, handleCreated)
    map.on(L.Draw.Event.EDITED, handleEdited)

    return () => {
      map.off(L.Draw.Event.CREATED, handleCreated)
      map.off(L.Draw.Event.EDITED, handleEdited)
      map.removeControl(control)
      map.removeLayer(group)
    }
  }, [active, map])

  return null
}

function FlyTo({ lat, lon, zoom }: { lat: number; lon: number; zoom: number }) {
  const map = useMap()
  React.useEffect(() => {
    map.flyTo([lat, lon], zoom, { duration: 0.6 })
  }, [lat, lon, zoom, map])
  return null
}

/** Same free, keyless Esri tile providers fleet-map.tsx already uses (no Mapbox/Google key in
 * .env) -- real user ask: a manager drawing a geofence by hand may want imagery, not just street
 * cartography, to see the actual dock/lot shape. */
function GeofenceTileLayer({ satellite }: { satellite: boolean }) {
  return satellite ? (
    <TileLayer attribution="Tiles &copy; Esri" url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}" />
  ) : (
    <TileLayer attribution="Tiles &copy; Esri" url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}" maxZoom={19} />
  )
}

function SatelliteToggle({ satellite, onChange }: { satellite: boolean; onChange: (v: boolean) => void }) {
  // Real bug found directly: this used to sit top-right (`right-2 top-2`) -- the EXACT corner
  // leaflet-draw's own toolbar (DrawControl above, `position: 'topright'`) mounts its polygon/edit
  // buttons in, so the moment "Edit Geofence" turned drawing on, the two controls stacked on top
  // of each other. Leaflet's default zoom control already owns top-left and its attribution strip
  // owns bottom-right, so bottom-left is the one corner nothing else claims.
  return (
    <label className="absolute bottom-2 left-2 z-[500] flex items-center gap-1.5 rounded-lg border border-ink-200 bg-white/95 px-2.5 py-1.5 text-xs text-ink-600 shadow-lg backdrop-blur">
      <Satellite className="size-3.5" />
      Satellite
      <Switch checked={satellite} onCheckedChange={onChange} />
    </label>
  )
}

export function GeofenceMap({
  pickup, dropoff, routeCoords, editingStop, hasDraftShape, onDrawnChange,
}: {
  pickup: GeofenceStop
  dropoff: GeofenceStop
  routeCoords: [number, number][] | null
  editingStop: GeofenceStop | null
  // Real user ask: while actively drawing, keep showing the EXISTING (default or manual)
  // geofence as a visual reference to trace against -- only actually replace it once the manager
  // has placed a real new shape (leaflet-draw's own preview layer renders the in-progress shape
  // on top regardless, so there's no visual conflict either way).
  hasDraftShape: boolean
  onDrawnChange: (points: [number, number][]) => void
}) {
  const [satellite, setSatellite] = React.useState(false)
  const center: [number, number] = editingStop ? [editingStop.lat, editingStop.lon] : [pickup.lat, pickup.lon]

  return (
    <div className="relative h-full w-full">
    <MapContainer center={center} zoom={13} className="h-full w-full" scrollWheelZoom>
      <GeofenceTileLayer satellite={satellite} />
      {editingStop && <FlyTo lat={editingStop.lat} lon={editingStop.lon} zoom={16} />}

      {/* routeCoords comes straight from /api/route -- [lon, lat] GeoJSON order (see fleet-map.tsx's
          own RouteSegment convention); react-leaflet's Polyline needs [lat, lon], swapped here.
          Real bug found directly: without this swap the line renders far off the visible map --
          "just shows the start point" is what that looks like once everything else is out of view. */}
      {routeCoords && routeCoords.length > 1 && (
        <Polyline positions={routeCoords.map(([lon, lat]) => [lat, lon])} pathOptions={{ color: '#2a78d6', weight: 3, opacity: 0.7 }} />
      )}

      {([
        [pickup, '#1baf7a', 'Pickup'],
        [dropoff, '#eb6834', 'Dropoff'],
      ] as const).map(([stop, color, label]) => {
        const isBeingEdited = editingStop?.location_id === stop.location_id
        return (
          <React.Fragment key={stop.location_id}>
            <Marker position={[stop.lat, stop.lon]} icon={pinIcon(color)}>
              <Tooltip>{label}: {stop.label}</Tooltip>
            </Marker>
            {/* Stays visible as a reference EVEN while this stop is being drawn on -- only hidden
                once a real new draft shape actually exists (see hasDraftShape above). */}
            {!(isBeingEdited && hasDraftShape) && stop.geofence_source === 'manual' && stop.manual_geometry && (
              <Polygon positions={toLatLngs(stop)} pathOptions={{ color, fillOpacity: 0.15 }} />
            )}
            {!(isBeingEdited && hasDraftShape) && stop.geofence_source === 'default' && (
              <Circle center={[stop.lat, stop.lon]} radius={stop.default_radius_m} pathOptions={{ color, fillOpacity: 0.1, dashArray: '4' }} />
            )}
          </React.Fragment>
        )
      })}

      <DrawControl active={!!editingStop} onDrawn={onDrawnChange} />
    </MapContainer>
    <SatelliteToggle satellite={satellite} onChange={setSatellite} />
    </div>
  )
}

/** Single-stop version -- used by the Simulation Trip demo page (real user ask: the same drawing
 * capability the real dispatch trip detail page has, for BOTH pickup and dropoff there too, not
 * just the existing fixed dropoff-only circle). Self-contained: fetches the stop's current
 * geofence status, lets the manager draw/redraw/revert it, opened as its own small dialog rather
 * than folded into the demo's live-animation map (that map's own truck-position rendering stays
 * untouched -- this is a deliberately separate, focused editing surface). */
export function GeofenceEditDialog({
  open, onOpenChange, tripId, locationId, label, lat, lon, color, routeCoords, otherStop, onSaved,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  tripId: string
  locationId: number
  label: string
  lat: number
  lon: number
  color: string
  // Real user ask: without seeing the OTHER end of the trip and the road between them, there's no
  // spatial context for where/how to draw -- both optional so this dialog still works standalone
  // if a caller genuinely has neither (falls back to just the one stop, as before).
  routeCoords?: [number, number][] | null
  otherStop?: { label: string; lat: number; lon: number } | null
  // Real bug found directly: a caller showing this SAME geofence elsewhere (e.g. the Simulation
  // Trip page's own live map) had no way to know a save/revert just happened -- this dialog stays
  // open after a successful save (so the manager sees the result before closing), so "wait for
  // onOpenChange(false)" never fires at the right moment. Called after both a successful save AND
  // a successful revert.
  onSaved?: () => void
}) {
  const [stop, setStop] = React.useState<GeofenceStop | null>(null)
  const [loading, setLoading] = React.useState(false)
  const [drawing, setDrawing] = React.useState(false)
  const [drawnPoints, setDrawnPoints] = React.useState<[number, number][] | null>(null)
  const [saving, setSaving] = React.useState(false)
  const [satellite, setSatellite] = React.useState(false)
  // Same silent-failure gap fixed on the real Dispatch Board's TripDetailDialog: a failed save had
  // no on-screen sign anything went wrong, which looks exactly like "I drew a shape and it just
  // went back to default."
  const [saveError, setSaveError] = React.useState<string | null>(null)

  const load = React.useCallback(async () => {
    setLoading(true)
    try {
      const status = await api.getTripGeofence(tripId, locationId)
      setStop({ location_id: locationId, label, lat, lon, ...status })
    } finally {
      setLoading(false)
    }
  }, [tripId, locationId, label, lat, lon])

  React.useEffect(() => {
    if (open) {
      setDrawing(false)
      setDrawnPoints(null)
      setSaveError(null)
      void load()
    }
  }, [open, load])

  async function handleSave() {
    if (!drawnPoints || drawnPoints.length < 3) return
    setSaving(true)
    setSaveError(null)
    try {
      await api.saveTripGeofence(tripId, locationId, drawnPoints)
      setDrawing(false)
      setDrawnPoints(null)
      await load()
      onSaved?.()
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Could not save this geofence')
    } finally {
      setSaving(false)
    }
  }

  async function handleRevert() {
    try {
      await api.clearTripGeofence(tripId, locationId)
      await load()
      onSaved?.()
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Could not revert this geofence')
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            Geofence · {label}
            {stop && <Badge tone={stop.geofence_source === 'manual' ? 'blue' : 'gray'}>{stop.geofence_source === 'manual' ? 'Manual' : 'Default'}</Badge>}
          </DialogTitle>
        </DialogHeader>
        {loading || !stop ? (
          <div className="flex h-80 items-center justify-center text-ink-400">Loading…</div>
        ) : (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              {drawing ? (
                <>
                  <span className="w-full text-xs font-medium text-ink-500">
                    {drawnPoints && drawnPoints.length >= 3
                      ? `${drawnPoints.length} point shape drawn — ready to save`
                      : 'Use the draw tool (top-right of the map): click each corner, click the first point again to close the shape.'}
                  </span>
                  <Button onClick={() => void handleSave()} disabled={!drawnPoints || drawnPoints.length < 3 || saving} className="text-white justify-center py-2.5 text-[14px] font-semibold" style={{ backgroundColor: color }}>
                    <PenLine className="size-4" /> {saving ? 'Saving…' : 'Save Geofence'}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => { setDrawing(false); setDrawnPoints(null) }}>Cancel</Button>
                </>
              ) : (
                <>
                  <Button size="sm" variant="outline" onClick={() => { setDrawing(true); setDrawnPoints(null) }}>
                    <PenLine className="size-3.5" /> {stop.geofence_source === 'manual' ? 'Redraw Geofence' : 'Draw Custom Geofence'}
                  </Button>
                  {stop.geofence_source === 'manual' && (
                    <Button size="sm" variant="outline" onClick={() => void handleRevert()}>
                      <Undo2 className="size-3.5" /> Revert to Default
                    </Button>
                  )}
                </>
              )}
            </div>
            {saveError && (
              <div className="mb-3 flex items-start gap-1.5 rounded-lg bg-status-red-100 px-3 py-2 text-xs text-status-red-700">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                <span>{saveError}</span>
              </div>
            )}
            <div className="relative h-[420px] overflow-hidden rounded-lg border border-ink-200">
              <MapContainer center={[stop.lat, stop.lon]} zoom={16} className="h-full w-full" scrollWheelZoom>
                <GeofenceTileLayer satellite={satellite} />
                {routeCoords && routeCoords.length > 1 && (
                  <Polyline positions={routeCoords.map(([lon, latP]) => [latP, lon])} pathOptions={{ color: '#2a78d6', weight: 3, opacity: 0.7 }} />
                )}
                <Marker position={[stop.lat, stop.lon]} icon={pinIcon(color)}><Tooltip>{stop.label}</Tooltip></Marker>
                {otherStop && (
                  <Marker position={[otherStop.lat, otherStop.lon]} icon={pinIcon('#6b7382')}><Tooltip>{otherStop.label}</Tooltip></Marker>
                )}
                {/* Stays visible as a reference even while actively drawing -- only hidden once a
                    real new shape has actually been placed (real user ask). */}
                {!(drawing && drawnPoints && drawnPoints.length >= 3) && stop.geofence_source === 'manual' && stop.manual_geometry && (
                  <Polygon positions={toLatLngs(stop)} pathOptions={{ color, fillOpacity: 0.15 }} />
                )}
                {!(drawing && drawnPoints && drawnPoints.length >= 3) && stop.geofence_source === 'default' && (
                  <Circle center={[stop.lat, stop.lon]} radius={stop.default_radius_m} pathOptions={{ color, fillOpacity: 0.1, dashArray: '4' }} />
                )}
                <DrawControl active={drawing} onDrawn={setDrawnPoints} />
              </MapContainer>
              <SatelliteToggle satellite={satellite} onChange={setSatellite} />
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
