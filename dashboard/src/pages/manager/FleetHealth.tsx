import { type ColumnDef, flexRender, getCoreRowModel, getSortedRowModel, type SortingState, useReactTable } from '@tanstack/react-table'
import { format } from 'date-fns'
import { ArrowUpDown } from 'lucide-react'
import * as React from 'react'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { supabase } from '@/lib/supabase'

// Real user pivot: "what happened at the end of the simulation for the day" -- backed by
// simulation.truck_maintenance_state (sim/sql/038), populated per truck by the most recent
// AI-dispatch replay (sim/live/ai_dispatch_replay.py), not live.* telemetry. The 85%-of-interval /
// 10-days-to-due warning rule is exactly what research/roadstar_platform_plan.md Section 5
// specifies, not invented here.
const POLL_MS = 30000
const SERVICE_INTERVAL_KM_WARN = 0.85

interface MaintenanceRow {
  truck_number: string
  cumulative_km_since_service: number
  last_service_at: string | null
  service_interval_km: number
  service_interval_days: number
  maintenance_until: string | null
}

type Severity = 'ok' | 'due_soon' | 'in_shop'

function severity(r: MaintenanceRow): Severity {
  if (r.maintenance_until && new Date(r.maintenance_until) > new Date()) return 'in_shop'
  const pctKm = r.cumulative_km_since_service / r.service_interval_km
  const daysSince = r.last_service_at ? (Date.now() - new Date(r.last_service_at).getTime()) / 86400000 : 0
  const daysToDue = r.service_interval_days - daysSince
  if (pctKm >= SERVICE_INTERVAL_KM_WARN || daysToDue <= 10) return 'due_soon'
  return 'ok'
}

const SEVERITY_TONE: Record<Severity, 'green' | 'amber' | 'red'> = { ok: 'green', due_soon: 'amber', in_shop: 'red' }
const SEVERITY_LABEL: Record<Severity, string> = { ok: 'Healthy', due_soon: 'Service due soon', in_shop: 'In the shop' }

export default function FleetHealth() {
  const [rows, setRows] = React.useState<MaintenanceRow[]>([])
  const [sorting, setSorting] = React.useState<SortingState>([])

  const refresh = React.useCallback(async () => {
    // Real user pivot: no more live.* -- reads whichever simulation run is most recent (a fresh
    // AI-dispatch replay), not a single always-on live table.
    // Real bug found directly: without filtering by run_kind, this picked up whatever run was
    // most recently created REGARDLESS of type -- including a 'trip_demo' run (the separate
    // single-trip Simulation Trip demo page), which never populates truck_maintenance_state at
    // all. Scoped to the real fleet-wide AI-dispatch-day replay specifically.
    const { data: latestRun } = await supabase.schema('simulation').from('runs').select('run_id').eq('run_kind', 'ai_dispatch_day').order('created_at', { ascending: false }).limit(1).single()
    if (!latestRun) {
      setRows([])
      return
    }
    // simulation.truck_maintenance_state has no maintenance_until column (unlike the old live.*
    // table) -- no real "in the shop" concept exists in a one-day replay, only the km/days-since-
    // service figures below.
    const { data, error } = await supabase
      .schema('simulation')
      .from('truck_maintenance_state')
      .select('truck_number,cumulative_km_since_service,last_service_at,service_interval_km,service_interval_days')
      .eq('run_id', latestRun.run_id)
    if (error) {
      console.error('fleet health refresh failed', error)
      return
    }
    setRows((data as unknown as MaintenanceRow[]).sort((a, b) => severity(b).localeCompare(severity(a))))
  }, [])

  React.useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh])

  const columns: ColumnDef<MaintenanceRow>[] = [
    { accessorKey: 'truck_number', header: 'Truck' },
    {
      id: 'severity',
      header: 'Status',
      accessorFn: (r) => severity(r),
      cell: (c) => <Badge tone={SEVERITY_TONE[c.getValue() as Severity]}>{SEVERITY_LABEL[c.getValue() as Severity]}</Badge>,
    },
    {
      id: 'pct_km',
      header: '% of service interval (km)',
      accessorFn: (r) => r.cumulative_km_since_service / r.service_interval_km,
      cell: (c) => `${((c.getValue() as number) * 100).toFixed(0)}%`,
    },
    {
      accessorKey: 'cumulative_km_since_service',
      header: 'Km since service',
      cell: (c) => Math.round(c.getValue() as number).toLocaleString(),
    },
    {
      accessorKey: 'last_service_at',
      header: 'Last serviced',
      cell: (c) => (c.getValue() ? format(new Date(c.getValue() as string), 'MMM d, yyyy') : '—'),
    },
    {
      accessorKey: 'maintenance_until',
      header: 'In shop until',
      cell: (c) => (c.getValue() && new Date(c.getValue() as string) > new Date() ? format(new Date(c.getValue() as string), 'MMM d, HH:mm') : '—'),
    },
  ]

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  const dueSoon = rows.filter((r) => severity(r) === 'due_soon').length
  const inShop = rows.filter((r) => severity(r) === 'in_shop').length

  return (
    <div className="flex h-screen flex-col">
      <PageHeader
        title="Fleet Health"
        description="Maintenance warnings — synthesized from odometer/service tracking (no real service-history source exists anywhere in the source data, flagged plainly, not presented as backtested)."
      />
      <div className="flex-1 overflow-auto p-6">
        <div className="mb-4 flex gap-3">
          <div className="rounded-lg border border-ink-200 bg-white px-4 py-2 text-sm">
            <span className="font-semibold text-status-amber-500">{dueSoon}</span> due soon
          </div>
          <div className="rounded-lg border border-ink-200 bg-white px-4 py-2 text-sm">
            <span className="font-semibold text-status-red-500">{inShop}</span> in the shop
          </div>
        </div>
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((hg) => (
              <TableRow key={hg.id}>
                {hg.headers.map((h) => (
                  <TableHead key={h.id}>
                    <button className="flex items-center gap-1" onClick={h.column.getToggleSortingHandler()}>
                      {flexRender(h.column.columnDef.header, h.getContext())}
                      {h.column.getCanSort() && <ArrowUpDown className="size-3 text-ink-300" />}
                    </button>
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.map((row) => (
              <TableRow key={row.id}>
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
