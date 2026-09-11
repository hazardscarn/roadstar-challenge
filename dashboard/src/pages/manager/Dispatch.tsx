import { format } from 'date-fns'
import { Loader2 } from 'lucide-react'
import * as React from 'react'
import { useSearchParams } from 'react-router-dom'
import { CandidateDetail } from '@/components/candidate-detail'
import { LocationPicker } from '@/components/location-picker'
import { ManageOrderPanel } from '@/components/manage-order-panel'
import { PageHeader } from '@/components/page-header'
import { QuoteCandidateCard } from '@/components/quote-candidate-card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { api, type FleetDriver, type LocationOption, type ScoreQuoteResult, type ScoreQuoteRow } from '@/lib/api'

const LOAD_TYPES = ['Dry Van', 'Reefer', 'Flatbed']
// Real user feedback: the New Quote form had no way to book an LTL order at all -- every live
// quote silently defaulted to FTL server-side (live.quote_requests.service_type default), so
// LTL only ever showed up in the simulation's auto-generated order book, never in a real
// manager-entered quote. FTL = straight to destination, no stops; LTL = consolidated with a
// real secondary pickup (see sim/engine/run_sim.py's STOPOFF handling) and a rate premium.
const SERVICE_TYPES = ['FTL', 'LTL'] as const
const FLEET_POLL_MS = 5000

function defaultPickupLocal() {
  const d = new Date(Date.now() + 24 * 3600 * 1000)
  return format(d, "yyyy-MM-dd'T'HH:mm")
}

export default function Dispatch() {
  // Real user feedback: the Orders page had no way to actually EDIT an order -- Manage Order
  // needs a quote/trip ID typed in by hand, but Orders never showed one. Orders' "Edit" action
  // links here as `?manage=<quote_id>`, so the tab switches and the lookup fires automatically
  // instead of making the manager retype an ID they just saw on screen.
  const [searchParams, setSearchParams] = useSearchParams()
  const manageId = searchParams.get('manage')
  const [tab, setTab] = React.useState<'new' | 'manage'>(manageId ? 'manage' : 'new')

  React.useEffect(() => {
    if (manageId) setTab('manage')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manageId])
  const [drivers, setDrivers] = React.useState<FleetDriver[]>([])
  const [origin, setOrigin] = React.useState<LocationOption | null>(null)
  const [dest, setDest] = React.useState<LocationOption | null>(null)
  const [weight, setWeight] = React.useState('18000')
  const [pallets, setPallets] = React.useState('12')
  const [loadType, setLoadType] = React.useState('Dry Van')
  const [serviceType, setServiceType] = React.useState<(typeof SERVICE_TYPES)[number]>('FTL')
  const [pickupAt, setPickupAt] = React.useState(defaultPickupLocal())
  const [scoring, setScoring] = React.useState(false)
  const [result, setResult] = React.useState<ScoreQuoteResult | null>(null)
  const [quoteOrigin, setQuoteOrigin] = React.useState<LocationOption | null>(null)
  const [quoteDest, setQuoteDest] = React.useState<LocationOption | null>(null)
  const [activeCandidate, setActiveCandidate] = React.useState<ScoreQuoteRow | null>(null)
  const [assigningDriverId, setAssigningDriverId] = React.useState<number | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    const refresh = () => api.fleet().then(setDrivers).catch(() => {})
    refresh()
    const id = setInterval(refresh, FLEET_POLL_MS)
    return () => clearInterval(id)
  }, [])

  async function handleScoreQuote(e: React.FormEvent) {
    e.preventDefault()
    if (!origin || !dest) {
      setError('Pick an origin and destination')
      return
    }
    setError(null)
    setScoring(true)
    setResult(null)
    setActiveCandidate(null)
    try {
      const res = await api.scoreQuote({
        origin_location_id: origin.location_id,
        dest_location_id: dest.location_id,
        weight_lbs: Number(weight),
        pallets: Number(pallets),
        load_type: loadType,
        requested_pickup_at: new Date(pickupAt).toISOString(),
        service_type: serviceType,
      })
      setResult(res)
      setQuoteOrigin(origin)
      setQuoteDest(dest)
      if (res.top_n[0]) setActiveCandidate(res.top_n[0])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Scoring failed')
    } finally {
      setScoring(false)
    }
  }

  async function handleAssign(driverId: number) {
    if (!result) return
    setAssigningDriverId(driverId)
    try {
      await api.assign(result.quote_id, driverId)
      setResult(null)
      setActiveCandidate(null)
      setOrigin(null)
      setDest(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Assign failed')
    } finally {
      setAssigningDriverId(null)
    }
  }

  return (
    <div className="flex h-screen flex-col">
      <PageHeader
        title="Dispatch"
        description="Enter a quote, see ranked candidates, inspect the plan, assign."
        actions={
          <div className="flex rounded-lg bg-ink-100 p-1">
            {(['new', 'manage'] as const).map((t) => (
              <button
                key={t}
                onClick={() => {
                  setTab(t)
                  if (t === 'new' && manageId) setSearchParams({}, { replace: true })
                }}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === t ? 'bg-white text-ink-900 shadow-sm' : 'text-ink-500'}`}
              >
                {t === 'new' ? 'New Quote' : 'Manage Order'}
              </button>
            ))}
          </div>
        }
      />
      {tab === 'manage' ? (
        <ManageOrderPanel drivers={drivers} initialId={manageId ?? undefined} />
      ) : (
      <div className="flex flex-1 overflow-hidden">
        <div className="flex w-[420px] shrink-0 flex-col overflow-auto border-r border-ink-200 bg-white p-4">
          <form onSubmit={handleScoreQuote} className="flex flex-col gap-3">
            <LocationPicker label="Origin" value={origin} onChange={setOrigin} />
            <LocationPicker label="Destination" value={dest} onChange={setDest} />
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1.5">
                <Label>Weight (lbs)</Label>
                <Input type="number" value={weight} onChange={(e) => setWeight(e.target.value)} />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Pallets</Label>
                <Input type="number" value={pallets} onChange={(e) => setPallets(e.target.value)} />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1.5">
                <Label>Load type</Label>
                <Select value={loadType} onValueChange={setLoadType}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {LOAD_TYPES.map((lt) => (
                      <SelectItem key={lt} value={lt}>{lt}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label>Service type</Label>
                <Select value={serviceType} onValueChange={(v) => setServiceType(v as (typeof SERVICE_TYPES)[number])}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="FTL">FTL</SelectItem>
                    <SelectItem value="LTL">LTL</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <p className="-mt-1.5 text-[11px] text-ink-400">
              {serviceType === 'LTL'
                ? 'LTL — consolidated with a real second pickup, priced with an LTL rate premium.'
                : 'FTL — one dedicated truck, direct to destination, no intermediate stops.'}
            </p>
            <div className="flex flex-col gap-1.5">
              <Label>Requested pickup</Label>
              <Input type="datetime-local" value={pickupAt} onChange={(e) => setPickupAt(e.target.value)} />
            </div>
            {error && <p className="text-xs text-status-red-500">{error}</p>}
            <Button type="submit" disabled={scoring} className="mt-1">
              {scoring ? <Loader2 className="size-4 animate-spin" /> : 'Get ranked candidates'}
            </Button>
          </form>

          {result && (
            <div className="mt-4 flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-1.5 text-xs text-ink-500">
                <Badge tone={serviceType === 'LTL' ? 'amber' : 'blue'}>{serviceType}</Badge>
                <span>{result.summary.n_feasible} feasible</span>
                <span>·</span>
                <span>{result.summary.n_mid_route_candidates} mid-route competing</span>
                {result.summary.n_excluded_for_inspection > 0 && (
                  <Badge tone="amber">{result.summary.n_excluded_for_inspection} excluded — no passing inspection</Badge>
                )}
              </div>
              {result.summary.estimated_total_charge != null && (
                <div className="rounded-lg border border-ink-200 bg-ink-50 p-3">
                  <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-ink-400">
                    Customer quote — {result.summary.loaded_miles} mi · ${result.summary.rate_per_mile}/mi
                  </p>
                  <div className="grid grid-cols-3 gap-2 text-xs">
                    <div>
                      <div className="text-ink-400">Linehaul</div>
                      <div className="font-semibold text-ink-800">${result.summary.linehaul_amount?.toFixed(2)}</div>
                    </div>
                    <div>
                      <div className="text-ink-400">Fuel surcharge</div>
                      <div className="font-semibold text-ink-800">${result.summary.fuel_surcharge_amount?.toFixed(2)}</div>
                    </div>
                    <div>
                      <div className="text-ink-400">Est. total (pre-tax)</div>
                      <div className="font-semibold text-brand-600">${result.summary.estimated_total_charge?.toFixed(2)}</div>
                    </div>
                  </div>
                  <p className="mt-1.5 text-[11px] text-ink-400">
                    Same price no matter which driver takes it — click a candidate to see their full plan on the map.
                    Detention (if any) and HST are added at invoicing.
                  </p>
                </div>
              )}
              {result.top_n.map((row) => (
                <QuoteCandidateCard
                  key={row.driver_id}
                  row={row}
                  requestedPickupAt={result.summary.requested_pickup_at}
                  assigning={assigningDriverId !== null}
                  isAssigningThis={assigningDriverId === row.driver_id}
                  onAssign={() => handleAssign(row.driver_id)}
                  selected={activeCandidate?.driver_id === row.driver_id}
                  onSelect={() => setActiveCandidate(row)}
                />
              ))}
            </div>
          )}
        </div>

        <div className="flex-1 overflow-auto p-4">
          {!activeCandidate || !quoteOrigin || !quoteDest ? (
            <div className="flex h-full items-center justify-center text-sm text-ink-400">
              Score a quote, then click a candidate to see their plan here.
            </div>
          ) : (
            <CandidateDetail
              candidate={activeCandidate}
              origin={quoteOrigin}
              dest={quoteDest}
              driver={drivers.find((d) => d.driver_id === activeCandidate.driver_id)}
            />
          )}
        </div>
      </div>
      )}
    </div>
  )
}
