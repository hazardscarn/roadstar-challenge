import { type ColumnDef, flexRender, getCoreRowModel, getSortedRowModel, type SortingState, useReactTable } from '@tanstack/react-table'
import { format, formatDistanceToNow } from 'date-fns'
import { ArrowUpDown, ChevronDown, ChevronRight } from 'lucide-react'
import * as React from 'react'
import { PageHeader } from '@/components/page-header'
import { TripStatusHistory } from '@/components/trip-status-history'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { supabase } from '@/lib/supabase'

// Real user feedback: "the trip log has to be a table that updates [on an interval] and has all
// this info" -- backed directly by live.trip_log (RLS-protected as of sim/sql/036), the real
// audit trail sim/live/telemetry_simulator.py writes to on every trip completion. Polling, not a
// one-time fetch, matches the "keeps itself current" ask without needing a Realtime subscription
// for a table that only grows a few rows an hour.
const REFRESH_MS = 5 * 60 * 1000 // 5 minutes, per the user's explicit cadence

interface TripLogRow {
  trip_id: string
  driver_id: number
  truck_number: string
  completed_at: string
  loaded_miles: number | null
  pre_pickup_deadhead_miles: number | null
  post_delivery_deadhead_miles: number | null
  pickup_dwell_hours: number | null
  delivery_dwell_hours: number | null
  on_time: boolean | null
  load_fill_ratio: number | null
  breakdown_occurred: boolean
  reward_total: number | null
}

function buildColumns(expandedTripId: string | null): ColumnDef<TripLogRow>[] {
  return [
  {
    id: 'expand',
    header: '',
    enableSorting: false,
    cell: ({ row }) => (row.original.trip_id === expandedTripId ? <ChevronDown className="size-4 text-ink-400" /> : <ChevronRight className="size-4 text-ink-400" />),
  },
  { accessorKey: 'completed_at', header: 'Completed', cell: (c) => format(new Date(c.getValue() as string), 'MMM d, HH:mm') },
  { accessorKey: 'driver_id', header: 'Driver', cell: (c) => `#${c.getValue()}` },
  { accessorKey: 'truck_number', header: 'Truck' },
  { accessorKey: 'loaded_miles', header: 'Loaded mi', cell: (c) => (c.getValue() != null ? (c.getValue() as number).toFixed(1) : '—') },
  { accessorKey: 'pre_pickup_deadhead_miles', header: 'Pre-pickup DH', cell: (c) => (c.getValue() != null ? (c.getValue() as number).toFixed(1) : '—') },
  { accessorKey: 'post_delivery_deadhead_miles', header: 'Post-delivery DH', cell: (c) => (c.getValue() != null ? (c.getValue() as number).toFixed(1) : '—') },
  {
    accessorKey: 'pickup_dwell_hours',
    header: 'Pickup dwell',
    cell: (c) => (c.getValue() != null ? `${((c.getValue() as number) * 60).toFixed(0)} min` : '—'),
  },
  {
    accessorKey: 'delivery_dwell_hours',
    header: 'Delivery dwell',
    cell: (c) => (c.getValue() != null ? `${((c.getValue() as number) * 60).toFixed(0)} min` : '—'),
  },
  {
    accessorKey: 'load_fill_ratio',
    header: 'Load fill',
    cell: (c) => (c.getValue() != null ? `${((c.getValue() as number) * 100).toFixed(0)}%` : '—'),
  },
  {
    accessorKey: 'on_time',
    header: 'On-time',
    cell: (c) => {
      const v = c.getValue() as boolean | null
      if (v == null) return <Badge tone="gray">unknown</Badge>
      return <Badge tone={v ? 'green' : 'red'}>{v ? 'on-time' : 'late'}</Badge>
    },
  },
  {
    accessorKey: 'breakdown_occurred',
    header: 'Breakdown',
    cell: (c) => (c.getValue() ? <Badge tone="red">yes</Badge> : <span className="text-ink-400">no</span>),
  },
  ]
}

export default function TripHistory() {
  const [rows, setRows] = React.useState<TripLogRow[]>([])
  const [lastRefresh, setLastRefresh] = React.useState<Date | null>(null)
  const [sorting, setSorting] = React.useState<SortingState>([{ id: 'completed_at', desc: true }])
  const [expandedTripId, setExpandedTripId] = React.useState<string | null>(null)

  const refresh = React.useCallback(async () => {
    const { data, error } = await supabase
      .schema('live')
      .from('trip_log')
      .select(
        'trip_id,driver_id,truck_number,completed_at,loaded_miles,pre_pickup_deadhead_miles,post_delivery_deadhead_miles,' +
          'pickup_dwell_hours,delivery_dwell_hours,on_time,load_fill_ratio,breakdown_occurred,reward_total',
      )
      .order('completed_at', { ascending: false })
      .limit(200)
    if (error) {
      console.error('trip history refresh failed', error)
      return
    }
    setRows(data as unknown as TripLogRow[])
    setLastRefresh(new Date())
  }, [])

  React.useEffect(() => {
    refresh()
    const id = setInterval(refresh, REFRESH_MS)
    return () => clearInterval(id)
  }, [refresh])

  const columns = React.useMemo(() => buildColumns(expandedTripId), [expandedTripId])

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
      <PageHeader
        title="Trip History"
        description={`Every completed trip, from live.trip_log. Click a row for its full status history.${lastRefresh ? ` Updated ${formatDistanceToNow(lastRefresh, { addSuffix: true })} (refreshes every 5 min).` : ''}`}
      />
      <div className="flex-1 overflow-auto p-6">
        {rows.length === 0 ? (
          <p className="text-sm text-ink-400">No completed trips yet.</p>
        ) : (
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
              {table.getRowModel().rows.map((row) => {
                const isExpanded = row.original.trip_id === expandedTripId
                return (
                  <React.Fragment key={row.id}>
                    <TableRow
                      className={`cursor-pointer ${isExpanded ? 'bg-brand-50' : ''}`}
                      onClick={() => setExpandedTripId(isExpanded ? null : row.original.trip_id)}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                      ))}
                    </TableRow>
                    {isExpanded && (
                      <TableRow>
                        <TableCell colSpan={row.getVisibleCells().length} className="bg-ink-50 p-0">
                          <div className="max-h-80 overflow-auto p-3">
                            <p className="mb-2 text-xs font-medium text-ink-500">
                              Full status history — every ~15-minute HOS/truck-health snapshot plus geofence arrival/departure milestones.
                            </p>
                            <TripStatusHistory tripId={row.original.trip_id} />
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </React.Fragment>
                )
              })}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  )
}
