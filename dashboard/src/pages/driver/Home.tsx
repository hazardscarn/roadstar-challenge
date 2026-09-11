import { format } from 'date-fns'
import { AlertTriangle } from 'lucide-react'
import * as React from 'react'
import { Link } from 'react-router-dom'
import { HosDial } from '@/components/hos-dial'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { useAuth } from '@/lib/auth-context'
import { supabase } from '@/lib/supabase'

// Driver's own current trip/duty status -- RLS-scoped automatically (sim/sql/009_rls.sql's
// "drivers read own status/trips" policies), no manual driver_id filter needed for correctness,
// only for which single row a `.single()`/`.eq()` query targets.
interface DriverStatusRow {
  duty_status: string
  hos_remaining_hours: number
  truck_number: string
  current_trip_id: string | null
}
interface TripRow {
  status: string
  eta: string | null
  dest_location_id: number | null
}

const POLL_MS = 10000

export default function DriverHome() {
  const { profile } = useAuth()
  const [status, setStatus] = React.useState<DriverStatusRow | null>(null)
  const [trip, setTrip] = React.useState<TripRow | null>(null)
  const [destLabel, setDestLabel] = React.useState<string | null>(null)
  const [inspectionOk, setInspectionOk] = React.useState<boolean | null>(null)

  const refresh = React.useCallback(async () => {
    if (!profile?.driver_id) return
    const { data: s } = await supabase
      .schema('live')
      .from('driver_status')
      .select('duty_status,hos_remaining_hours,truck_number,current_trip_id')
      .eq('driver_id', profile.driver_id)
      .maybeSingle()
    setStatus(s as DriverStatusRow | null)

    if (s?.current_trip_id) {
      const { data: t } = await supabase.schema('live').from('trips').select('status,eta,dest_location_id').eq('trip_id', s.current_trip_id).maybeSingle()
      setTrip(t as TripRow | null)
      if (t?.dest_location_id) {
        const { data: loc } = await supabase.schema('reference').from('locations').select('label').eq('location_id', t.dest_location_id).maybeSingle()
        setDestLabel((loc?.label as string) ?? null)
      }
    } else {
      setTrip(null)
    }

    const { data: insp } = await supabase
      .schema('live')
      .from('vehicle_inspections')
      .select('overall_pass,submitted_at')
      .eq('driver_id', profile.driver_id)
      .order('submitted_at', { ascending: false })
      .limit(1)
      .maybeSingle()
    const withinDay = insp?.submitted_at ? Date.now() - new Date(insp.submitted_at).getTime() < 24 * 3600 * 1000 : false
    setInspectionOk(Boolean(insp?.overall_pass && withinDay))
  }, [profile?.driver_id])

  React.useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh])

  if (!status) {
    return <div className="p-6 text-sm text-ink-400">Loading your status…</div>
  }

  return (
    <div className="flex flex-col gap-4 p-4 sm:p-6">
      {inspectionOk === false && (
        <Link
          to="/driver/inspection"
          className="flex items-center gap-2 rounded-lg border border-status-amber-500/30 bg-status-amber-100 px-4 py-3 text-sm text-status-amber-500"
        >
          <AlertTriangle className="size-4 shrink-0" />
          No passing pre-trip inspection on file in the last 24h — you can't be marked available until you submit one.
        </Link>
      )}

      <Card>
        <CardContent className="flex flex-col items-center gap-3 pt-6">
          <HosDial hoursRemaining={status.hos_remaining_hours} />
          <Badge tone={status.duty_status === 'driving' ? 'green' : status.duty_status === 'on_duty_not_driving' ? 'amber' : 'gray'}>
            {status.duty_status}
          </Badge>
          <p className="text-sm text-ink-500">Truck {status.truck_number}</p>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-6">
          <h2 className="mb-2 font-display text-sm font-semibold text-ink-900">Current trip</h2>
          {trip ? (
            <div className="flex flex-col gap-1 text-sm">
              <div className="flex items-center gap-2">
                <span className="text-ink-500">Status:</span>
                <Badge tone="blue">{trip.status}</Badge>
              </div>
              {destLabel && (
                <div>
                  <span className="text-ink-500">Next stop:</span> {destLabel}
                </div>
              )}
              {trip.eta && (
                <div>
                  <span className="text-ink-500">ETA:</span> {format(new Date(trip.eta), 'MMM d, h:mm a')}
                </div>
              )}
            </div>
          ) : (
            <p className="text-sm text-ink-400">No active trip — you'll see it here as soon as dispatch assigns one.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
