import { type ColumnDef, flexRender, getCoreRowModel, getSortedRowModel, type SortingState, useReactTable } from '@tanstack/react-table'
import { format } from 'date-fns'
import { ArrowUpDown } from 'lucide-react'
import * as React from 'react'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { supabase } from '@/lib/supabase'

// Real user pivot: "what happened at the end of the simulation for the day" -- driver status is
// each driver's LAST simulation.driver_state_snapshots row for the most recent AI-dispatch replay
// (sim/live/ai_dispatch_replay.py), joined with simulation.driver_ratings (a VIEW computed from
// trip_log) and that run's own vehicle_inspections row per driver. No more live.* telemetry.
const POLL_MS = 30000

interface DriverRow {
  driver_id: number
  truck_number: string
  duty_status: string
  hos_remaining_hours: number
  trips_completed: number | null
  on_time_rate: number | null
  avg_post_delivery_deadhead_miles: number | null
  last_inspection_at: string | null
  last_inspection_pass: boolean | null
}

export default function Drivers() {
  const [rows, setRows] = React.useState<DriverRow[]>([])
  const [sorting, setSorting] = React.useState<SortingState>([])

  const refresh = React.useCallback(async () => {
    const { data: latestRun } = await supabase.schema('simulation').from('runs').select('run_id').order('created_at', { ascending: false }).limit(1).single()
    if (!latestRun) {
      setRows([])
      return
    }
    const runId = latestRun.run_id as string

    // Real user pivot: driver_state_snapshots is an event log (several rows per driver across the
    // simulated day), not a single always-current row like the old live.driver_status -- take
    // each driver's LAST snapshot for this run as their end-of-day status.
    const { data: snapshots, error } = await supabase
      .schema('simulation')
      .from('driver_state_snapshots')
      .select('driver_id,truck_number,duty_status,hos_driving_hours_remaining')
      .eq('run_id', runId)
      .order('snapshot_at', { ascending: false })
    if (error || !snapshots) {
      console.error('drivers refresh failed', error)
      return
    }
    const latestByDriver = new Map<number, { driver_id: number; truck_number: string; duty_status: string; hos_driving_hours_remaining: number }>()
    for (const s of snapshots) {
      if (!latestByDriver.has(s.driver_id)) latestByDriver.set(s.driver_id, s as never)
    }
    const driverIds = Array.from(latestByDriver.keys())

    const { data: ratings } = await supabase
      .schema('simulation')
      .from('driver_ratings')
      .select('driver_id,trips_completed,on_time_rate,avg_post_delivery_deadhead_miles')
      .eq('run_id', runId)
      .in('driver_id', driverIds)
    const ratingByDriver = new Map((ratings ?? []).map((r) => [r.driver_id, r]))

    const { data: inspections } = await supabase
      .schema('simulation')
      .from('vehicle_inspections')
      .select('driver_id,submitted_at,overall_pass')
      .eq('run_id', runId)
      .in('driver_id', driverIds)
      .order('submitted_at', { ascending: false })
    const latestInspectionByDriver = new Map<number, { submitted_at: string; overall_pass: boolean }>()
    for (const insp of inspections ?? []) {
      if (!latestInspectionByDriver.has(insp.driver_id)) latestInspectionByDriver.set(insp.driver_id, insp as never)
    }

    setRows(
      driverIds.map((driverId) => {
        const status = latestByDriver.get(driverId)!
        const rating = ratingByDriver.get(driverId)
        const insp = latestInspectionByDriver.get(driverId)
        return {
          driver_id: driverId,
          truck_number: status.truck_number,
          duty_status: status.duty_status,
          hos_remaining_hours: status.hos_driving_hours_remaining,
          trips_completed: rating?.trips_completed ?? 0,
          on_time_rate: rating?.on_time_rate ?? null,
          avg_post_delivery_deadhead_miles: rating?.avg_post_delivery_deadhead_miles ?? null,
          last_inspection_at: insp?.submitted_at ?? null,
          last_inspection_pass: insp?.overall_pass ?? null,
        }
      }),
    )
  }, [])

  React.useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh])

  const columns: ColumnDef<DriverRow>[] = [
    { accessorKey: 'driver_id', header: 'Driver', cell: (c) => `#${c.getValue()}` },
    { accessorKey: 'truck_number', header: 'Truck' },
    { accessorKey: 'duty_status', header: 'Status', cell: (c) => <Badge tone={c.getValue() === 'driving' ? 'green' : 'gray'}>{c.getValue() as string}</Badge> },
    {
      accessorKey: 'hos_remaining_hours',
      header: 'HOS left',
      cell: (c) => {
        const v = c.getValue() as number
        return <span className={v < 2 ? 'font-medium text-status-red-500' : ''}>{v.toFixed(1)}h</span>
      },
    },
    { accessorKey: 'trips_completed', header: 'Trips completed' },
    {
      accessorKey: 'on_time_rate',
      header: 'On-time rate',
      cell: (c) => (c.getValue() != null ? `${((c.getValue() as number) * 100).toFixed(0)}%` : '—'),
    },
    {
      accessorKey: 'avg_post_delivery_deadhead_miles',
      header: 'Avg post-delivery deadhead',
      cell: (c) => (c.getValue() != null ? `${(c.getValue() as number).toFixed(1)} mi` : '—'),
    },
    {
      id: 'inspection',
      header: 'Last inspection',
      cell: ({ row }) => {
        const r = row.original
        if (!r.last_inspection_at) return <Badge tone="amber">none on file</Badge>
        const withinDay = Date.now() - new Date(r.last_inspection_at).getTime() < 24 * 3600 * 1000
        return (
          <div className="flex flex-col gap-0.5">
            <Badge tone={r.last_inspection_pass ? (withinDay ? 'green' : 'amber') : 'red'}>
              {r.last_inspection_pass ? (withinDay ? 'pass · current' : 'pass · stale') : 'failed'}
            </Badge>
            <span className="text-[11px] text-ink-400">{format(new Date(r.last_inspection_at), 'MMM d, HH:mm')}</span>
          </div>
        )
      },
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

  return (
    <div className="flex h-screen flex-col">
      <PageHeader title="Drivers" description="Fleet-wide roster — HOS, ratings, inspection status." />
      <div className="flex-1 overflow-auto p-6">
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
