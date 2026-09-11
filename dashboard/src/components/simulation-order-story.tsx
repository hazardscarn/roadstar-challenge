import { format } from 'date-fns'
import { Home, Loader2, Satellite, X } from 'lucide-react'
import * as React from 'react'
import { FleetMap, type MapPin, type RouteSegment } from '@/components/fleet-map'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { getOrderStory, type OrderStory, type OrderStoryCycle } from '@/lib/simulation-api'

function toRoute(trajectory: [number, number, number][], color: string, weight?: number): RouteSegment | null {
  if (!trajectory || trajectory.length < 2) return null
  return { coords: trajectory.map(([, lat, lon]) => [lon, lat] as [number, number]), color, weight }
}

// Real user feedback: "another page ... that will show the whole story of an order I select --
// from candidates available to candidate selected, the trip, the savings ... whether a geofence
// happened, the trip log updates, and the subsequent trip if any (was this guy having a
// deadhead)." A full-screen drill-in, not a new route -- keeps the live playback state (cursor,
// map) intact underneath while this is open.
export function SimulationOrderStory({ quoteId, onClose }: { quoteId: string; onClose: () => void }) {
  const [story, setStory] = React.useState<OrderStory | null>(null)
  const [loading, setLoading] = React.useState(true)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    setLoading(true)
    setError(null)
    getOrderStory(quoteId)
      .then(setStory)
      .catch((err) => setError(err instanceof Error ? err.message : 'Could not load this order'))
      .finally(() => setLoading(false))
  }, [quoteId])

  return (
    <div className="fixed inset-0 z-[1000] flex items-stretch justify-end bg-black/30" onClick={onClose}>
      <div
        className="flex h-full w-full max-w-3xl flex-col overflow-hidden bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-ink-200 px-5 py-3">
          <div>
            <h2 className="font-display text-lg font-bold text-ink-900">Order story</h2>
            <p className="text-xs text-ink-400">quote {quoteId.slice(0, 8)} — from quote to delivery, every real step</p>
          </div>
          <button onClick={onClose} className="rounded-md p-1.5 text-ink-400 hover:bg-ink-100"><X className="size-5" /></button>
        </div>

        <div className="flex-1 overflow-auto p-5">
          {loading && <div className="flex items-center gap-2 text-sm text-ink-400"><Loader2 className="size-4 animate-spin" /> Loading…</div>}
          {error && <p className="text-sm text-status-red-500">{error}</p>}
          {story && <StoryBody story={story} />}
        </div>
      </div>
    </div>
  )
}

function StoryBody({ story }: { story: OrderStory }) {
  const { quote, candidates, deadhead_vs_avg_alternative: dv, trip } = story
  const sortedCandidates = [...candidates].sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999))

  return (
    <div className="flex flex-col gap-6">
      {/* 1. The order */}
      <section>
        <SectionLabel n={1} title="The order came in" />
        <div className="rounded-lg border border-ink-200 p-3 text-sm">
          <div className="flex items-center gap-1.5">
            <span className="font-medium text-ink-800">{quote.origin_label} → {quote.dest_label}</span>
            <Badge tone={quote.service_type === 'LTL' ? 'amber' : 'blue'}>{quote.service_type}</Badge>
          </div>
          <div className="mt-1 text-xs text-ink-500">
            Quote time {format(new Date(quote.requested_at), 'MMM d, h:mm a')} · Requested pickup {format(new Date(quote.requested_pickup_at), 'MMM d, h:mm a')}
          </div>
          <div className="mt-1 text-xs text-ink-500">
            {quote.weight_lbs.toLocaleString()} lbs · {quote.pallets} pallets · {quote.load_type}
          </div>
        </div>
      </section>

      {/* 2. Candidates evaluated -- real user feedback: "are we only evaluating 10 candidates or
          scored for all 30 drivers? show the score for all 30 along with the features we use."
          The engine always scored every feasible driver in the fleet; only the persisted audit
          trail was capped at 10 (a constant meant for a different, unrelated training contract --
          see run_sim.py's comment) -- fixed at the source, so this is now the REAL full list. */}
      <section>
        <SectionLabel n={2} title={`${sortedCandidates.length} candidates evaluated`} />
        <p className="mb-1.5 text-[11px] text-ink-400">
          Every driver with a legal (HOS-feasible) truck at decision time, ranked by the model's total score — <strong>higher
          score = better</strong>; rank 1 is always the highest score, and rank 1 is always who got assigned. Scores can be
          negative (they include forward-looking risk terms, not just $) — compare them to EACH OTHER, not to zero.
        </p>
        <div className="flex max-h-96 flex-col gap-1.5 overflow-auto pr-1">
          {sortedCandidates.map((c) => (
            <div
              key={c.driver_id}
              className={`rounded-md border p-2 text-xs ${c.was_assigned ? 'border-brand-400 bg-brand-50' : 'border-ink-200'}`}
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-ink-800">#{c.rank} — Driver {c.driver_id} · Truck {c.truck_number}</span>
                {c.was_assigned && <Badge tone="green">selected</Badge>}
              </div>
              <div className="mt-0.5 text-ink-500">
                score <strong className="text-ink-700">{c.score?.toFixed(1)}</strong> · {c.deadhead_miles?.toFixed(1)} mi deadhead · {c.hos_remaining_hours?.toFixed(1)}h HOS left
              </div>
              <div className="mt-0.5 text-ink-400">
                {c.planned_driving_hours?.toFixed(1)}h driving · {c.planned_duty_hours?.toFixed(1)}h on-duty this trip
                {c.truck_breakdown_risk != null && ` · ${(c.truck_breakdown_risk * 100).toFixed(0)}% breakdown risk`}
                {c.truck_pct_km_interval != null && ` · ${(c.truck_pct_km_interval * 100).toFixed(0)}% of service-km interval`}
                {c.truck_pct_days_interval != null && ` · ${(c.truck_pct_days_interval * 100).toFixed(0)}% of service-day interval`}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* 3. Why this one -- the real, computed comparison */}
      {dv && (
        <section>
          <SectionLabel n={3} title="Why this driver" />
          <div className="rounded-lg border border-ink-200 bg-ink-50 p-3 text-sm">
            <p>
              Driver {story.chosen_driver_id} had <strong>{dv.chosen_deadhead_miles?.toFixed(1)} mi</strong> of deadhead to reach pickup, vs an average of{' '}
              <strong>{dv.avg_alternative_deadhead_miles.toFixed(1)} mi</strong> across the {dv.n_other_candidates} other real candidates the model considered.
            </p>
            <p className={`mt-1.5 font-semibold ${dv.value_saved >= 0 ? 'text-status-green-500' : 'text-ink-600'}`}>
              {dv.value_saved >= 0
                ? `≈ $${dv.value_saved.toFixed(0)} less deadhead cost than the average alternative candidate.`
                : `≈ $${Math.abs(dv.value_saved).toFixed(0)} more deadhead cost than average — likely picked for a different reason (HOS margin, truck health, or future positioning value).`}
            </p>
            <p className="mt-1.5 text-[11px] text-ink-400">
              Compared against the real OTHER candidates this exact order actually had on the table, not a theoretical baseline.
            </p>
          </div>
        </section>
      )}

      {/* 4. The full cycle -- real user feedback: "order story should show all the trips driver
          took in that cycle instead of just before this and after ... P1 P2 ... D1 D2 ... H etc."
          A cycle = home base back to home base; this is EVERY leg in it, not just the ones
          immediately adjacent to this order, with the real map and the real $ this driver's
          efficient chaining saved along the way -- including the end-of-cycle return to base,
          the other real "never running empty" thing the model optimizes for besides pickup
          deadhead. */}
      {trip?.cycle && (
        <section>
          <SectionLabel n={4} title="The full cycle — home base to home base" />
          <CycleView cycle={trip.cycle} driverId={trip.driver_id} />
        </section>
      )}

      {/* 5. The trip itself */}
      {trip ? (
        <>
          <section>
            <SectionLabel n={5} title="The trip" />
            <div className="flex flex-col gap-2">
              {trip.previous_trip && (
                <TimelineRow
                  label={`Previous job delivered — ${trip.previous_trip.origin_label ?? '?'} → ${trip.previous_trip.dest_label ?? '?'}`}
                  at={trip.previous_trip.completed_at ?? trip.previous_trip.assigned_at}
                  faded
                />
              )}
              <TimelineRow label="Assigned" at={trip.assigned_at} />
              {trip.geofence_events.map((e, i) => (
                // eslint-disable-next-line react/no-array-index-key
                <TimelineRow key={i} label={`${e.event_type === 'arrival' ? 'Arrived' : 'Departed'} — ${e.location_label ?? 'facility'} (geofence-confirmed)`} at={e.occurred_at} confirmed />
              ))}
              <TimelineRow label="Delivered" at={trip.completed_at} badge={trip.on_time ? { tone: 'green', text: 'on time' } : { tone: 'amber', text: 'late' }} />
              {trip.next_trip && (
                <TimelineRow
                  label={`Next job assigned — ${trip.next_trip.origin_label ?? '?'} → ${trip.next_trip.dest_label ?? '?'}`}
                  at={trip.next_trip.assigned_at}
                  faded
                />
              )}
            </div>
          </section>

          {/* 6. The money */}
          <section>
            <SectionLabel n={6} title="The savings" />
            <div className="grid grid-cols-2 gap-2 rounded-lg border border-ink-200 p-3 text-sm">
              <Stat label="Revenue" value={`$${trip.order_revenue.toFixed(2)}`} />
              <Stat label="Deadhead cost (pre-pickup)" value={`$${trip.deadhead_cost.toFixed(2)}`} />
              <Stat
                label="Reloaded immediately after?"
                value={
                  trip.reloaded_immediately
                    ? trip.reload_savings_value > 0
                      ? `Yes — ≈ $${trip.reload_savings_value.toFixed(0)} saved vs. the average other candidate for that next job`
                      : 'Yes — no empty return leg needed'
                    : `No — $${trip.post_delivery_deadhead_cost.toFixed(2)} empty return`
                }
                tone={trip.reloaded_immediately ? 'green' : undefined}
              />
              <Stat label="Late penalty" value={`$${trip.lateness_penalty.toFixed(2)}`} />
              {trip.detention.some((d) => d.amount > 0) && (
                <Stat label="Detention" value={`$${trip.detention.reduce((s, d) => s + d.amount, 0).toFixed(2)}`} />
              )}
              {trip.had_breakdown && <div className="col-span-2 text-xs text-ink-400">A breakdown occurred on this trip (fleet maintenance, outside dispatch scope).</div>}
            </div>
            <p className="mt-1.5 text-[11px] text-ink-400">
              "Reloaded immediately" is the SAME real figure as the order book's "+$ saved" badge — the value of the driver's
              NEXT job needing no deadhead, vs. the real other candidates that next job actually had on the table. See the full
              cycle above for every leg's own breakdown, and the end-of-cycle return-to-base figure.
            </p>
            {trip.invoice && (
              <div className="mt-2 rounded-lg border border-ink-200 p-3 text-xs text-ink-500">
                Invoice <strong>{trip.invoice.invoice_number}</strong>: linehaul ${trip.invoice.linehaul_amount.toFixed(2)}
                {trip.invoice.detention_amount > 0 && ` + detention $${trip.invoice.detention_amount.toFixed(2)}`}
                {' '}+ fuel surcharge ${trip.invoice.fuel_surcharge_amount.toFixed(2)} + HST ${trip.invoice.tax_amount.toFixed(2)} ={' '}
                <strong>${trip.invoice.total_amount.toFixed(2)}</strong> ({trip.invoice.status})
              </div>
            )}
          </section>
        </>
      ) : (
        <p className="text-sm text-ink-400">No truck could be assigned to this order — a lost-opportunity case, not a trip.</p>
      )}
    </div>
  )
}

// The full cycle: every leg the driver ran between two home-base touches, labeled P1/D1/P2/D2/...,
// mapped with the CURRENT order's own leg highlighted, and the real $ figures for both kinds of
// deadhead the model optimizes away -- mid-cycle (reload_immediate, per leg) and end-of-cycle
// (closed_via: did this cycle land back at base via a real paid trip, or would it need an assumed
// empty return).
// already-driven / current / still-ahead, in that order -- shared between the route lines and the
// stop pins so a pin's color always matches the leg color it belongs to.
const CYCLE_COLORS = ['#111827', '#1e46b3', '#92400e'] as const

function CycleView({ cycle, driverId }: { cycle: OrderStoryCycle; driverId: number }) {
  const [satellite, setSatellite] = React.useState(false)
  const currentIndex = cycle.trips.findIndex((t) => t.is_current)
  const colorIdxFor = (i: number) => (i < currentIndex ? 0 : i === currentIndex ? 1 : 2)

  // Solid, high-contrast lines. Real user feedback: red for "still ahead" blended into the
  // basemap's own red/orange highway shading and was hard to tell apart from the driven legs at a
  // glance -- switched to brown, a color nothing else on this map uses. Already driven = black,
  // the CURRENT leg = blue and bold, legs still ahead = brown.
  const routes: RouteSegment[] = cycle.trips
    .map((t, i) => toRoute(t.trajectory, CYCLE_COLORS[colorIdxFor(i)], t.is_current ? 5 : 3))
    .filter((r): r is RouteSegment => r !== null)
  const fitTo = routes.flatMap((r) => r.coords.map(([lon, lat]) => [lat, lon] as [number, number]))

  // Real user feedback: "so hard to understand the trips with so many lines -- can we have the P1
  // D1 and all added in the map also?" -- one pin per real stop, labeled with its P/D tag, colored
  // to match the leg it belongs to. Consecutive legs share a physical stop (this trip's drop-off
  // IS the next trip's pickup) -- merged into ONE pin with a combined label ("D1/P2") instead of
  // two markers stacked exactly on top of each other.
  type Waypoint = { lat: number; lon: number; label: string; colorIdx: number }
  const waypoints: Waypoint[] = []
  cycle.trips.forEach((t, i) => {
    const start = t.trajectory[0]
    const end = t.trajectory[t.trajectory.length - 1]
    if (start) waypoints.push({ lat: start[1], lon: start[2], label: i === 0 ? `H/${t.pickup_label}` : t.pickup_label, colorIdx: colorIdxFor(i) })
    if (end) {
      const isHomeClose = i === cycle.trips.length - 1 && cycle.closed_via === 'trip'
      waypoints.push({ lat: end[1], lon: end[2], label: isHomeClose ? `${t.dropoff_label}/H` : t.dropoff_label, colorIdx: colorIdxFor(i) })
    }
  })
  const pins: MapPin[] = []
  for (const wp of waypoints) {
    const existing = pins.find((p) => Math.abs(p.lat - wp.lat) < 0.001 && Math.abs(p.lon - wp.lon) < 0.001)
    if (existing) {
      existing.permanentLabel = `${existing.permanentLabel}/${wp.label}`
      if (wp.colorIdx === 1) existing.color = CYCLE_COLORS[1] // the current leg's own stop always reads blue, even if merged with a black/brown neighbor
    } else {
      pins.push({ lat: wp.lat, lon: wp.lon, color: CYCLE_COLORS[wp.colorIdx], permanentLabel: wp.label })
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {/* The labeled strip -- H -> P1 -> D1 -> P2 -> D2 -> ... -> H (or "still away") */}
      <div className="flex flex-wrap items-center gap-1 rounded-lg border border-ink-200 bg-ink-50 p-2.5 text-xs">
        <StopBadge label="H" tone="home" title="Home base" />
        {cycle.trips.map((t, i) => (
          // eslint-disable-next-line react/no-array-index-key
          <React.Fragment key={t.trip_id}>
            <Arrow />
            <StopBadge label={t.pickup_label} tone={t.is_current ? 'current' : 'normal'} title={`Pickup — ${t.origin_label ?? '?'}`} />
            {t.reload_immediate && i > 0 && <span className="mx-0.5 text-status-green-500" title="Reloaded immediately, no deadhead to reach this pickup">⚡</span>}
            <Arrow />
            <StopBadge label={t.dropoff_label} tone={t.is_current ? 'current' : 'normal'} title={`Drop-off — ${t.dest_label ?? '?'}`} />
            {t.reload_immediate && i < cycle.trips.length - 1 && (
              <span className="ml-0.5 rounded-full bg-status-green-50 px-1.5 py-0.5 text-[10px] font-medium text-status-green-600">
                +${t.reload_savings_value.toFixed(0)}
              </span>
            )}
          </React.Fragment>
        ))}
        <Arrow />
        {cycle.closed_via === 'trip' ? (
          <StopBadge label="H" tone="home" title="Back at home base — a real paid trip landed here" />
        ) : (
          <span className="flex items-center gap-1 rounded-full border border-status-amber-300 bg-status-amber-50 px-2 py-0.5 text-[11px] font-medium text-status-amber-600">
            still away — ~{cycle.empty_return_miles.toFixed(0)}mi to {cycle.home_hub_label ?? 'home base'}
          </span>
        )}
      </div>

      <div className="h-64 overflow-hidden rounded-lg border border-ink-200">
        <div className="relative h-full">
          <FleetMap drivers={[]} satellite={satellite} routes={routes} pins={pins} fitTo={fitTo.length ? fitTo : undefined} />
          <label className="absolute right-2 top-2 z-[1000] flex items-center gap-1.5 rounded-md bg-white/95 px-2 py-1 text-[11px] text-ink-600 shadow">
            <Satellite className="size-3.5" />
            <Switch checked={satellite} onCheckedChange={setSatellite} />
          </label>
        </div>
      </div>

      <div className="flex flex-wrap gap-3 text-[11px] text-ink-500">
        <span className="flex items-center gap-1"><span className="h-1 w-4 rounded-full bg-black" /> already driven</span>
        <span className="flex items-center gap-1"><span className="h-1 w-4 rounded-full bg-[#1e46b3]" /> this order</span>
        <span className="flex items-center gap-1"><span className="h-1 w-4 rounded-full bg-[#92400e]" /> still ahead</span>
      </div>

      <p className="text-sm text-ink-600">
        Driver {driverId} ran <strong>{cycle.n_trips}</strong> trip{cycle.n_trips === 1 ? '' : 's'} this cycle, earning{' '}
        <strong>${cycle.total_revenue.toFixed(0)}</strong> in revenue. It{' '}
        {cycle.closed_via === 'trip' ? (
          <>closed with a <strong className="text-status-green-500">real paid trip landing right at home base</strong> — 0 extra empty-return miles needed.</>
        ) : (
          <>
            was still away from home base when this run's data ends — an assumed{' '}
            <strong className="text-status-amber-600">~{cycle.empty_return_miles.toFixed(0)} mi (≈ ${cycle.empty_return_value.toFixed(0)})</strong>{' '}
            empty return to {cycle.home_hub_label ?? 'home base'} would be needed to close it.
          </>
        )}
      </p>
    </div>
  )
}

function StopBadge({ label, tone, title }: { label: string; tone: 'home' | 'current' | 'normal'; title: string }) {
  const toneClasses =
    tone === 'home' ? 'border-ink-400 bg-ink-100 text-ink-700'
    : tone === 'current' ? 'border-brand-500 bg-brand-500 text-white ring-2 ring-brand-200'
    : 'border-ink-200 bg-white text-ink-600'
  return (
    <span title={title} className={`flex size-6 shrink-0 items-center justify-center rounded-full border text-[10px] font-bold ${toneClasses}`}>
      {tone === 'home' ? <Home className="size-3" /> : label}
    </span>
  )
}

function Arrow() {
  return <span className="text-ink-300">→</span>
}

function SectionLabel({ n, title }: { n: number; title: string }) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-brand-600 text-[10px] font-bold text-white">{n}</span>
      <h3 className="font-display text-sm font-semibold text-ink-900">{title}</h3>
    </div>
  )
}

function TimelineRow({ label, at, confirmed, faded, badge }: { label: string; at: string | null; confirmed?: boolean; faded?: boolean; badge?: { tone: 'green' | 'amber'; text: string } }) {
  return (
    <div className={`flex items-center gap-3 text-xs ${faded ? 'opacity-60' : ''}`}>
      <span className="w-24 shrink-0 text-ink-400">{at ? format(new Date(at), 'MMM d, HH:mm') : '—'}</span>
      <span className={`size-1.5 shrink-0 rounded-full ${confirmed ? 'bg-status-green-500' : faded ? 'bg-ink-300' : 'bg-brand-500'}`} />
      <span className="flex-1 text-ink-700">{label}</span>
      {badge && <Badge tone={badge.tone}>{badge.text}</Badge>}
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'green' }) {
  return (
    <div>
      <div className="text-ink-400">{label}</div>
      <div className={`font-semibold ${tone === 'green' ? 'text-status-green-500' : 'text-ink-800'}`}>{value}</div>
    </div>
  )
}
