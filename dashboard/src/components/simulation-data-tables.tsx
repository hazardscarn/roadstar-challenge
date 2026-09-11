import { format } from 'date-fns'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { supabase } from '@/lib/supabase'

// The "show them this table logging happening" panel -- real Supabase reads against the
// `simulation` schema (sim/sql/038) for the run that just ran, not client-side JSON. Proves the
// week's data actually landed in real, queryable tables.

const TABS = [
  { key: 'trip_log', label: 'Trip Log' },
  { key: 'geofence_events', label: 'Geofence Events' },
  { key: 'invoices', label: 'Invoices' },
  { key: 'driver_state_snapshots', label: 'Driver Snapshots' },
] as const

export function SimulationDataTables({ runId }: { runId: string }) {
  const [tab, setTab] = React.useState<(typeof TABS)[number]['key']>('trip_log')
  const [rows, setRows] = React.useState<Record<string, unknown>[]>([])
  const [loading, setLoading] = React.useState(false)

  React.useEffect(() => {
    setLoading(true)
    ;(async () => {
      const orderCol = tab === 'trip_log' ? 'completed_at' : tab === 'geofence_events' ? 'occurred_at' : tab === 'invoices' ? 'issued_at' : 'snapshot_at'
      const { data } = await supabase.schema('simulation').from(tab).select('*').eq('run_id', runId).order(orderCol, { ascending: false }).limit(100)
      setRows((data as Record<string, unknown>[]) ?? [])
      setLoading(false)
    })()
  }, [tab, runId])

  const columns = rows[0] ? Object.keys(rows[0]).filter((c) => c !== 'run_id') : []

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-1 border-b border-ink-200 px-3 py-2">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`rounded-md px-2.5 py-1 text-xs font-medium ${tab === t.key ? 'bg-brand-100 text-brand-700' : 'text-ink-500 hover:bg-ink-50'}`}
          >
            {t.label}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-ink-400">simulation.{tab} · run {runId.slice(0, 8)}</span>
      </div>
      <div className="flex-1 overflow-auto p-2">
        {loading ? (
          <p className="p-3 text-xs text-ink-400">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="p-3 text-xs text-ink-400">No rows yet.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>{columns.map((c) => <TableHead key={c} className="whitespace-nowrap text-[11px]">{c}</TableHead>)}</TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r, i) => (
                // eslint-disable-next-line react/no-array-index-key
                <TableRow key={i}>
                  {columns.map((c) => (
                    <TableCell key={c} className="whitespace-nowrap text-[11px] text-ink-600">{formatCell(r[c])}</TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  )
}

function formatCell(v: unknown): string {
  if (v == null) return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(v)) {
    try { return format(new Date(v), 'MMM d, HH:mm:ss') } catch { return v }
  }
  if (typeof v === 'number') return v.toFixed(2)
  return String(v)
}

interface DriverRating {
  driver_id: number
  trips_completed: number
  on_time_rate: number
  avg_post_delivery_deadhead_miles: number
  avg_load_fill_ratio: number
  total_reward: number
}

export function SimulationDriverSpotlight({ runId, driverIds }: { runId: string; driverIds: number[] }) {
  const [driverId, setDriverId] = React.useState<number | null>(driverIds[0] ?? null)
  const [rating, setRating] = React.useState<DriverRating | null>(null)
  const [inspections, setInspections] = React.useState<number>(0)

  React.useEffect(() => {
    if (driverId == null) return
    ;(async () => {
      const { data } = await supabase.schema('simulation').from('driver_ratings').select('*').eq('run_id', runId).eq('driver_id', driverId).maybeSingle()
      setRating(data as unknown as DriverRating | null)
      const { count } = await supabase.schema('simulation').from('vehicle_inspections').select('*', { count: 'exact', head: true }).eq('run_id', runId).eq('driver_id', driverId).eq('overall_pass', true)
      setInspections(count ?? 0)
    })()
  }, [runId, driverId])

  if (driverIds.length === 0) return null

  return (
    <div className="rounded-lg border border-ink-200 bg-white p-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="font-display text-sm font-semibold text-ink-900">Driver spotlight</h3>
        <Select value={String(driverId)} onValueChange={(v) => setDriverId(Number(v))}>
          <SelectTrigger className="h-7 w-28 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            {driverIds.map((id) => <SelectItem key={id} value={String(id)}>Driver {id}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      {rating ? (
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div><div className="text-ink-400">Trips completed</div><div className="font-semibold text-ink-800">{rating.trips_completed}</div></div>
          <div><div className="text-ink-400">On-time rate</div><div className="font-semibold text-status-green-500">{(rating.on_time_rate * 100).toFixed(0)}%</div></div>
          <div><div className="text-ink-400">Avg load fill</div><div className="font-semibold text-ink-800">{(rating.avg_load_fill_ratio * 100).toFixed(0)}%</div></div>
          <div><div className="text-ink-400">Net reward earned</div><div className="font-semibold text-ink-800">${rating.total_reward.toFixed(0)}</div></div>
          <div className="col-span-2 flex items-center gap-1.5">
            <Badge tone="green">{inspections}/7 passing pre-trip checks</Badge>
          </div>
        </div>
      ) : (
        <p className="text-xs text-ink-400">No completed trips yet for this driver.</p>
      )}
    </div>
  )
}
