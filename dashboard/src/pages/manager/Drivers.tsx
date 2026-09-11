import { type ColumnDef, flexRender, getCoreRowModel, getSortedRowModel, type SortingState, useReactTable } from '@tanstack/react-table'
import { format } from 'date-fns'
import { ArrowUpDown } from 'lucide-react'
import * as React from 'react'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { supabase } from '@/lib/supabase'

// The manager-facing rollup of everything the driver role records individually (research/
// roadstar_platform_plan.md Section 8.1): live status joined with live.driver_ratings (a VIEW
// computed on read from trip_log, security_invoker as of sim/sql/036) and the most recent
// vehicle_inspections row per driver.
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
    const { data: statuses, error } = await supabase
      .schema('live')
      .from('driver_status')
      .select('driver_id,truck_number,duty_status,hos_remaining_hours')
      .order('driver_id')
    if (error || !statuses) {
      console.error('drivers refresh failed', error)
      return
    }

    const driverIds = statuses.map((s) => s.driver_id)
    const { data: ratings } = await supabase
      .schema('live')
      .from('driver_ratings')
      .select('driver_id,trips_completed,on_time_rate,avg_post_delivery_deadhead_miles')
      .in('driver_id', driverIds)
    const ratingByDriver = new Map((ratings ?? []).map((r) => [r.driver_id, r]))

    const { data: inspections } = await supabase
      .schema('live')
      .from('vehicle_inspections')
      .select('driver_id,submitted_at,overall_pass')
      .in('driver_id', driverIds)
      .order('submitted_at', { ascending: false })
    const latestInspectionByDriver = new Map<number, { submitted_at: string; overall_pass: boolean }>()
    for (const insp of inspections ?? []) {
      if (!latestInspectionByDriver.has(insp.driver_id)) latestInspectionByDriver.set(insp.driver_id, insp as never)
    }

    setRows(
      statuses.map((s) => {
        const rating = ratingByDriver.get(s.driver_id)
        const insp = latestInspectionByDriver.get(s.driver_id)
        return {
          driver_id: s.driver_id,
          truck_number: s.truck_number,
          duty_status: s.duty_status,
          hos_remaining_hours: s.hos_remaining_hours,
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
