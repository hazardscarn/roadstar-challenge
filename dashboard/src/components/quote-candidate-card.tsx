import { format } from 'date-fns'
import { Loader2 } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { ScoreQuoteRow } from '@/lib/api'
import { cn } from '@/lib/utils'

// Real user feedback: order_revenue is the same for every candidate on a given quote (it's a
// property of the JOB, not the driver) -- showing it per-card read as "why do these all say the
// same number, how do I tell them apart?" to a non-technical dispatcher. Fix: state the fixed
// job value once (Dispatch.tsx, above the list), and make each card's headline number the thing
// that actually differs -- net value after deadhead/risk -- in plain language, with the
// deductions spelled out only when non-zero so the "why" is visible without clutter.
const DEDUCTIONS: { key: keyof ScoreQuoteRow; label: string }[] = [
  { key: 'deadhead_cost', label: 'deadhead miles' },
  { key: 'opportunity_cost_penalty', label: 'opportunity cost' },
  { key: 'hos_stranding_risk_penalty', label: 'HOS risk' },
  { key: 'maintenance_risk_penalty', label: 'maintenance risk' },
  { key: 'expected_lateness_penalty', label: 'lateness risk' },
]

export function QuoteCandidateCard({
  row,
  requestedPickupAt,
  assigning,
  isAssigningThis,
  onAssign,
  selected,
  onSelect,
}: {
  row: ScoreQuoteRow
  requestedPickupAt: string
  assigning: boolean
  isAssigningThis: boolean
  onAssign: () => void
  selected?: boolean
  onSelect?: () => void
}) {
  const etaLate = new Date(row.eta_pickup) > new Date(requestedPickupAt)
  const deductions = DEDUCTIONS.map((d) => ({ label: d.label, amount: row[d.key] as number })).filter((d) => d.amount > 0)

  return (
    // biome-ignore lint: card is a click target for "view detail" in addition to its own controls
    <div
      onClick={onSelect}
      className={cn(
        'rounded-lg border p-3 transition-colors',
        onSelect && 'cursor-pointer hover:border-brand-300',
        selected ? 'border-brand-400 bg-brand-50/40 ring-1 ring-brand-300' : 'border-ink-200',
      )}
    >
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-sm font-semibold text-ink-900">
          #{row.rank} — Driver {row.driver_id}
        </span>
        {row.is_mid_route_candidate && <Badge tone="blue">mid-route</Badge>}
      </div>

      <div className="mb-1 flex items-baseline gap-1.5">
        <span className="font-display text-lg font-bold text-ink-900">${row.immediate_reward}</span>
        <span className="text-xs text-ink-400">net to fleet after costs</span>
      </div>

      {deductions.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-x-2 gap-y-0.5 text-[11px] text-status-red-500">
          {deductions.map((d) => (
            <span key={d.label}>− ${d.amount} {d.label}</span>
          ))}
        </div>
      )}

      <div className="mb-2 grid grid-cols-2 gap-x-3 gap-y-0.5 text-xs text-ink-500">
        <span>Truck {row.truck_number}</span>
        <span>{row.deadhead_miles} mi deadhead</span>
      </div>

      <div className={etaLate ? 'mb-2 rounded-md bg-status-amber-100 px-2 py-1.5 text-xs text-status-amber-500' : 'mb-2 rounded-md bg-status-green-100 px-2 py-1.5 text-xs text-status-green-500'}>
        {etaLate ? (
          <>Can't make the requested pickup — ready at <strong>{format(new Date(row.eta_pickup), 'MMM d, h:mm a')}</strong> instead</>
        ) : (
          <>On track for pickup by <strong>{format(new Date(row.eta_pickup), 'MMM d, h:mm a')}</strong></>
        )}
      </div>

      <Button
        size="sm"
        className="w-full"
        disabled={assigning}
        onClick={(e) => {
          e.stopPropagation()
          onAssign()
        }}
      >
        {isAssigningThis ? <Loader2 className="size-4 animate-spin" /> : 'Assign'}
      </Button>
    </div>
  )
}
