import { type ColumnDef, flexRender, getCoreRowModel, getSortedRowModel, type SortingState, useReactTable } from '@tanstack/react-table'
import { formatDistanceToNow } from 'date-fns'
import { ArrowUpDown } from 'lucide-react'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { FleetDriver } from '@/lib/api'

// Real user feedback: Live Ops needed a table view of every truck/driver alongside the map --
// speed, mileage, fuel -- not just markers. One shared grid component (design system convention:
// TanStack Table everywhere) so Orders/Trip History/Drivers reuse the same look later.
function dutyTone(status: string): 'green' | 'amber' | 'gray' | 'red' {
  if (status === 'driving') return 'green'
  if (status === 'on_duty_not_driving') return 'amber'
  if (status === 'out_of_service' || status === 'breakdown') return 'red'
  return 'gray'
}

const columns: ColumnDef<FleetDriver>[] = [
  { accessorKey: 'driver_id', header: 'Driver', cell: (c) => `#${c.getValue()}` },
  { accessorKey: 'truck_number', header: 'Truck' },
  {
    accessorKey: 'duty_status',
    header: 'Status',
    cell: (c) => <Badge tone={dutyTone(c.getValue() as string)}>{c.getValue() as string}</Badge>,
  },
  {
    accessorKey: 'hos_remaining_hours',
    header: 'HOS left',
    cell: (c) => {
      const v = c.getValue() as number
      return <span className={v < 2 ? 'font-medium text-status-red-500' : v < 4 ? 'text-status-amber-500' : ''}>{v.toFixed(1)}h</span>
    },
  },
  { accessorKey: 'speed_mph', header: 'Speed', cell: (c) => (c.getValue() ? `${(c.getValue() as number).toFixed(0)} mph` : '—') },
  { accessorKey: 'odometer_km', header: 'Odometer', cell: (c) => (c.getValue() != null ? `${Math.round(c.getValue() as number).toLocaleString()} km` : '—') },
  {
    accessorKey: 'fuel_pct',
    header: 'Fuel',
    cell: (c) => {
      const v = c.getValue() as number | null
      if (v == null) return '—'
      return <span className={v < 20 ? 'font-medium text-status-red-500' : ''}>{v.toFixed(0)}%</span>
    },
  },
  {
    accessorKey: 'inspection_ok',
    header: 'Inspection',
    cell: (c) => <Badge tone={c.getValue() ? 'green' : 'red'}>{c.getValue() ? 'OK' : 'Missing'}</Badge>,
  },
  { accessorKey: 'trip_status', header: 'Trip', cell: (c) => (c.getValue() as string) ?? '—' },
]

export function FleetTable({ drivers, onSelectDriver }: { drivers: FleetDriver[]; onSelectDriver: (id: number) => void }) {
  const [sorting, setSorting] = React.useState<SortingState>([])
  const table = useReactTable({
    data: drivers,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  return (
    <div className="flex h-full flex-col gap-2 overflow-auto p-4">
      <p className="text-xs text-ink-400">Updated live from the telemetry feed · speed/odometer/fuel are simulated (no real ELD hardware exists in this project's data)</p>
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
            <TableRow key={row.id} className="cursor-pointer" onClick={() => onSelectDriver(row.original.driver_id)}>
              {row.getVisibleCells().map((cell) => (
                <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

export function formatUpdatedAgo(iso: string | null) {
  if (!iso) return '—'
  return formatDistanceToNow(new Date(iso), { addSuffix: true })
}
