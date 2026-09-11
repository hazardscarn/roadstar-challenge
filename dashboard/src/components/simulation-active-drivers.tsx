import * as React from 'react'
import { supabase } from '@/lib/supabase'

// Real user feedback: "we are using both daily continuous limit and weekly limit also right?
// Can we show this as another layer" -- driver_state_snapshots (sim/sql/038) now carries the
// real, DISTINCT HOSState components (not one aggregate repeated) -- daily driving-hours window
// AND the 7-day cycle, shown as two layers per driver.

interface SnapshotRow {
  driver_id: number
  truck_number: string
  snapshot_at: string
  duty_status: string
  hos_driving_hours_remaining: number | null
  hos_cycle1_hours_remaining: number | null
}

function hosTone(hours: number | null): string {
  if (hours == null) return 'text-ink-400'
  if (hours < 1) return 'text-status-red-500'
  if (hours < 3) return 'text-status-amber-500'
  return 'text-status-green-500'
}

export function SimulationActiveDrivers({
  runId, simStart, cursorSeconds, activeDriverIds,
}: {
  runId: string
  simStart: string
  cursorSeconds: number
  activeDriverIds: number[]
}) {
  const [snapshots, setSnapshots] = React.useState<SnapshotRow[]>([])

  React.useEffect(() => {
    ;(async () => {
      const { data } = await supabase
        .schema('simulation')
        .from('driver_state_snapshots')
        .select('driver_id,truck_number,snapshot_at,duty_status,hos_driving_hours_remaining,hos_cycle1_hours_remaining')
        .eq('run_id', runId)
        .order('snapshot_at', { ascending: true })
      setSnapshots((data as unknown as SnapshotRow[]) ?? [])
    })()
  }, [runId])

  const cursorTime = new Date(new Date(simStart).getTime() + cursorSeconds * 1000).getTime()

  const rows = activeDriverIds.map((driverId) => {
    let latest: SnapshotRow | null = null
    for (const s of snapshots) {
      if (s.driver_id !== driverId) continue
      const t = new Date(s.snapshot_at).getTime()
      if (t > cursorTime) break
      latest = s
    }
    return { driverId, snapshot: latest }
  })

  if (rows.length === 0) return null

  return (
    <div className="absolute bottom-3 right-3 z-[400] max-h-64 w-64 overflow-auto rounded-lg border border-ink-200 bg-white/95 p-2 shadow-lg backdrop-blur">
      <div className="mb-1 flex items-center justify-between text-[10px] font-semibold uppercase tracking-wide text-ink-400">
        <span>Active drivers</span>
        <span className="flex gap-2 normal-case font-normal"><span>daily</span><span>weekly</span></span>
      </div>
      <div className="flex flex-col gap-1">
        {rows.map(({ driverId, snapshot }) => (
          <div key={driverId} className="flex items-center justify-between text-[11px]">
            <span className="text-ink-700">D{driverId} · {snapshot?.truck_number ?? '—'}</span>
            <span className="flex gap-2 tabular-nums">
              <span className={`font-semibold ${hosTone(snapshot?.hos_driving_hours_remaining ?? null)}`}>
                {snapshot?.hos_driving_hours_remaining != null ? `${snapshot.hos_driving_hours_remaining.toFixed(1)}h` : '—'}
              </span>
              <span className={`font-semibold ${hosTone(snapshot?.hos_cycle1_hours_remaining ?? null)}`}>
                {snapshot?.hos_cycle1_hours_remaining != null ? `${snapshot.hos_cycle1_hours_remaining.toFixed(0)}h` : '—'}
              </span>
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
