import { CheckCircle2, Loader2, XCircle } from 'lucide-react'
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/lib/auth-context'
import { supabase } from '@/lib/supabase'

// The real live.vehicle_inspections schema (sim/sql/006 -- brakes/tires/lights/fluids/coupling/
// trailer + defects notes + overall_pass generated column). Verified against FMCSA 49 CFR
// Sec.396.11's real minimum inspection categories: the federal list is actually 11 items
// (service brakes, PARKING brake, STEERING, lighting, tires, HORN, WIPERS, MIRRORS, coupling,
// WHEELS/RIMS, emergency equipment) -- this schema's 6 categories are a condensed subset, not
// the full list. Flagged here explicitly rather than silently claiming full federal compliance --
// exactly the "don't invent scope, but don't hide a real gap either" instruction this project
// has followed throughout. Changing the schema itself is a real scope decision for the user, not
// made unilaterally here.
const CHECKS: { key: keyof FormState; label: string }[] = [
  { key: 'brakes_ok', label: 'Brakes' },
  { key: 'tires_ok', label: 'Tires' },
  { key: 'lights_ok', label: 'Lights & reflectors' },
  { key: 'fluid_levels_ok', label: 'Fluid levels' },
  { key: 'coupling_ok', label: 'Coupling device' },
  { key: 'trailer_ok', label: 'Trailer condition' },
]

interface FormState {
  brakes_ok: boolean
  tires_ok: boolean
  lights_ok: boolean
  fluid_levels_ok: boolean
  coupling_ok: boolean
  trailer_ok: boolean
}

export default function Inspection() {
  const { profile } = useAuth()
  const [form, setForm] = React.useState<FormState>({
    brakes_ok: true, tires_ok: true, lights_ok: true, fluid_levels_ok: true, coupling_ok: true, trailer_ok: true,
  })
  const [odometer, setOdometer] = React.useState('')
  const [defects, setDefects] = React.useState('')
  const [truckNumber, setTruckNumber] = React.useState<string | null>(null)
  const [submitting, setSubmitting] = React.useState(false)
  const [result, setResult] = React.useState<{ pass: boolean } | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (!profile?.driver_id) return
    supabase.schema('live').from('driver_status').select('truck_number').eq('driver_id', profile.driver_id).maybeSingle()
      .then(({ data }) => setTruckNumber((data?.truck_number as string) ?? null))
  }, [profile?.driver_id])

  const overallPass = Object.values(form).every(Boolean)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!profile?.driver_id) return
    setSubmitting(true)
    setError(null)
    const { error: insertError } = await supabase.schema('live').from('vehicle_inspections').insert({
      driver_id: profile.driver_id,
      truck_number: truckNumber,
      odometer_km: odometer ? Number(odometer) : null,
      defects_noted: defects || null,
      ...form,
    })
    setSubmitting(false)
    if (insertError) {
      setError(insertError.message)
      return
    }
    setResult({ pass: overallPass })
  }

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-4 p-4 sm:p-6">
      <div>
        <h1 className="font-display text-lg font-bold text-ink-900">Pre-Trip Inspection</h1>
        <p className="text-sm text-ink-500">Truck {truckNumber ?? '—'} — required before going available.</p>
      </div>

      {result ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-8 text-center">
            {result.pass ? (
              <>
                <CheckCircle2 className="size-10 text-status-green-500" />
                <p className="font-medium text-ink-800">Inspection passed — you're clear to go available.</p>
              </>
            ) : (
              <>
                <XCircle className="size-10 text-status-red-500" />
                <p className="font-medium text-ink-800">Inspection failed — this truck is NOT clear to dispatch.</p>
                <p className="text-xs text-ink-500">Report the defect(s) to your fleet manager before continuing.</p>
              </>
            )}
            <Button size="sm" variant="outline" onClick={() => setResult(null)}>Submit another</Button>
          </CardContent>
        </Card>
      ) : (
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <Card>
            <CardContent className="flex flex-col gap-3 pt-6">
              {CHECKS.map((c) => (
                <div key={c.key} className="flex items-center justify-between">
                  <Label htmlFor={c.key}>{c.label}</Label>
                  <div className="flex items-center gap-2">
                    <span className={form[c.key] ? 'text-xs text-status-green-500' : 'text-xs text-status-red-500'}>
                      {form[c.key] ? 'OK' : 'Defect'}
                    </span>
                    <Switch id={c.key} checked={form[c.key]} onCheckedChange={(v) => setForm((f) => ({ ...f, [c.key]: v }))} />
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>

          <div className="flex flex-col gap-1.5">
            <Label>Odometer (km)</Label>
            <Input type="number" value={odometer} onChange={(e) => setOdometer(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Defects noted (if any)</Label>
            <textarea
              value={defects}
              onChange={(e) => setDefects(e.target.value)}
              className="min-h-20 rounded-lg border border-ink-200 px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-brand-400"
            />
          </div>

          <div className="flex items-center gap-2">
            <span className="text-sm text-ink-600">Overall:</span>
            <Badge tone={overallPass ? 'green' : 'red'}>{overallPass ? 'Pass' : 'Fail'}</Badge>
          </div>

          {error && <p className="text-xs text-status-red-500">{error}</p>}
          <Button type="submit" disabled={submitting}>
            {submitting ? <Loader2 className="size-4 animate-spin" /> : 'Submit inspection'}
          </Button>
        </form>
      )}
    </div>
  )
}
