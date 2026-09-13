import { format } from 'date-fns'
import { AlertTriangle, CheckCircle2, Package } from 'lucide-react'
import * as React from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  acceptOrder, driverToday, dutyLog, lastInspection, logDutyStatus,
  type DriverToday, type DutyLogEntry, type DutyStatus,
} from '@/lib/driver-api'

// Real user ask: "Driver Assist" -- when a driver logs in, they see ONLY today's trips as
// assigned by the dispatcher the day before (dispatch.day_orders/assignments, the real
// finalized Dispatch Board plan), can accept each load, and log their real duty status --
// the standard 4-status HOS framework (Off Duty, Sleeper Berth, Driving, On-Duty Not Driving --
// 49 CFR Part 395 / Canada's ELD Technical Standard), not a guessed set of statuses.
const DUTY_OPTIONS: { value: DutyStatus; label: string; tone: 'gray' | 'blue' | 'green' | 'amber' }[] = [
  { value: 'off_duty', label: 'Off Duty', tone: 'gray' },
  { value: 'sleeper_berth', label: 'Sleeper Berth', tone: 'blue' },
  { value: 'driving', label: 'Driving', tone: 'green' },
  { value: 'on_duty_not_driving', label: 'On-Duty (Not Driving)', tone: 'amber' },
]

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

export default function DriverHome() {
  // Real user ask: "does this get created automatically for any day the driver have trips
  // assigned... can we have a calendar." There's no separate creation step -- this is a live
  // read of whatever the Dispatch Board already has for the chosen date; the picker just lets a
  // driver look at a different day (e.g. tomorrow's plan once dispatch has finalized it).
  const [selectedDate, setSelectedDate] = React.useState(todayIso())
  const [today, setToday] = React.useState<DriverToday | null>(null)
  const [log, setLog] = React.useState<DutyLogEntry[]>([])
  const [inspectionOk, setInspectionOk] = React.useState<boolean | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [accepting, setAccepting] = React.useState<string | null>(null)
  const [loggingStatus, setLoggingStatus] = React.useState(false)
  const isToday = selectedDate === todayIso()

  const refresh = React.useCallback(async () => {
    try {
      const [t, l, insp] = await Promise.all([driverToday(selectedDate), dutyLog(), lastInspection()])
      setToday(t)
      setLog(l.entries)
      const withinDay = insp.submitted_at ? Date.now() - new Date(insp.submitted_at).getTime() < 24 * 3600 * 1000 : false
      setInspectionOk(Boolean(insp.overall_pass && withinDay))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load your trips')
    }
  }, [selectedDate])

  React.useEffect(() => {
    setToday(null)
    refresh()
    // Duty status is a real-time, "right now" concept -- only poll it (and re-check trips) while
    // looking at today; no reason to keep polling while browsing a future day's plan.
    if (!isToday) return
    const id = setInterval(refresh, 30000)
    return () => clearInterval(id)
  }, [refresh, isToday])

  const currentStatus = log[0]?.status ?? null

  async function handleAccept(orderId: string) {
    setAccepting(orderId)
    try {
      await acceptOrder(orderId)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not accept this load')
    } finally {
      setAccepting(null)
    }
  }

  async function handleDutyStatus(status: DutyStatus) {
    setLoggingStatus(true)
    try {
      await logDutyStatus(status)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not log duty status')
    } finally {
      setLoggingStatus(false)
    }
  }

  if (error) return <p className="p-6 text-sm text-status-red-500">{error}</p>
  if (!today) return <div className="p-6 text-sm text-ink-400">Loading your trips…</div>

  return (
    <div className="flex flex-col gap-4 p-4 sm:p-6">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-lg font-bold text-ink-900">Driver Assist</h1>
          <p className="text-sm text-ink-500">{format(new Date(today.service_date), 'EEEE, MMM d')} {today.truck_number && <>· Truck {today.truck_number}</>}</p>
        </div>
        <input
          type="date" value={selectedDate} onChange={(e) => e.target.value && setSelectedDate(e.target.value)}
          className="rounded-md border border-ink-200 px-2 py-1.5 text-sm focus:border-brand-500 focus:outline-none"
        />
      </div>

      {isToday && inspectionOk === false && (
        <Link
          to="/driver/inspection"
          className="flex items-center gap-2 rounded-lg border border-status-amber-500/30 bg-status-amber-100 px-4 py-3 text-sm text-status-amber-500"
        >
          <AlertTriangle className="size-4 shrink-0" />
          No passing pre-trip inspection on file in the last 24h — submit one before your first trip.
        </Link>
      )}

      {isToday && (
      <Card>
        <CardContent className="pt-6">
          <h2 className="mb-3 font-display text-sm font-semibold text-ink-900">Duty status</h2>
          <div className="grid grid-cols-2 gap-2">
            {DUTY_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                disabled={loggingStatus}
                onClick={() => handleDutyStatus(opt.value)}
                className={`rounded-lg border px-3 py-2.5 text-left text-sm font-medium transition-colors disabled:opacity-50 ${
                  currentStatus === opt.value ? 'border-brand-500 bg-brand-50 text-brand-700' : 'border-ink-200 hover:bg-ink-50'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          {log[0] && (
            <p className="mt-2 text-xs text-ink-400">
              Since {format(new Date(log[0].logged_at), 'h:mm a')}
            </p>
          )}
        </CardContent>
      </Card>
      )}

      <div>
        <h2 className="mb-2 font-display text-sm font-semibold text-ink-900">
          {isToday ? 'Today' : format(new Date(today.service_date), 'MMM d')}'s trips ({today.trips.length})
        </h2>
        {today.trips.length === 0 ? (
          <Card><CardContent className="py-8 text-center text-sm text-ink-400">No trips assigned for today yet.</CardContent></Card>
        ) : (
          <div className="flex flex-col gap-3">
            {today.trips.map((t, i) => (
              <Card key={t.order_id}>
                <CardContent className="flex flex-col gap-2 pt-6">
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-sm font-semibold text-ink-900">
                      <Package className="size-4" /> Trip {i + 1}
                    </span>
                    {t.accepted_at ? (
                      <Badge tone="green"><CheckCircle2 className="mr-1 inline size-3" />Accepted</Badge>
                    ) : (
                      <Badge tone="amber">Pending</Badge>
                    )}
                  </div>
                  <div className="text-sm text-ink-700">{t.pickup_label} → {t.dest_label}</div>
                  <div className="text-xs text-ink-500">
                    Pickup {format(new Date(t.pickup_at), 'h:mm a')}
                    {t.delivery_eta && <> · ETA {format(new Date(t.delivery_eta), 'h:mm a')}</>}
                  </div>
                  <div className="text-xs text-ink-400">{t.weight_lbs.toLocaleString()} lbs · {t.pallets} plt · {t.load_type}</div>
                  {!t.accepted_at && (
                    <Button size="sm" onClick={() => handleAccept(t.order_id)} disabled={accepting === t.order_id}>
                      {accepting === t.order_id ? 'Accepting…' : 'Accept load'}
                    </Button>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
