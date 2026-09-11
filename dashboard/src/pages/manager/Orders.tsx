import { type ColumnDef, flexRender, getCoreRowModel, getSortedRowModel, type SortingState, useReactTable } from '@tanstack/react-table'
import { format } from 'date-fns'
import { ArrowUpDown, SquarePen } from 'lucide-react'
import * as React from 'react'
import { Link } from 'react-router-dom'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { supabase } from '@/lib/supabase'

// Orders = the full lifecycle view of a quote_requests row: upcoming (assigned, not yet
// completed), ongoing (assigned + trip actively in_transit/at_pickup/at_delivery), completed
// (trip_log exists). No new table needed -- this is a real synthesis of live.quote_requests +
// live.trips + live.trip_log, exactly the design call already made in the build plan ("no new
// order entity beyond the Phase-1 columns").
const POLL_MS = 15000

type OrderLifecycle = 'upcoming' | 'ongoing' | 'completed' | 'open'

interface OrderRow {
  quote_id: string
  requested_at: string
  requested_pickup_at: string | null
  origin_location_id: number | null
  dest_location_id: number | null
  weight_lbs: number | null
  pallets: number | null
  load_type: string | null
  service_type: string | null
  status: string
  driver_id: number | null
  truck_number: string | null
  trip_status: string | null
  completed_at: string | null
  on_time: boolean | null
  lifecycle: OrderLifecycle
}

const LIFECYCLE_TONE: Record<OrderLifecycle, 'blue' | 'amber' | 'green' | 'gray'> = {
  open: 'blue',
  upcoming: 'amber',
  ongoing: 'amber',
  completed: 'green',
}

function classify(tripStatus: string | null, completedAt: string | null): OrderLifecycle {
  if (completedAt) return 'completed'
  if (tripStatus === 'assigned') return 'upcoming'
  if (tripStatus) return 'ongoing'
  return 'open'
}

export default function Orders() {
  const [rows, setRows] = React.useState<OrderRow[]>([])
  const [labels, setLabels] = React.useState<Record<number, string>>({})
  const [filter, setFilter] = React.useState<'all' | OrderLifecycle>('all')
  const [sorting, setSorting] = React.useState<SortingState>([{ id: 'requested_at', desc: true }])

  const refresh = React.useCallback(async () => {
    const { data: quotes, error } = await supabase
      .schema('live')
      .from('quote_requests')
      .select('quote_id,requested_at,requested_pickup_at,origin_location_id,dest_location_id,weight_lbs,pallets,load_type,service_type,status')
      .order('requested_at', { ascending: false })
      .limit(200)
    if (error || !quotes) {
      console.error('orders refresh failed', error)
      return
    }

    const quoteIds = quotes.map((q) => q.quote_id)
    // live.trips has no truck_number column of its own (only live.driver_status does) --
    // resolve it via the driver.
    const { data: trips } = await supabase
      .schema('live')
      .from('trips')
      .select('trip_id,quote_id,driver_id,status')
      .in('quote_id', quoteIds)
    const driverIds = Array.from(new Set((trips ?? []).map((t) => t.driver_id)))
    const { data: statuses } = driverIds.length > 0
      ? await supabase.schema('live').from('driver_status').select('driver_id,truck_number').in('driver_id', driverIds)
      : { data: [] as { driver_id: number; truck_number: string }[] }
    const truckByDriver = new Map((statuses ?? []).map((s) => [s.driver_id, s.truck_number]))
    const tripByQuote = new Map(
      (trips ?? []).map((t) => [t.quote_id, { ...t, truck_number: truckByDriver.get(t.driver_id) ?? '—' }]),
    )

    const realTripIds = (trips ?? []).map((t) => t.trip_id)
    const { data: logs } = realTripIds.length > 0
      ? await supabase.schema('live').from('trip_log').select('trip_id,completed_at,on_time').in('trip_id', realTripIds)
      : { data: [] as { trip_id: string; completed_at: string; on_time: boolean | null }[] }
    const logByTrip = new Map((logs ?? []).map((l) => [l.trip_id, l]))

    const result: OrderRow[] = quotes.map((q) => {
      const trip = tripByQuote.get(q.quote_id)
      const log = trip ? logByTrip.get(trip.trip_id) : undefined
      return {
        quote_id: q.quote_id,
        requested_at: q.requested_at,
        requested_pickup_at: q.requested_pickup_at,
        origin_location_id: q.origin_location_id,
        dest_location_id: q.dest_location_id,
        weight_lbs: q.weight_lbs,
        pallets: q.pallets,
        load_type: q.load_type,
        service_type: q.service_type,
        status: q.status,
        driver_id: trip?.driver_id ?? null,
        truck_number: trip?.truck_number ?? null,
        trip_status: trip?.status ?? null,
        completed_at: log?.completed_at ?? null,
        on_time: log?.on_time ?? null,
        lifecycle: classify(trip?.status ?? null, log?.completed_at ?? null),
      }
    })
    setRows(result)

    const ids = Array.from(new Set(quotes.flatMap((q) => [q.origin_location_id, q.dest_location_id]).filter((x): x is number => x != null)))
    const missing = ids.filter((id) => !(id in labels))
    if (missing.length > 0) {
      const { data: locs } = await supabase.schema('reference').from('locations').select('location_id,label').in('location_id', missing)
      if (locs) setLabels((prev) => ({ ...prev, ...Object.fromEntries(locs.map((l) => [l.location_id, l.label as string])) }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [labels])

  React.useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const filtered = filter === 'all' ? rows : rows.filter((r) => r.lifecycle === filter)

  const columns: ColumnDef<OrderRow>[] = [
    {
      // Real user feedback: "we need quote ID or trip ID to manage order in Dispatch, but the
      // Orders page shows nothing of this -- how can we edit the order then?" -- show the real
      // quote_id (title attribute carries the full UUID) and link straight into Dispatch's
      // Manage Order tab with it pre-filled, instead of making the manager copy an ID by hand.
      id: 'quote_id',
      header: 'Order ID',
      accessorFn: (r) => r.quote_id,
      cell: (c) => {
        const id = c.getValue() as string
        return (
          <Link
            to={`/manager/dispatch?manage=${id}`}
            title={id}
            className="flex items-center gap-1 font-mono text-[11px] text-brand-600 hover:underline"
          >
            <SquarePen className="size-3" />
            {id.slice(0, 8)}
          </Link>
        )
      },
    },
    { accessorKey: 'requested_at', header: 'Requested', cell: (c) => format(new Date(c.getValue() as string), 'MMM d, HH:mm') },
    {
      id: 'lane',
      header: 'Lane',
      accessorFn: (r) => `${labels[r.origin_location_id ?? -1] ?? ''} → ${labels[r.dest_location_id ?? -1] ?? ''}`,
      cell: (c) => <span className="whitespace-nowrap">{c.getValue() as string}</span>,
    },
    {
      id: 'load',
      header: 'Load',
      accessorFn: (r) => `${r.weight_lbs ?? '—'} lbs · ${r.pallets ?? '—'} plt · ${r.load_type ?? '—'}`,
    },
    {
      // Real user feedback: "we need to show the status of orders as FTL or LTL ... in the sim
      // and in the orders and all and live as well." FTL = direct to destination, no
      // intermediate stops; LTL = consolidated, gets a real second pickup in the sim engine
      // (sim/engine/run_sim.py's STOPOFF handling) and a rate premium (sim/config.py).
      accessorKey: 'service_type',
      header: 'Service',
      cell: (c) => <Badge tone={c.getValue() === 'LTL' ? 'amber' : 'blue'}>{(c.getValue() as string) ?? 'FTL'}</Badge>,
    },
    {
      id: 'driver',
      header: 'Driver / Truck',
      accessorFn: (r) => (r.driver_id ? `#${r.driver_id} / ${r.truck_number}` : '—'),
    },
    {
      accessorKey: 'lifecycle',
      header: 'Status',
      cell: (c) => <Badge tone={LIFECYCLE_TONE[c.getValue() as OrderLifecycle]}>{c.getValue() as string}</Badge>,
    },
    {
      accessorKey: 'on_time',
      header: 'On-time',
      cell: (c) => {
        const v = c.getValue() as boolean | null
        if (v == null) return <span className="text-ink-300">—</span>
        return <Badge tone={v ? 'green' : 'red'}>{v ? 'yes' : 'no'}</Badge>
      },
    },
  ]

  const table = useReactTable({
    data: filtered,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  const counts = {
    all: rows.length,
    open: rows.filter((r) => r.lifecycle === 'open').length,
    upcoming: rows.filter((r) => r.lifecycle === 'upcoming').length,
    ongoing: rows.filter((r) => r.lifecycle === 'ongoing').length,
    completed: rows.filter((r) => r.lifecycle === 'completed').length,
  }

  return (
    <div className="flex h-screen flex-col">
      <PageHeader title="Orders" description="Every quote, from request through delivery — completed, ongoing, and upcoming." />
      <div className="flex flex-1 flex-col gap-3 overflow-auto p-6">
        <div className="flex gap-1.5">
          {(['all', 'open', 'upcoming', 'ongoing', 'completed'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium capitalize ${filter === f ? 'bg-brand-600 text-white' : 'bg-ink-100 text-ink-600 hover:bg-ink-200'}`}
            >
              {f} ({counts[f]})
            </button>
          ))}
        </div>
        {filtered.length === 0 ? (
          <p className="text-sm text-ink-400">No orders in this view yet.</p>
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
              {table.getRowModel().rows.map((row) => (
                <TableRow key={row.id}>
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <p className="text-[11px] text-ink-400">Refreshes every 15s.</p>
      </div>
    </div>
  )
}
