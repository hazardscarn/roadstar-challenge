import { ChevronDown, ChevronUp } from 'lucide-react'
import * as React from 'react'
import type { FleetMetrics } from '@/lib/simulation-api'

// Real user feedback: "for metrics shown right, I think we can show a few more (maybe an expander
// that will show all this without making the map view small)". These are the direct sim-demo
// equivalents of what real backtesting already showed (sim/backtest/real_data_replay.py's
// run_cycle_analysis(), --cycles) -- cycle-based metrics, daily HOS utilization, trips-per-driver
// spread -- shown as DISTRIBUTIONS (a small histogram), not just an average, per that same request.
// Collapsed by default: a single toggle row, full width, ABOVE the map+aside flex row -- expanding
// it only grows this row's own height, the map's WIDTH is never touched.

function MiniHistogram({ values, unit, bins = 10 }: { values: number[]; unit: string; bins?: number }) {
  if (values.length === 0) return <p className="text-[11px] text-ink-400">No data this run.</p>
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const counts = new Array(bins).fill(0)
  for (const v of values) {
    const idx = Math.min(bins - 1, Math.floor(((v - min) / span) * bins))
    counts[idx] += 1
  }
  const maxCount = Math.max(...counts)
  return (
    <div>
      <div className="flex h-12 items-end gap-0.5">
        {counts.map((c, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <div key={i} className="flex-1 rounded-sm bg-brand-300" style={{ height: `${maxCount ? (c / maxCount) * 100 : 0}%`, minHeight: c > 0 ? '2px' : 0 }} title={`${c} at this range`} />
        ))}
      </div>
      <div className="mt-0.5 flex justify-between text-[10px] text-ink-400">
        <span>{min.toFixed(1)}{unit}</span>
        <span>{max.toFixed(1)}{unit}</span>
      </div>
    </div>
  )
}

function Stat({ label, value, sublabel }: { label: string; value: string; sublabel?: string }) {
  return (
    <div>
      <div className="text-[11px] text-ink-500">{label}</div>
      <div className="font-display text-lg font-bold tabular-nums text-ink-900">{value}</div>
      {sublabel && <div className="text-[10px] text-ink-400">{sublabel}</div>}
    </div>
  )
}

export function SimulationFleetMetricsExpander({ metrics }: { metrics: FleetMetrics | null }) {
  const [expanded, setExpanded] = React.useState(false)

  return (
    <div className="border-b border-ink-200 bg-white">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-1.5 px-6 py-1.5 text-xs font-medium text-ink-500 hover:text-ink-800"
      >
        {expanded ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
        More metrics — cycles, HOS utilization, trip distribution (full week)
      </button>
      {expanded && (
        metrics == null ? (
          <p className="px-6 pb-3 text-xs text-ink-400">Not available for this run (saved before this metric set was added — run a fresh week to see it).</p>
        ) : (
          <div className="grid grid-cols-1 gap-6 px-6 pb-4 md:grid-cols-4">
            <div className="flex flex-col gap-3 border-r border-ink-100 pr-4 last:border-0">
              <h3 className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">Fleet utilization</h3>
              <Stat label="Drivers used" value={`${metrics.drivers_used} of ${metrics.driver_pool_size}`} />
              <Stat
                label="Trips per driver"
                value={metrics.trips_per_driver.mean.toFixed(1)}
                sublabel={`± ${metrics.trips_per_driver.std_dev.toFixed(1)} std dev · range ${metrics.trips_per_driver.min}–${metrics.trips_per_driver.max}`}
              />
              <div>
                <div className="mb-1 text-[11px] text-ink-500">Distribution</div>
                <MiniHistogram values={metrics.trips_per_driver.histogram} unit=" trips" />
              </div>
            </div>

            <div className="flex flex-col gap-3 border-r border-ink-100 pr-4 last:border-0">
              <h3 className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                Cycles <span className="font-normal normal-case text-ink-400">(home base → home base)</span>
              </h3>
              <Stat label="Cycles completed" value={String(metrics.cycles.n_cycles)} sublabel={`${metrics.cycles.n_closed_by_trip} closed by a real trip landing home · ${metrics.cycles.n_closed_by_assumed_empty_return} by an assumed empty return`} />
              <Stat label="Avg trips / cycle" value={metrics.cycles.avg_trips_per_cycle.toFixed(1)} />
              <Stat label="Avg revenue / cycle" value={`$${metrics.cycles.avg_revenue_per_cycle.toFixed(0)}`} />
              <Stat label="Avg cycle duration" value={`${metrics.cycles.avg_duration_hours.toFixed(0)}h`} />
            </div>

            <div className="flex flex-col gap-3 border-r border-ink-100 pr-4 last:border-0">
              <h3 className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">Empty return miles / cycle</h3>
              <Stat label="Average" value={`${metrics.cycles.avg_empty_return_miles.toFixed(1)} mi`} />
              <Stat
                label="HOS remaining at return"
                value={metrics.cycles.avg_hos_remaining_at_return != null ? `${metrics.cycles.avg_hos_remaining_at_return.toFixed(1)}h` : 'n/a'}
                sublabel="legal driving hours left, unused, back at home base"
              />
              <div>
                <div className="mb-1 text-[11px] text-ink-500">Distribution</div>
                <MiniHistogram values={metrics.cycles.empty_return_miles_histogram} unit="mi" />
              </div>
            </div>

            <div className="flex flex-col gap-3">
              <h3 className="text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                Daily HOS utilization <span className="font-normal normal-case text-ink-400">(of 13h legal driving limit)</span>
              </h3>
              <Stat
                label="Avg daily utilization"
                value={`${metrics.daily_hos_utilization.avg_utilization_pct.toFixed(0)}%`}
                sublabel={`${metrics.daily_hos_utilization.n_work_days_observed} work-days observed`}
              />
              <Stat
                label="Over the 13h legal limit"
                value={String(metrics.daily_hos_utilization.n_over_13h_limit)}
                sublabel={metrics.daily_hos_utilization.n_over_13h_limit > 0 ? 'sanity-check flag, not a real HOS violation' : 'clean — as expected'}
              />
              <div>
                <div className="mb-1 text-[11px] text-ink-500">Distribution, per driver per day</div>
                <MiniHistogram values={metrics.daily_hos_utilization.utilization_pct_histogram} unit="%" />
              </div>
            </div>
          </div>
        )
      )}
    </div>
  )
}
