import { format } from 'date-fns'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { useAuth } from '@/lib/auth-context'
import { supabase } from '@/lib/supabase'

// live.driver_ratings is a security_invoker VIEW over live.trip_log (sim/sql/036) -- it respects
// THIS driver's own RLS policy ("drivers read own trip_log"), so this query only ever returns
// this driver's own aggregate, never another driver's, enforced at the database, not by this
// component choosing not to ask for more.
interface RatingRow {
  trips_completed: number
  on_time_rate: number | null
  avg_post_delivery_deadhead_miles: number | null
  avg_hos_stranding_risk: number | null
  avg_load_fill_ratio: number | null
}
interface TripLogRow {
  completed_at: string
  loaded_miles: number | null
  on_time: boolean | null
  breakdown_occurred: boolean
}

export default function MyStats() {
  const { profile } = useAuth()
  const [rating, setRating] = React.useState<RatingRow | null>(null)
  const [recent, setRecent] = React.useState<TripLogRow[]>([])

  React.useEffect(() => {
    if (!profile?.driver_id) return
    supabase.schema('live').from('driver_ratings').select('*').eq('driver_id', profile.driver_id).maybeSingle()
      .then(({ data }) => setRating(data as RatingRow | null))
    supabase.schema('live').from('trip_log').select('completed_at,loaded_miles,on_time,breakdown_occurred')
      .eq('driver_id', profile.driver_id).order('completed_at', { ascending: false }).limit(10)
      .then(({ data }) => setRecent((data as TripLogRow[]) ?? []))
  }, [profile?.driver_id])

  return (
    <div className="flex flex-col gap-4 p-4 sm:p-6">
      <h1 className="font-display text-lg font-bold text-ink-900">My Stats</h1>

      {!rating || rating.trips_completed === 0 ? (
        <p className="text-sm text-ink-400">No completed trips yet — stats will show up here after your first delivery.</p>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <Card><CardContent className="pt-4"><div className="text-xs text-ink-400">Trips completed</div><div className="font-display text-xl font-bold">{rating.trips_completed}</div></CardContent></Card>
          <Card><CardContent className="pt-4"><div className="text-xs text-ink-400">On-time rate</div><div className="font-display text-xl font-bold">{rating.on_time_rate != null ? `${(rating.on_time_rate * 100).toFixed(0)}%` : '—'}</div></CardContent></Card>
          <Card><CardContent className="pt-4"><div className="text-xs text-ink-400">Avg post-delivery deadhead</div><div className="font-display text-xl font-bold">{rating.avg_post_delivery_deadhead_miles != null ? `${rating.avg_post_delivery_deadhead_miles.toFixed(1)} mi` : '—'}</div></CardContent></Card>
          <Card><CardContent className="pt-4"><div className="text-xs text-ink-400">Avg load fill</div><div className="font-display text-xl font-bold">{rating.avg_load_fill_ratio != null ? `${(rating.avg_load_fill_ratio * 100).toFixed(0)}%` : '—'}</div></CardContent></Card>
        </div>
      )}

      <div>
        <h2 className="mb-2 font-display text-sm font-semibold text-ink-900">Recent trips</h2>
        {recent.length === 0 ? (
          <p className="text-xs text-ink-400">Nothing yet.</p>
        ) : (
          <div className="flex flex-col gap-2">
            {recent.map((t, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <div key={i} className="flex items-center justify-between rounded-lg border border-ink-200 bg-white p-3 text-xs">
                <span className="text-ink-500">{format(new Date(t.completed_at), 'MMM d, HH:mm')}</span>
                <span>{t.loaded_miles?.toFixed(1) ?? '—'} mi</span>
                {t.breakdown_occurred && <Badge tone="red">breakdown</Badge>}
                <Badge tone={t.on_time == null ? 'gray' : t.on_time ? 'green' : 'red'}>{t.on_time == null ? 'unknown' : t.on_time ? 'on-time' : 'late'}</Badge>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
