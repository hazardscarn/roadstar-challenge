import { format } from 'date-fns'
import { Loader2, Satellite, X } from 'lucide-react'
import * as React from 'react'
import { FleetMap, type RouteSegment } from '@/components/fleet-map'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { getOrderStory, type OrderStory, type OrderStoryAdjacentTrip } from '@/lib/simulation-api'

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

      {/* 4. The route -- previous trip (if any) -> this trip -> next trip (if any), the driver's
          real continuous path. Real user feedback: "show the previous ride done, and stopped,
          and the ride to new location, and the next ride -- the whole route of them going back
          to home zone." */}
      {trip && (
        <section>
          <SectionLabel n={4} title="The route" />
          <div className="h-64 overflow-hidden rounded-lg border border-ink-200">
            <RouteMap trip={trip} />
          </div>
          <div className="mt-1.5 flex flex-wrap gap-3 text-[11px] text-ink-500">
            {trip.previous_trip && <span className="flex items-center gap-1"><span className="h-1 w-4 rounded-full bg-black" /> previous job</span>}
            <span className="flex items-center gap-1"><span className="h-1 w-4 rounded-full bg-[#1e46b3]" /> this order</span>
            {trip.next_trip && <span className="flex items-center gap-1"><span className="h-1 w-4 rounded-full bg-[#dc2626]" /> next job</span>}
          </div>
        </section>
      )}

      {/* 5. What came before -- explains WHY the deadhead was this low: the driver's previous
          job often ends right where this one begins. */}
      {trip && (
        <section>
          <SectionLabel n={5} title="Before this" />
          {trip.previous_trip ? (
            <div className="rounded-lg border border-ink-200 p-3 text-sm">
              <p>
                Driver {trip.driver_id}'s previous job: {trip.previous_trip.origin_label} → {trip.previous_trip.dest_label}, finishing right
                before this one was assigned{trip.previous_trip.assigned_at && ` (${format(new Date(trip.previous_trip.assigned_at), 'MMM d, h:mm a')})`}.
              </p>
              <p className="mt-1 font-medium text-status-green-500">
                That drop-off is why this pickup only needed <strong>{trip.deadhead_miles.toFixed(1)} mi</strong> of deadhead — the
                driver was already right there, not brought in from somewhere else.
              </p>
            </div>
          ) : (
            <p className="text-sm text-ink-400">
              This was this driver's first job in this run — the {trip.deadhead_miles.toFixed(1)} mi deadhead is from their starting position, not a prior delivery.
            </p>
          )}
        </section>
      )}

      {/* 6. The trip itself */}
      {trip ? (
        <>
          <section>
            <SectionLabel n={6} title="The trip" />
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

          {/* 5. The money */}
          <section>
            <SectionLabel n={7} title="The savings" />
            <div className="grid grid-cols-2 gap-2 rounded-lg border border-ink-200 p-3 text-sm">
              <Stat label="Revenue" value={`$${trip.order_revenue.toFixed(2)}`} />
              <Stat label="Deadhead cost (pre-pickup)" value={`$${trip.deadhead_cost.toFixed(2)}`} />
              <Stat
                label="Return leg"
                value={trip.reloaded_immediately ? 'Reloaded immediately — $0' : `$${trip.post_delivery_deadhead_cost.toFixed(2)} empty return`}
                tone={trip.reloaded_immediately ? 'green' : undefined}
              />
              <Stat label="Late penalty" value={`$${trip.lateness_penalty.toFixed(2)}`} />
              {trip.detention.some((d) => d.amount > 0) && (
                <Stat label="Detention" value={`$${trip.detention.reduce((s, d) => s + d.amount, 0).toFixed(2)}`} />
              )}
              {trip.had_breakdown && <div className="col-span-2 text-xs text-ink-400">A breakdown occurred on this trip (fleet maintenance, outside dispatch scope).</div>}
            </div>
            {trip.invoice && (
              <div className="mt-2 rounded-lg border border-ink-200 p-3 text-xs text-ink-500">
                Invoice <strong>{trip.invoice.invoice_number}</strong>: linehaul ${trip.invoice.linehaul_amount.toFixed(2)}
                {trip.invoice.detention_amount > 0 && ` + detention $${trip.invoice.detention_amount.toFixed(2)}`}
                {' '}+ fuel surcharge ${trip.invoice.fuel_surcharge_amount.toFixed(2)} + HST ${trip.invoice.tax_amount.toFixed(2)} ={' '}
                <strong>${trip.invoice.total_amount.toFixed(2)}</strong> ({trip.invoice.status})
              </div>
            )}
          </section>

          {/* 6. What happened next */}
          <section>
            <SectionLabel n={8} title="What this driver did next" />
            {trip.next_trip ? (
              <div className="rounded-lg border border-ink-200 p-3 text-sm">
                <p>
                  Driver {trip.driver_id}'s next job: {trip.next_trip.origin_label} → {trip.next_trip.dest_label}, assigned{' '}
                  {trip.next_trip.assigned_at && format(new Date(trip.next_trip.assigned_at), 'MMM d, h:mm a')}.
                </p>
                <p className={`mt-1 font-medium ${trip.next_trip.had_deadhead ? 'text-status-amber-500' : 'text-status-green-500'}`}>
                  {trip.next_trip.had_deadhead
                    ? `Had to deadhead ${trip.next_trip.deadhead_miles.toFixed(1)} mi to reach it.`
                    : 'No deadhead — was already positioned for it.'}
                </p>
              </div>
            ) : (
              <p className="text-sm text-ink-400">No further trip for this driver in this run (last job of the week, or still ahead).</p>
            )}
          </section>
        </>
      ) : (
        <p className="text-sm text-ink-400">No truck could be assigned to this order — a lost-opportunity case, not a trip.</p>
      )}
    </div>
  )
}

function RouteMap({ trip }: { trip: { trajectory: [number, number, number][]; previous_trip: OrderStoryAdjacentTrip | null; next_trip: OrderStoryAdjacentTrip | null } }) {
  // Real user feedback: "better line needed, have colors used black blue and red" -- solid,
  // high-contrast lines (the earlier gray/amber dashed scheme was hard to pick out against the
  // basemap) so the three legs of the driver's real continuous route are unambiguous at a glance.
  const routes: RouteSegment[] = []
  const previousRoute = trip.previous_trip ? toRoute(trip.previous_trip.trajectory, '#000000', 5) : null
  if (previousRoute) routes.push(previousRoute)
  const thisRoute = toRoute(trip.trajectory, '#1e46b3', 5)
  if (thisRoute) routes.push(thisRoute)
  const nextRoute = trip.next_trip ? toRoute(trip.next_trip.trajectory, '#dc2626', 5) : null
  if (nextRoute) routes.push(nextRoute)

  const fitTo = routes.flatMap((r) => r.coords.map(([lon, lat]) => [lat, lon] as [number, number]))
  // Real user feedback / hackathon brief: satellite view so a manager can visually inspect the
  // real dock/yard layout for this order's pickup or delivery facility.
  const [satellite, setSatellite] = React.useState(false)

  return (
    <div className="relative h-full">
      <FleetMap drivers={[]} satellite={satellite} routes={routes} fitTo={fitTo.length ? fitTo : undefined} />
      <label className="absolute right-2 top-2 z-[1000] flex items-center gap-1.5 rounded-md bg-white/95 px-2 py-1 text-[11px] text-ink-600 shadow">
        <Satellite className="size-3.5" />
        <Switch checked={satellite} onCheckedChange={setSatellite} />
      </label>
    </div>
  )
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
