import { format } from 'date-fns'
import { AlertTriangle, ArrowUpDown, ShieldCheck, Wrench } from 'lucide-react'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { supabase } from '@/lib/supabase'

// Real user feedback: "another view alternate in sim page ... kpi dashboards ... driver dashboard
// and truck dashboard ... driver dashboard will have info and driver score and all created from
// trips completed info recently (we save this in tables right?) ... for trucks and reports of
// maintenance alerts etc." Everything here reads straight from the SAME real per-run rollup views
// the single-driver "Driver spotlight" card already used (simulation.driver_ratings/truck_ratings,
// sim/sql/038 -- themselves a run_id-scoped mirror of live.driver_ratings/truck_ratings, sim/sql/
// 011) plus simulation.truck_maintenance_state for the maintenance-alert column -- nothing
// recomputed client-side except the alert thresholds themselves.

interface DriverRating {
  driver_id: number
  trips_completed: number
  on_time_rate: number
  avg_post_delivery_deadhead_miles: number
  avg_hos_stranding_risk: number
  avg_load_fill_ratio: number
  total_reward: number
}

interface TruckRating {
  truck_number: string
  trips_completed: number
  breakdown_count: number
  avg_repair_hours_when_broken: number | null
  avg_load_fill_ratio: number
}

interface MaintenanceRow {
  truck_number: string
  cumulative_km_since_service: number
  last_service_at: string | null
  service_interval_km: number
  service_interval_days: number
  // Note: unlike live.truck_maintenance_state, simulation.truck_maintenance_state has no
  // maintenance_until column (sim/sql/029 only added it to the live table) -- a showcase run
  // ends with every truck's final post-week state, not a live "currently in the shop" flag.
}

type SortDir = 'asc' | 'desc'

function useSort<T>(rows: T[], defaultKey: keyof T, defaultDir: SortDir = 'desc') {
  const [key, setKey] = React.useState<keyof T>(defaultKey)
  const [dir, setDir] = React.useState<SortDir>(defaultDir)
  const sorted = React.useMemo(() => {
    const copy = [...rows]
    copy.sort((a, b) => {
      const av = a[key], bv = b[key]
      const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv))
      return dir === 'asc' ? cmp : -cmp
    })
    return copy
  }, [rows, key, dir])
  function toggle(k: keyof T) {
    if (k === key) setDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setKey(k); setDir('desc') }
  }
  return { sorted, sortKey: key, sortDir: dir, toggle }
}

function SortHead<T>({ label, col, sortKey, toggle }: { label: string; col: keyof T; sortKey: keyof T; toggle: (k: keyof T) => void }) {
  return (
    <TableHead>
      <button className={`flex items-center gap-1 ${sortKey === col ? 'text-ink-900' : 'text-ink-500'}`} onClick={() => toggle(col)}>
        {label} <ArrowUpDown className="size-3 text-ink-300" />
      </button>
    </TableHead>
  )
}

// Maintenance status is judged on whichever of the two real intervals (km-based, days-based) is
// further along -- a truck can be overdue on either axis independently.
function maintenanceStatus(m: MaintenanceRow): { tone: 'green' | 'amber' | 'red'; label: string; pct: number } {
  const pctKm = m.service_interval_km > 0 ? m.cumulative_km_since_service / m.service_interval_km : 0
  const pctDays = m.last_service_at && m.service_interval_days > 0
    ? (Date.now() - new Date(m.last_service_at).getTime()) / (m.service_interval_days * 86400000)
    : 0
  const pct = Math.max(pctKm, pctDays)
  if (pct >= 1) return { tone: 'red', label: 'Overdue', pct }
  if (pct >= 0.8) return { tone: 'amber', label: 'Due soon', pct }
  return { tone: 'green', label: 'OK', pct }
}

export function SimulationFleetDashboard({ runId }: { runId: string }) {
  const [drivers, setDrivers] = React.useState<DriverRating[] | null>(null)
  const [trucks, setTrucks] = React.useState<TruckRating[] | null>(null)
  const [maint, setMaint] = React.useState<Record<string, MaintenanceRow>>({})

  React.useEffect(() => {
    setDrivers(null)
    setTrucks(null)
    ;(async () => {
      const [{ data: dr }, { data: tr }, { data: mt }] = await Promise.all([
        supabase.schema('simulation').from('driver_ratings').select('*').eq('run_id', runId),
        supabase.schema('simulation').from('truck_ratings').select('*').eq('run_id', runId),
        supabase.schema('simulation').from('truck_maintenance_state').select('*').eq('run_id', runId),
      ])
      setDrivers((dr as unknown as DriverRating[]) ?? [])
      setTrucks((tr as unknown as TruckRating[]) ?? [])
      setMaint(Object.fromEntries(((mt as unknown as MaintenanceRow[]) ?? []).map((m) => [m.truck_number, m])))
    })()
  }, [runId])

  const driverSort = useSort(drivers ?? [], 'trips_completed')
  const truckSort = useSort(trucks ?? [], 'trips_completed')

  const alertTrucks = trucks
    ?.map((t) => ({ t, m: maint[t.truck_number], status: maint[t.truck_number] ? maintenanceStatus(maint[t.truck_number]) : null }))
    .filter((r) => r.status && r.status.tone !== 'green')
    .sort((a, b) => (b.status!.pct - a.status!.pct)) ?? []

  return (
    <div className="flex flex-1 flex-col gap-4 overflow-auto p-6">
      {alertTrucks.length > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-status-amber-200 bg-status-amber-50 p-3 text-sm text-status-amber-700">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <div>
            <strong>{alertTrucks.length} truck{alertTrucks.length === 1 ? '' : 's'}</strong> need service attention this run:{' '}
            {alertTrucks.map((r) => `${r.t.truck_number} (${r.status!.label})`).join(', ')}.
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <section className="rounded-lg border border-ink-200 bg-white">
          <div className="flex items-center gap-2 border-b border-ink-200 px-4 py-3">
            <ShieldCheck className="size-4 text-brand-600" />
            <h2 className="font-display text-sm font-semibold text-ink-900">Driver dashboard</h2>
            <span className="ml-auto text-[11px] text-ink-400">simulation.driver_ratings · run {runId.slice(0, 8)}</span>
          </div>
          {drivers == null ? (
            <p className="p-4 text-xs text-ink-400">Loading…</p>
          ) : drivers.length === 0 ? (
            <p className="p-4 text-xs text-ink-400">No completed trips yet for this run.</p>
          ) : (
            <div className="overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortHead label="Driver" col="driver_id" sortKey={driverSort.sortKey} toggle={driverSort.toggle} />
                    <SortHead label="Trips" col="trips_completed" sortKey={driverSort.sortKey} toggle={driverSort.toggle} />
                    <SortHead label="On-time" col="on_time_rate" sortKey={driverSort.sortKey} toggle={driverSort.toggle} />
                    <SortHead label="Avg deadhead (mi)" col="avg_post_delivery_deadhead_miles" sortKey={driverSort.sortKey} toggle={driverSort.toggle} />
                    <SortHead label="Avg HOS risk" col="avg_hos_stranding_risk" sortKey={driverSort.sortKey} toggle={driverSort.toggle} />
                    <SortHead label="Load fill" col="avg_load_fill_ratio" sortKey={driverSort.sortKey} toggle={driverSort.toggle} />
                    <SortHead label="Net reward" col="total_reward" sortKey={driverSort.sortKey} toggle={driverSort.toggle} />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {driverSort.sorted.map((d) => (
                    <TableRow key={d.driver_id}>
                      <TableCell className="font-medium text-ink-800">#{d.driver_id}</TableCell>
                      <TableCell>{d.trips_completed}</TableCell>
                      <TableCell>
                        <Badge tone={d.on_time_rate >= 0.9 ? 'green' : d.on_time_rate >= 0.75 ? 'amber' : 'red'}>
                          {(d.on_time_rate * 100).toFixed(0)}%
                        </Badge>
                      </TableCell>
                      <TableCell>{d.avg_post_delivery_deadhead_miles.toFixed(1)}</TableCell>
                      <TableCell>{(d.avg_hos_stranding_risk * 100).toFixed(0)}%</TableCell>
                      <TableCell>{(d.avg_load_fill_ratio * 100).toFixed(0)}%</TableCell>
                      <TableCell className="font-medium text-status-green-500">${d.total_reward.toFixed(0)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </section>

        <section className="rounded-lg border border-ink-200 bg-white">
          <div className="flex items-center gap-2 border-b border-ink-200 px-4 py-3">
            <Wrench className="size-4 text-brand-600" />
            <h2 className="font-display text-sm font-semibold text-ink-900">Truck dashboard</h2>
            <span className="ml-auto text-[11px] text-ink-400">simulation.truck_ratings + truck_maintenance_state · run {runId.slice(0, 8)}</span>
          </div>
          {trucks == null ? (
            <p className="p-4 text-xs text-ink-400">Loading…</p>
          ) : trucks.length === 0 ? (
            <p className="p-4 text-xs text-ink-400">No completed trips yet for this run.</p>
          ) : (
            <div className="overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortHead label="Truck" col="truck_number" sortKey={truckSort.sortKey} toggle={truckSort.toggle} />
                    <SortHead label="Trips" col="trips_completed" sortKey={truckSort.sortKey} toggle={truckSort.toggle} />
                    <SortHead label="Breakdowns" col="breakdown_count" sortKey={truckSort.sortKey} toggle={truckSort.toggle} />
                    <SortHead label="Load fill" col="avg_load_fill_ratio" sortKey={truckSort.sortKey} toggle={truckSort.toggle} />
                    <TableHead>Maintenance</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {truckSort.sorted.map((t) => {
                    const m = maint[t.truck_number]
                    const status = m ? maintenanceStatus(m) : null
                    return (
                      <TableRow key={t.truck_number}>
                        <TableCell className="font-medium text-ink-800">{t.truck_number}</TableCell>
                        <TableCell>{t.trips_completed}</TableCell>
                        <TableCell>
                          {t.breakdown_count > 0 ? (
                            <Badge tone="red">{t.breakdown_count} ({(t.avg_repair_hours_when_broken ?? 0).toFixed(1)}h avg repair)</Badge>
                          ) : (
                            <span className="text-ink-400">0</span>
                          )}
                        </TableCell>
                        <TableCell>{(t.avg_load_fill_ratio * 100).toFixed(0)}%</TableCell>
                        <TableCell>
                          {status && m ? (
                            <div className="flex flex-col gap-0.5">
                              <Badge tone={status.tone}>{status.label}</Badge>
                              <span className="text-[11px] text-ink-400">
                                {m.cumulative_km_since_service.toFixed(0)}/{m.service_interval_km.toFixed(0)} km
                                {m.last_service_at && ` · serviced ${format(new Date(m.last_service_at), 'MMM d')}`}
                              </span>
                            </div>
                          ) : (
                            <span className="text-ink-300">—</span>
                          )}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
