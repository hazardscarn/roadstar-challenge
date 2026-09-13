import { format } from 'date-fns'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { supabase } from '@/lib/supabase'

// Real user pivot: the simulation is now the app's only data source (live.* dropped) --
// simulation.driver_state_snapshots (sim/sql/038), an event-driven feature-state log (HOS
// remaining, duty status) written per trip by sim/live/ai_dispatch_replay.py's generate(), plus
// the real geofence arrival/departure events it writes alongside, interleaved as milestone rows.
interface Row {
  kind: 'snapshot' | 'arrival' | 'departure'
  at: string
  duty_status?: string
  hos_remaining?: number
  breakdown_risk?: number
  location_label?: string
}

export function TripStatusHistory({ tripId }: { tripId: string | null }) {
  const [rows, setRows] = React.useState<Row[]>([])
  const [loading, setLoading] = React.useState(false)

  React.useEffect(() => {
    if (!tripId) {
      setRows([])
      return
    }
    setLoading(true)
    ;(async () => {
      const [{ data: snapshots }, { data: events }] = await Promise.all([
        supabase
          .schema('simulation')
          .from('driver_state_snapshots')
          .select('snapshot_at,duty_status,hos_driving_hours_remaining,truck_breakdown_risk')
          .eq('trip_id', tripId)
          .order('snapshot_at', { ascending: true }),
        supabase.schema('simulation').from('geofence_events').select('event_type,occurred_at,location_id').eq('trip_id', tripId).order('occurred_at', { ascending: true }),
      ])

      let locationLabels: Record<number, string> = {}
      const locIds = Array.from(new Set((events ?? []).map((e) => e.location_id).filter((x): x is number => x != null)))
      if (locIds.length > 0) {
        const { data: locs } = await supabase.schema('reference').from('locations').select('location_id,label').in('location_id', locIds)
        if (locs) locationLabels = Object.fromEntries(locs.map((l) => [l.location_id, l.label as string]))
      }

      const snapshotRows: Row[] = (snapshots ?? []).map((s) => ({
        kind: 'snapshot',
        at: s.snapshot_at,
        duty_status: s.duty_status,
        hos_remaining: s.hos_driving_hours_remaining,
        breakdown_risk: s.truck_breakdown_risk,
      }))
      const milestones: Row[] = (events ?? []).map((e) => ({
        kind: e.event_type as 'arrival' | 'departure',
        at: e.occurred_at,
        location_label: locationLabels[e.location_id as number],
      }))

      const merged = [...snapshotRows, ...milestones].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())
      setRows(merged)
      setLoading(false)
    })()
  }, [tripId])

  if (!tripId) return <p className="p-3 text-xs text-ink-400">No trip selected.</p>
  if (loading) return <p className="p-3 text-xs text-ink-400">Loading history…</p>
  if (rows.length === 0) return <p className="p-3 text-xs text-ink-400">No status updates recorded yet (snapshots write every ~15 minutes).</p>

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Time</TableHead>
          <TableHead>Update</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <TableRow key={i}>
            <TableCell className="whitespace-nowrap text-xs text-ink-500">{format(new Date(r.at), 'HH:mm:ss')}</TableCell>
            <TableCell className="text-xs">
              {r.kind === 'snapshot' && (
                <span>
                  {r.duty_status ?? 'en route'} · {r.hos_remaining != null ? `${r.hos_remaining.toFixed(1)}h HOS left` : '—'}
                  {r.breakdown_risk != null && ` · ${(r.breakdown_risk * 100).toFixed(0)}% breakdown risk`}
                </span>
              )}
              {r.kind === 'arrival' && <Badge tone="green">Arrived · {r.location_label ?? 'facility'}</Badge>}
              {r.kind === 'departure' && <Badge tone="blue">Departed · {r.location_label ?? 'facility'}</Badge>}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
