import { format } from 'date-fns'
import { AlertTriangle, List, Map as MapIcon, Satellite, Truck, X } from 'lucide-react'
import * as React from 'react'
import { DispatchFeed } from '@/components/dispatch-feed'
import { DriverSearch } from '@/components/driver-search'
import { FleetMap, type RouteSegment } from '@/components/fleet-map'
import { FleetTable } from '@/components/fleet-table'
import { KpiBand, KpiTile } from '@/components/kpi-band'
import { MapLegend, QuoteStatusLegend } from '@/components/map-legend'
import { PageHeader } from '@/components/page-header'
import { TripStatusHistory } from '@/components/trip-status-history'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { api, type FleetDriver } from '@/lib/api'
import { SCHEDULE_LABEL, scheduleStatus } from '@/lib/schedule-status'
import { useTripGeofence } from '@/lib/use-trip-geofence'
import { cn } from '@/lib/utils'

const FLEET_POLL_MS = 5000 // local backend, cheap -- see fleet-map.tsx's note on why polling over Realtime here

export default function LiveOps() {
  const [drivers, setDrivers] = React.useState<FleetDriver[]>([])
  const [view, setView] = React.useState<'map' | 'table'>('map')
  const [satellite, setSatellite] = React.useState(false)
  const [selectedId, setSelectedId] = React.useState<number | null>(null)
  const [tab, setTab] = React.useState('feed')
  const [routeCoords, setRouteCoords] = React.useState<[number, number][] | undefined>()
  const [statusFilter, setStatusFilter] = React.useState<'all' | 'active'>('all')
  const [focusMode, setFocusMode] = React.useState(false)

  const refreshFleet = React.useCallback(() => {
    api.fleet().then(setDrivers).catch((e) => console.error('fleet poll failed', e))
  }, [])

  React.useEffect(() => {
    refreshFleet()
    const id = setInterval(refreshFleet, FLEET_POLL_MS)
    return () => clearInterval(id)
  }, [refreshFleet])

  const selected = drivers.find((d) => d.driver_id === selectedId) ?? null
  const { circles: geofenceCircles, rows: geofenceRows } = useTripGeofence(selected?.current_trip_id ?? null)

  React.useEffect(() => {
    if (!selected?.current_trip_id) {
      setRouteCoords(undefined)
      return
    }
    const fromId = selected.origin_location_id ?? selected.last_location_id
    const toId = selected.dest_location_id
    if (fromId == null || toId == null) {
      setRouteCoords(undefined)
      return
    }
    api.route(fromId, toId).then((r) => setRouteCoords(r.coordinates.map(([lon, lat]) => [lon, lat]))).catch(() => setRouteCoords(undefined))
  }, [selected])

  function selectDriver(id: number, opts?: { focus?: boolean }) {
    setSelectedId(id)
    setTab('selected')
    setView('map')
    if (opts?.focus) setFocusMode(true)
  }

  const isActive = (d: FleetDriver) => d.duty_status === 'driving' || d.duty_status === 'on_duty_not_driving'
  const driving = drivers.filter((d) => d.duty_status === 'driving').length
  const hosCritical = drivers.filter((d) => d.hos_remaining_hours < 2).length
  const inspectionIssues = drivers.filter((d) => !d.inspection_ok).length

  const visibleDrivers = focusMode && selected ? [selected] : statusFilter === 'active' ? drivers.filter(isActive) : drivers

  const scheduleState = selected ? scheduleStatus(selected.eta) : null
  const routes: RouteSegment[] | undefined =
    routeCoords && selected
      ? [{
          coords: routeCoords,
          color: scheduleState === 'late' ? '#d9342b' : scheduleState === 'at_risk' ? '#e0940f' : '#1f9d55',
        }]
      : undefined
  const pins =
    selected?.origin_lat != null && selected?.origin_lon != null
      ? [
          { lat: selected.origin_lat, lon: selected.origin_lon, color: '#6b7382', label: 'Origin' },
          ...(selected.dest_lat != null && selected.dest_lon != null
            ? [{ lat: selected.dest_lat, lon: selected.dest_lon, color: '#2a5cdb', label: 'Destination' }]
            : []),
        ]
      : undefined

  return (
    <div className="flex h-screen flex-col">
      <PageHeader
        title="Live Ops"
        description="Southern Ontario fleet — track, monitor, drill in."
        actions={
          <div className="flex items-center gap-4">
            <DriverSearch drivers={drivers} onSelect={(id) => selectDriver(id)} />
            <div className="flex rounded-lg bg-ink-100 p-1">
              <button
                onClick={() => setStatusFilter('all')}
                className={cn('rounded-md px-2.5 py-1 text-xs font-medium', statusFilter === 'all' ? 'bg-white shadow-sm text-ink-900' : 'text-ink-500')}
              >
                All ({drivers.length})
              </button>
              <button
                onClick={() => setStatusFilter('active')}
                className={cn('rounded-md px-2.5 py-1 text-xs font-medium', statusFilter === 'active' ? 'bg-white shadow-sm text-ink-900' : 'text-ink-500')}
              >
                Active only ({drivers.filter(isActive).length})
              </button>
            </div>
            <div className="flex rounded-lg bg-ink-100 p-1">
              <button
                onClick={() => setView('map')}
                className={cn('flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium', view === 'map' ? 'bg-white shadow-sm text-ink-900' : 'text-ink-500')}
              >
                <MapIcon className="size-3.5" /> Map
              </button>
              <button
                onClick={() => setView('table')}
                className={cn('flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium', view === 'table' ? 'bg-white shadow-sm text-ink-900' : 'text-ink-500')}
              >
                <List className="size-3.5" /> Table
              </button>
            </div>
            {view === 'map' && (
              <label className="flex items-center gap-2 text-sm text-ink-600">
                <Satellite className="size-4" />
                Satellite
                <Switch checked={satellite} onCheckedChange={setSatellite} />
              </label>
            )}
          </div>
        }
      />
      <div className="border-b border-ink-200 bg-white px-6 py-3">
        <KpiBand>
          <KpiTile label="Active Trucks" value={String(drivers.length)} icon={Truck} hero />
          <KpiTile label="On the Road" value={String(driving)} tone="green" />
          <KpiTile label="HOS Critical (<2h)" value={String(hosCritical)} tone={hosCritical > 0 ? 'red' : 'neutral'} />
          <KpiTile
            label="Inspection Issues"
            value={String(inspectionIssues)}
            tone={inspectionIssues > 0 ? 'amber' : 'neutral'}
            icon={inspectionIssues > 0 ? AlertTriangle : undefined}
          />
        </KpiBand>
      </div>

      <div className="flex flex-1 overflow-hidden">
        <div className="relative flex-1">
          {focusMode && selected && (
            <div className="absolute top-3 left-1/2 z-[400] flex -translate-x-1/2 items-center gap-2 rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-xs shadow-md">
              <span className="font-medium text-ink-800">Focused on Driver {selected.driver_id}'s trip</span>
              <button onClick={() => setFocusMode(false)} className="flex items-center gap-1 text-brand-600 hover:underline">
                <X className="size-3" /> Show all trucks
              </button>
            </div>
          )}
          {view === 'map' ? (
            <>
              <FleetMap
                drivers={visibleDrivers}
                selectedDriverId={selectedId}
                onSelectDriver={selectDriver}
                satellite={satellite}
                routes={routes}
                pins={pins}
                geofences={geofenceCircles}
              />
              <MapLegend />
            </>
          ) : (
            <FleetTable drivers={visibleDrivers} onSelectDriver={selectDriver} />
          )}
        </div>

        <aside className="flex w-96 shrink-0 flex-col overflow-hidden border-l border-ink-200 bg-white">
          <Tabs value={tab} onValueChange={setTab} className="flex flex-1 flex-col overflow-hidden p-3">
            <TabsList className="w-full">
              <TabsTrigger value="feed" className="flex-1">Feed</TabsTrigger>
              <TabsTrigger value="selected" className="flex-1">Selected</TabsTrigger>
              <TabsTrigger value="history" className="flex-1">History</TabsTrigger>
            </TabsList>

            <TabsContent value="feed" className="flex-1 overflow-auto">
              <QuoteStatusLegend />
              <DispatchFeed onSelectDriver={(id) => selectDriver(id, { focus: true })} />
            </TabsContent>

            <TabsContent value="selected" className="flex-1 overflow-auto p-1">
              {!selected ? (
                <p className="p-3 text-sm text-ink-400">Search, click a truck, or select an assigned quote from the Feed.</p>
              ) : (
                <div className="flex flex-col gap-3">
                  <div>
                    <div className="font-display text-base font-bold text-ink-900">Driver {selected.driver_id}</div>
                    <div className="text-sm text-ink-500">Truck {selected.truck_number}</div>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    <Badge tone={selected.duty_status === 'driving' ? 'green' : 'gray'}>{selected.duty_status}</Badge>
                    <Badge tone={selected.hos_remaining_hours < 2 ? 'red' : selected.hos_remaining_hours < 4 ? 'amber' : 'neutral'}>
                      {selected.hos_remaining_hours.toFixed(1)}h HOS left
                    </Badge>
                    <Badge tone={selected.inspection_ok ? 'green' : 'red'}>
                      {selected.inspection_ok ? 'Inspection OK' : 'No passing inspection'}
                    </Badge>
                  </div>
                  {selected.current_trip_id ? (
                    <div className="rounded-lg border border-ink-200 p-3 text-sm">
                      <div className="mb-1.5 flex items-center justify-between">
                        <span className="font-medium text-ink-800">Current trip</span>
                        {scheduleState && (
                          <Badge tone={scheduleState === 'on_time' ? 'green' : scheduleState === 'at_risk' ? 'amber' : 'red'}>
                            {SCHEDULE_LABEL[scheduleState]}
                          </Badge>
                        )}
                      </div>
                      <div className="text-xs text-ink-500">Status: {selected.trip_status}</div>
                      {selected.eta && <div className="text-xs text-ink-500">ETA: {format(new Date(selected.eta), 'MMM d, HH:mm')}</div>}
                      {selected.speed_mph != null && <div className="text-xs text-ink-500">Speed: {selected.speed_mph.toFixed(0)} mph</div>}
                      {selected.odometer_km != null && <div className="text-xs text-ink-500">Odometer: {Math.round(selected.odometer_km).toLocaleString()} km</div>}
                      {selected.fuel_pct != null && <div className="text-xs text-ink-500">Fuel: {selected.fuel_pct.toFixed(0)}%</div>}
                    </div>
                  ) : (
                    <p className="text-xs text-ink-400">No active trip.</p>
                  )}

                  {geofenceRows.length > 0 && (
                    <div className="rounded-lg border border-ink-200 p-3 text-sm">
                      <div className="mb-1.5 font-medium text-ink-800">Geofence activity</div>
                      {geofenceRows.map((r) => {
                        const dwellMin =
                          r.arrived_at && r.departed_at
                            ? Math.round((new Date(r.departed_at).getTime() - new Date(r.arrived_at).getTime()) / 60000)
                            : null
                        return (
                          <div key={r.location_id} className="mb-2 border-b border-ink-100 pb-2 text-xs last:mb-0 last:border-0 last:pb-0">
                            <div className="font-medium text-ink-700">{r.label}</div>
                            <div className="text-ink-500">
                              {r.arrived_at ? `Arrived ${format(new Date(r.arrived_at), 'HH:mm:ss')}` : 'Not yet arrived'}
                              {r.departed_at && ` · Departed ${format(new Date(r.departed_at), 'HH:mm:ss')}`}
                            </div>
                            {dwellMin != null && <div className="text-ink-400">Dwell: {dwellMin} min</div>}
                          </div>
                        )
                      })}
                    </div>
                  )}

                  {focusMode ? (
                    <Button size="sm" variant="outline" onClick={() => setFocusMode(false)}>Show all trucks</Button>
                  ) : (
                    selected.current_trip_id && (
                      <Button size="sm" variant="outline" onClick={() => setFocusMode(true)}>Focus on this trip</Button>
                    )
                  )}
                </div>
              )}
            </TabsContent>

            <TabsContent value="history" className="flex-1 overflow-auto p-1">
              <TripStatusHistory tripId={selected?.current_trip_id ?? null} />
            </TabsContent>
          </Tabs>
        </aside>
      </div>
    </div>
  )
}
