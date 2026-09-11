import { format } from 'date-fns'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { useAuth } from '@/lib/auth-context'
import { supabase } from '@/lib/supabase'

interface MaintenanceRow {
  truck_number: string
  cumulative_km_since_service: number
  last_service_at: string | null
  service_interval_km: number
  service_interval_days: number
  maintenance_until: string | null
}

export default function MyTruck() {
  const { profile } = useAuth()
  const [truckNumber, setTruckNumber] = React.useState<string | null>(null)
  const [maint, setMaint] = React.useState<MaintenanceRow | null>(null)

  React.useEffect(() => {
    if (!profile?.driver_id) return
    supabase.schema('live').from('driver_status').select('truck_number').eq('driver_id', profile.driver_id).maybeSingle()
      .then(({ data }) => setTruckNumber((data?.truck_number as string) ?? null))
  }, [profile?.driver_id])

  React.useEffect(() => {
    if (!truckNumber) return
    supabase.schema('live').from('truck_maintenance_state').select('*').eq('truck_number', truckNumber).maybeSingle()
      .then(({ data }) => setMaint(data as MaintenanceRow | null))
  }, [truckNumber])

  if (!truckNumber) return <div className="p-6 text-sm text-ink-400">Loading…</div>

  const pctKm = maint ? maint.cumulative_km_since_service / maint.service_interval_km : 0
  const inShop = maint?.maintenance_until && new Date(maint.maintenance_until) > new Date()

  return (
    <div className="flex flex-col gap-4 p-4 sm:p-6">
      <div>
        <h1 className="font-display text-lg font-bold text-ink-900">My Truck</h1>
        <p className="text-sm text-ink-500">Truck {truckNumber}</p>
      </div>

      {inShop && (
        <div className="rounded-lg border border-status-red-500/30 bg-status-red-100 px-4 py-3 text-sm text-status-red-500">
          In the shop until {format(new Date(maint!.maintenance_until!), 'MMM d, h:mm a')}.
        </div>
      )}

      {maint && (
        <Card>
          <CardContent className="flex flex-col gap-3 pt-6">
            <div className="flex items-center justify-between">
              <span className="text-sm text-ink-600">Service interval used</span>
              <Badge tone={pctKm >= 0.85 ? 'red' : pctKm >= 0.7 ? 'amber' : 'green'}>{(pctKm * 100).toFixed(0)}%</Badge>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-ink-100">
              <div
                className="h-full rounded-full"
                style={{ width: `${Math.min(100, pctKm * 100)}%`, background: pctKm >= 0.85 ? '#d9342b' : pctKm >= 0.7 ? '#e0940f' : '#1f9d55' }}
              />
            </div>
            <div className="text-xs text-ink-500">{Math.round(maint.cumulative_km_since_service).toLocaleString()} km since last service</div>
            {maint.last_service_at && (
              <div className="text-xs text-ink-500">Last serviced {format(new Date(maint.last_service_at), 'MMM d, yyyy')}</div>
            )}
            <p className="mt-2 text-[11px] text-ink-400">
              Synthesized from odometer tracking — no real service-history source exists in this fleet's data.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
