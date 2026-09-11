import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface KpiTileProps {
  label: string
  value: string
  icon?: LucideIcon
  hero?: boolean
  tone?: 'neutral' | 'green' | 'amber' | 'red'
  sublabel?: string
}

/** One hero tile + up to 5 context tiles per role's "one hero question" (see plan's UI design
 * grounding). Reused by Live Ops and the Simulation Showcase's accumulating stats band. */
export function KpiTile({ label, value, icon: Icon, hero, tone = 'neutral', sublabel }: KpiTileProps) {
  const toneClasses: Record<string, string> = {
    neutral: 'text-ink-900',
    green: 'text-status-green-500',
    amber: 'text-status-amber-500',
    red: 'text-status-red-500',
  }
  return (
    <div
      className={cn(
        'flex min-w-36 flex-1 flex-col gap-1 rounded-xl border border-ink-200 bg-white px-4 py-3',
        hero && 'border-brand-200 bg-brand-50/60',
      )}
    >
      <div className="flex items-center gap-1.5 text-xs font-medium text-ink-500">
        {Icon && <Icon className="size-3.5" />}
        {label}
      </div>
      <div className={cn('font-display text-2xl font-bold tabular-nums', toneClasses[tone])}>{value}</div>
      {sublabel && <div className="text-[11px] text-ink-400">{sublabel}</div>}
    </div>
  )
}

export function KpiBand({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap gap-3">{children}</div>
}
