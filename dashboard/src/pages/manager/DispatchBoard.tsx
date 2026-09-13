import { format } from 'date-fns'
import { AlertTriangle, Loader2, MapPin, Package, PenLine, RotateCcw, Shuffle, Sparkles, Truck, Undo2, Users, X } from 'lucide-react'
import * as React from 'react'
import { GeofenceMap, type GeofenceStop } from '@/components/geofence-map'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { api, type DispatchAssignmentRow, type DispatchBoardData, type DispatchOrder, type DispatchTruck, type TripDetail } from '@/lib/api'

// Real hackathon-lead clarification (see the plan): a fleet manager assigns TOMORROW's work
// TODAY, by hand -- trucks/trailers matched to orders by equipment type + capacity, drivers to
// trucks by home hub. Replaces the old single-quote scoring flow entirely (no "New Quote"/
// "Manage Order" here anymore -- see dashboard/src/pages/manager/Dispatch.tsx, left in place,
// unrouted, as the future "AI Assign" button's integration point). Ported from the Claude Design
// mock (`Dispatch Board.dc.html`, project b924929f-fb99-4404-a488-cb1c97c310b1) onto our real
// data -- see this build's plan file for the full mapping.

const TRUCK_TYPES = ['Dry Van', 'Reefer', 'Flatbed'] as const
type TypeFilter = 'All' | (typeof TRUCK_TYPES)[number]

const CAPACITY_DANGER_PCT = 88
const CAPACITY_WARN_PCT = 68
const HOS_DANGER_HOURS = 2
const HOS_WARN_HOURS = 4

function formatLbs(lbs: number) {
  return `${Math.round(lbs).toLocaleString()} lbs`
}

function pickupLabel(order: DispatchOrder) {
  return `Pickup ${format(new Date(order.pickup_at), 'HH:mm')}`
}

function etaLabel(order: DispatchOrder) {
  // Real OSRM-derived delivery ETA (sim/live/generate_dispatch_day.py's use of
  // sim/engine/run_sim.py's get_route()), not a guessed figure.
  return `ETA ${format(new Date(order.delivery_eta), 'HH:mm')}`
}

function capacityColor(pct: number) {
  if (pct >= CAPACITY_DANGER_PCT) return 'text-status-red-500'
  if (pct >= CAPACITY_WARN_PCT) return 'text-status-amber-500'
  return 'text-brand-600'
}

function hosColor(remaining: number) {
  if (remaining <= HOS_DANGER_HOURS) return 'text-status-red-500'
  if (remaining <= HOS_WARN_HOURS) return 'text-status-amber-500'
  return 'text-status-green-500'
}

type DragPayload = { type: 'order'; id: string } | { type: 'driver'; id: number }

const HUBS = ['Milton', 'London', 'Barrie'] as const

// Real user decision (sim/config.py's LOAD_TYPE_SHARES), superseding the real historical mix
// (~85% Dry Van/10% Flatbed/4% Reefer) that used to be the default here: "Im keeping 70% dry van
// 25 as reefer and 5% as flatbed in truck sim." Same fixed split calibrate_truck_profile.py now
// builds the truck population from and generate_dispatch_day.py now draws order load_type from --
// still just an editable starting point in this panel, not a hard-coded floor.
const DEFAULT_TYPE_SHARES: Record<string, number> = { 'Dry Van': 70, Reefer: 25, Flatbed: 5 }
// Real, previously-fixed default (Milton 55% / London 30% / Barrie 15% of a 20-truck fleet,
// sim/hub_weights.py) -- still the panel's starting point, just editable now.
const DEFAULT_HUB_COUNTS: Record<string, number> = { Milton: 11, London: 6, Barrie: 3 }
const DEFAULT_NUM_ORDERS = 40

// Reconfiguring an already-simulated day pre-fills the panel from what's ACTUALLY on the board
// right now, not the app defaults -- editing feels like a real adjustment, not starting over blind.
function deriveSetupDefaults(board: DispatchBoardData) {
  const hubCounts: Record<string, number> = { Milton: 0, London: 0, Barrie: 0 }
  const typeCounts: Record<string, number> = { 'Dry Van': 0, Reefer: 0, Flatbed: 0 }
  for (const t of board.trucks) {
    hubCounts[t.hub] = (hubCounts[t.hub] ?? 0) + 1
    typeCounts[t.truck_type] = (typeCounts[t.truck_type] ?? 0) + 1
  }
  const total = board.trucks.length || 1
  const typeShares: Record<string, number> = {}
  for (const [type, n] of Object.entries(typeCounts)) typeShares[type] = Math.round((n / total) * 100)
  return { hub_counts: hubCounts, type_shares: typeShares, num_orders: board.orders.length }
}

// Real user ask: "AI Assign" should feel like it's actually thinking, not a frozen spinner --
// same spirit as Claude Code's own rotating status text. Purely cosmetic (the backend does one
// blocking CP-SAT solve, no real step-by-step progress to report), cycled client-side while the
// request is in flight -- each line names a real part of what sim/dispatch_solver.py actually does.
// Real user ask: the solve now runs up to 60s (sim/dispatch_solver.py's SOLVER_TIME_LIMIT_SECONDS),
// but this list only had 8 lines at a fast 1.5s cadence -- visibly looped 3-4 times over on a real
// solve, which read as broken/stuck rather than "still working." Expanded to a real, sequential
// walk through what the solver ACTUALLY does (matches its own module docstring step for step, not
// invented flavor text), paired with a slower cadence below so one full pass roughly spans a solve
// instead of lapping it.
const AI_STATUS_MESSAGES = [
  "Reading tomorrow's order book...",
  'Loading today\'s truck roster and driver Hours-of-Service clocks...',
  'Filtering out equipment-type mismatches (Dry Van vs Reefer vs Flatbed)...',
  'Matching truck capacity to freight weight & pallets...',
  'Computing real drive times from every hub to every pickup (OSRM routing)...',
  'Checking which orders could realistically chain onto the same truck...',
  'Ruling out chains that would arrive late for a real pickup appointment...',
  'Building the constraint model (Google OR-Tools CP-SAT)...',
  'Weighing thousands of truck x driver x order combinations...',
  'Solving the assignment problem...',
  'Chaining multi-stop routes to cut deadhead miles...',
  'Scoring options by real linehaul revenue minus deadhead cost...',
  'Applying the distance-tiered rate card by truck type...',
  'Re-checking Hours-of-Service limits on every candidate driver...',
  'Minimizing empty miles across the whole fleet...',
  'Breaking ties by driver rest margin, not just cost...',
  "Making sure no order gets dropped without a hard equipment, capacity, or timing reason...",
  'Re-checking every chain for physically realistic pickup and dropoff times...',
  'Converging on a high-value, feasible assignment...',
  'Almost there -- finalizing the plan...',
]

// Real user ask: "can I set the date on the dispatch... so I can have the whole data and
// simulations run across different days" -- the board used to always resolve to the server's own
// notion of "tomorrow" (dashboard/server/main.py's _dispatch_date("tomorrow")), with no way to
// point it at a different date at all. Defaults to tomorrow (unchanged default behavior) but is
// now a real, editable date.
function tomorrowIso(): string {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  return d.toISOString().slice(0, 10)
}

export default function DispatchBoard() {
  const [selectedDate, setSelectedDate] = React.useState<string>(tomorrowIso())
  const [board, setBoard] = React.useState<DispatchBoardData | null>(null)
  const [loading, setLoading] = React.useState(true)
  const [error, setError] = React.useState<string | null>(null)
  // Real user ask: no more silent auto-generation of a default-sized fleet/order book the first
  // time a date is opened -- the manager picks fleet size/hub mix/type mix/order count up front
  // via the Setup panel (see DispatchSetupPanel below) and clicks Simulate. `needsSetup` is true
  // when the backend has no dispatch.days row yet for this date at all (a 404, not an error).
  const [needsSetup, setNeedsSetup] = React.useState(false)
  const [reconfiguring, setReconfiguring] = React.useState(false)
  const [toast, setToast] = React.useState<string | null>(null)
  const [dragOverTruck, setDragOverTruck] = React.useState<string | null>(null)
  const [selectedTruck, setSelectedTruck] = React.useState<string | null>(null)
  const [showUnavailableDrivers, setShowUnavailableDrivers] = React.useState(false)
  const [showUnavailableTrucks, setShowUnavailableTrucks] = React.useState(false)
  // Real user ask: a type filter makes drag-and-drop easier -- narrows both Trucks & Trailers and
  // the Order Book to one equipment type at a time, so a manager isn't scanning past Reefer/
  // Flatbed cards while placing a run of Dry Van orders. Drivers aren't typed, so this doesn't
  // touch that column.
  const [typeFilter, setTypeFilter] = React.useState<TypeFilter>('All')
  const [itineraryOpen, setItineraryOpen] = React.useState(false)
  const [selectedTripId, setSelectedTripId] = React.useState<string | null>(null)
  const [aiRunning, setAiRunning] = React.useState(false)
  const [aiStatusIndex, setAiStatusIndex] = React.useState(0)
  const toastTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null)

  React.useEffect(() => {
    if (!aiRunning) return
    setAiStatusIndex(0)
    // 20 messages x 2.8s ~= 56s -- one full pass roughly spans the solver's own 60s budget
    // (sim/dispatch_solver.py's SOLVER_TIME_LIMIT_SECONDS). Real user ask: this used to WRAP back
    // to index 0 ("Reading tomorrow's order book...") if the solve ran past the last message --
    // looked broken/backwards to land on "finalizing the plan" and then see it revert to the very
    // first step. Caps at the last message and holds there instead of cycling.
    const interval = setInterval(() => {
      setAiStatusIndex((i) => Math.min(i + 1, AI_STATUS_MESSAGES.length - 1))
    }, 2800)
    return () => clearInterval(interval)
  }, [aiRunning])

  const load = React.useCallback(async () => {
    try {
      const data = await api.dispatchBoardIfExists(selectedDate)
      if (data === null) {
        setNeedsSetup(true)
        setBoard(null)
      } else {
        setBoard(data)
        setNeedsSetup(false)
      }
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load dispatch board')
    } finally {
      setLoading(false)
    }
  }, [selectedDate])

  // Real user ask: switching dates should show a real loading state for the NEW date, not a
  // flash of the previous date's board while the new one fetches.
  React.useEffect(() => {
    setLoading(true)
    setBoard(null)
    setNeedsSetup(false)
    setReconfiguring(false)
    void load()
  }, [load])

  function showToast(msg: string) {
    if (toastTimer.current) clearTimeout(toastTimer.current)
    setToast(msg)
    toastTimer.current = setTimeout(() => setToast(null), 3800)
  }

  // Merges just the truck(s) a mutation actually changed into local state, instead of a full
  // load() refetch -- real user feedback: refetching the whole board (several sequential queries
  // against a remote Supabase instance) after every single drop made each drag feel like a ~2s
  // delay. The backend now returns exactly the changed row(s) (sim/live/dispatch_board.py), so a
  // drop only ever costs one small write + a same-response merge, no second round trip.
  function mergeAssignments(rows: DispatchAssignmentRow[]) {
    setBoard((prev) => {
      if (!prev) return prev
      const next = { ...prev.assignments }
      for (const row of rows) next[row.truck_number] = { driver_id: row.driver_id, order_ids: row.order_ids }
      return { ...prev, assignments: next }
    })
  }

  async function applyMutation(fn: () => Promise<{ assignments: DispatchAssignmentRow[] }>) {
    try {
      mergeAssignments((await fn()).assignments)
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Something went wrong')
    }
  }

  // Real bug found directly: this used to just flip `board.status` locally after Finalize/Edit
  // Dispatch succeeded, leaving every OTHER server-computed field (trip_id most importantly) as
  // whatever it was on the LAST fetch -- which, right after Finalize, is all null, since finalize()
  // is what creates the real live.trips rows in the first place. That's exactly why "Edit Geofence"
  // did nothing the first time: FinalizedView's row click (and the button below) both gate on
  // `o.trip_id`, which was still stale-null until a full page refresh re-fetched the board. Fix:
  // refetch for real after the mutation succeeds, instead of hand-patching one field.
  async function setStatus(fn: () => Promise<unknown>) {
    try {
      await fn()
      await load()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Something went wrong')
    }
  }

  // Real user ask: an "AI Assign" option that runs the CP-SAT solver over the WHOLE day at once
  // and leaves the board looking exactly as if a manager had dragged every match by hand -- same
  // draft state, still editable, Finalize Dispatch works unchanged afterward.
  async function handleAiAssign() {
    setAiRunning(true)
    try {
      const result = await api.dispatchAiAssign(date)
      setBoard((prev) => (prev ? { ...prev, assignments: result.assignments } : prev))
      const revenue = Math.round(result.total_net_revenue).toLocaleString()
      const gapNote = result.num_unassigned_orders > 0 ? `, ${result.num_unassigned_orders} still unassigned` : ' -- every order covered'
      showToast(`AI matched ${result.num_assigned_orders} order(s)${gapNote}. Projected net revenue $${revenue}, ${Math.round(result.deadhead_miles_total).toLocaleString()} deadhead mi.`)
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'AI Assign failed')
    } finally {
      setAiRunning(false)
    }
  }

  // Real user ask: a plain Reset -- clears every truck back to its initial empty state so AI
  // Assign (or manual dispatch) can be run again from scratch, without touching the order book.
  async function handleReset() {
    try {
      const result = await api.dispatchReset(date)
      setBoard((prev) => (prev ? { ...prev, assignments: result.assignments } : prev))
      showToast('Board reset -- every truck is unassigned again.')
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Reset failed')
    }
  }

  // DEMO-ONLY: swaps in a genuinely different random order book for the same day (same trucks/
  // drivers) -- real user ask, for showing the board against more than one scenario live.
  async function handleRegenerateOrders() {
    if (!window.confirm("Generate a new random order book for tomorrow? This clears the current orders and any assignments (demo only).")) return
    setLoading(true)
    try {
      const data = await api.dispatchRegenerateOrders(date)
      setBoard(data)
      showToast(`New demo order book generated -- ${data.orders.length} orders.`)
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Could not regenerate the order book')
    } finally {
      setLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center text-ink-400">
        <Loader2 className="size-5 animate-spin" />
      </div>
    )
  }
  if (error) {
    return <div className="flex h-screen items-center justify-center text-sm text-status-red-500">{error}</div>
  }
  if (needsSetup || (reconfiguring && board)) {
    return (
      <div className="flex h-screen flex-col">
        {/* Real user ask: "can I set the date on the dispatch... so I can have the whole data and
            simulations run across different days" -- visible here too, not just once a board
            already exists, since this IS the entry point for a date with no board yet. */}
        <div className="flex items-center gap-2 border-b border-ink-200 bg-white px-4 py-2.5">
          <label className="text-xs font-semibold text-ink-500">Dispatch date</label>
          <input
            type="date" value={selectedDate} onChange={(e) => e.target.value && setSelectedDate(e.target.value)}
            className="rounded-md border border-ink-200 px-2.5 py-1 text-sm focus:border-brand-500 focus:outline-none"
          />
        </div>
        <div className="flex-1 overflow-auto">
          <DispatchSetupPanel
            date={selectedDate}
            initial={board ? deriveSetupDefaults(board) : undefined}
            onCancel={board ? () => setReconfiguring(false) : undefined}
            onSimulated={(data) => {
              setBoard(data)
              setNeedsSetup(false)
              setReconfiguring(false)
              const s = data.setup
              if (s) {
                const hubBits = Object.entries(s.actual_hub_counts ?? {}).map(([hub, n]) => `${hub} ${n}`).join(', ')
                showToast(`Simulated: ${data.trucks.length} trucks/drivers (${hubBits}), ${s.actual_num_orders ?? data.orders.length} orders.`)
              }
            }}
          />
        </div>
      </div>
    )
  }
  if (!board) {
    return <div className="flex h-screen items-center justify-center text-sm text-status-red-500">No dispatch board</div>
  }

  const date = board.service_date
  const ordersById = new Map(board.orders.map((o) => [o.order_id, o]))
  const assignedOrderIds = new Set(Object.values(board.assignments).flatMap((a) => a.order_ids))
  const unassignedOrders = board.orders
    .filter((o) => !assignedOrderIds.has(o.order_id))
    .filter((o) => typeFilter === 'All' || o.load_type === typeFilter)
  const visibleTrucks = board.trucks
    .filter((t) => typeFilter === 'All' || t.truck_type === typeFilter)
    .filter((t) => showUnavailableTrucks || t.available)
  const readyTripCount = board.trucks.filter((t) => {
    const a = board.assignments[t.truck_number]
    return a?.driver_id != null && a.order_ids.length > 0
  }).length
  // Real user feedback: spare trucks/drivers beyond order volume are normal and fine -- a fleet
  // with more capacity than today's orders need isn't "incomplete." Only an unassigned ORDER
  // means dispatch actually isn't done -- every order needs a truck+driver, nothing else does.
  const hasGaps = unassignedOrders.length > 0
  const selectedTruckObj = selectedTruck ? board.trucks.find((t) => t.truck_number === selectedTruck) : null

  async function handleDropOnTruck(truck: DispatchTruck, e: React.DragEvent) {
    e.preventDefault()
    setDragOverTruck(null)
    // TS doesn't carry the outer `if (!board) return` narrowing into this nested function
    // (it's called later, from a drop event) -- re-assert it locally.
    if (!board) return
    if (!truck.available) {
      showToast(`Truck ${truck.truck_number} is unavailable today (${truck.unavailable_reason}).`)
      return
    }
    let payload: DragPayload
    try {
      payload = JSON.parse(e.dataTransfer.getData('application/json'))
    } catch {
      return
    }
    if (payload.type === 'order') {
      const order = ordersById.get(payload.id)
      if (!order) return
      // Client-side type-match check for instant feedback -- the mock only checked capacity;
      // real user ask: "truck types can be matched on drag and drop i.e. dry van order to dry
      // van and so." Capacity is still re-checked server-side too (sim/live/dispatch_board.py).
      if (order.load_type !== truck.truck_type) {
        showToast(`Truck ${truck.truck_number} is ${truck.truck_type}, this order is ${order.load_type} -- equipment types must match.`)
        return
      }
      await applyMutation(() => api.dispatchAssignOrder(date, truck.truck_number, order.order_id))
    } else {
      const driver = board.drivers.find((d) => d.driver_id === payload.id)
      if (!driver) return
      if (!driver.available) {
        showToast(`Driver ${driver.driver_id} is off today (${driver.unavailable_reason}).`)
        return
      }
      if (driver.hub !== truck.hub) {
        showToast(`Hub mismatch -- Driver ${driver.driver_id} is based in ${driver.hub}, Truck ${truck.truck_number} is based in ${truck.hub}.`)
        return
      }
      await applyMutation(() => api.dispatchAssignDriver(date, truck.truck_number, driver.driver_id))
    }
  }

  return (
    <div className="flex h-screen flex-col">
      {aiRunning && (
        <div className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-5 bg-ink-900/70 backdrop-blur-sm">
          <div className="flex items-center gap-4 rounded-2xl bg-white px-8 py-6 shadow-xl">
            <Sparkles className="size-7 animate-pulse text-brand-600" />
            <div>
              <div className="text-[16px] font-bold text-ink-900">Intelligent Optimal Dispatch in progress</div>
              <div key={aiStatusIndex} className="mt-1.5 animate-in fade-in-0 text-sm text-ink-500 duration-300">
                {AI_STATUS_MESSAGES[aiStatusIndex]}
              </div>
            </div>
            <Loader2 className="size-5 animate-spin text-ink-300" />
          </div>
        </div>
      )}
      <PageHeader
        title="Dispatch"
        description={`${board.trucks.length} trucks · ${readyTripCount} trip(s) ready`}
        actions={
          <div className="flex items-center gap-4">
            {/* Real user ask: "can I set the date on the dispatch... so I can have the whole data
                and simulations run across different days" -- was a fixed "(tomorrow)" label with
                no way to change it at all. */}
            <div className="flex items-center gap-1.5">
              <input
                type="date" value={selectedDate} onChange={(e) => e.target.value && setSelectedDate(e.target.value)}
                className="rounded-md border border-ink-200 px-2 py-1 text-sm focus:border-brand-500 focus:outline-none"
              />
              <span className="text-sm text-ink-500">
                {format(new Date(`${date}T00:00:00`), 'EEEE, MMM d')}{date === tomorrowIso() ? ' (tomorrow)' : ''}
              </span>
            </div>
            <Button variant="outline" onClick={() => setItineraryOpen(true)}>
              <MapPin className="size-4" /> View Itinerary
            </Button>
            <div className="flex items-center gap-1.5 border-l border-ink-200 pl-4">
              <Button
                variant="outline"
                onClick={() => setReconfiguring(true)}
                title="Pick a different fleet size, hub mix, truck-type mix, or order count for this day"
                className="text-ink-500 hover:bg-ink-100"
              >
                <Users className="size-4" /> Reconfigure Fleet
              </Button>
              <Button
                variant="outline"
                onClick={() => void handleRegenerateOrders()}
                title="Demo only -- generates a new random order book for this day"
                className="text-ink-500 hover:bg-ink-100"
              >
                <Shuffle className="size-4" /> New Order Book
              </Button>
              <Badge tone="gray">Demo only</Badge>
            </div>
          </div>
        }
      />

      {board.status === 'draft' && (
        <div className="flex items-center gap-2 border-b border-ink-200 bg-white px-6 py-2.5">
          <span className="text-xs font-semibold tracking-wide text-ink-400 uppercase">Filter</span>
          {(['All', ...TRUCK_TYPES] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTypeFilter(t)}
              className={`rounded-full px-3 py-1 text-[13px] font-medium transition-colors ${
                typeFilter === t ? 'bg-brand-600 text-white' : 'bg-ink-100 text-ink-600 hover:bg-ink-200'
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      )}

      {board.status === 'finalized' ? (
        <FinalizedView board={board} hasGaps={hasGaps} unassignedCount={unassignedOrders.length}
          onEdit={() => void setStatus(() => api.dispatchReopen(date))}
          onSelectTrip={setSelectedTripId} />
      ) : (
        <div className="grid flex-1 grid-cols-[320px_minmax(0,1fr)_336px] divide-x-2 divide-ink-400 overflow-hidden bg-ink-200">
          {/* TRUCKS */}
          <div className="flex min-h-0 flex-col bg-ink-50">
            <div className="border-b border-ink-200 bg-white px-4 pt-4 pb-3">
              <div className="flex items-center gap-2">
                <Truck className="size-4.5 text-brand-600" />
                <span className="text-[15px] font-bold">Trucks &amp; Trailers</span>
                <span className="ml-auto rounded-full border border-ink-200 bg-ink-100 px-2.5 py-0.5 text-xs font-semibold text-ink-500">
                  {visibleTrucks.length}
                </span>
              </div>
              <div className="mt-1 text-sm text-ink-500">Drop an order or driver onto a truck</div>
              <label className="mt-2.5 flex items-center gap-1.5 text-[12.5px] text-ink-500">
                <input type="checkbox" checked={showUnavailableTrucks} onChange={(e) => setShowUnavailableTrucks(e.target.checked)} className="accent-brand-600" />
                Show unavailable trucks ({board.trucks.filter((t) => !t.available).length})
              </label>
            </div>
            <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-3">
              {visibleTrucks.map((truck) => {
                const a = board.assignments[truck.truck_number] ?? { driver_id: null, order_ids: [] }
                // Real user ask: show the truck's day as an actual timeline (pickup/dropoff times,
                // in order), not just a bare list of legs -- sorted chronologically so "Stop 1/2/3"
                // means something real, not just whatever order the backend happened to return.
                const assignedOrders = a.order_ids
                  .map((id) => ordersById.get(id))
                  .filter((o): o is DispatchOrder => !!o)
                  .sort((x, y) => x.pickup_at.localeCompare(y.pickup_at))
                // Real user correction: a truck can carry several orders across its day now
                // (sequencing/chaining), but never more than ONE at a time -- each order occupies
                // it only for its own leg, dropped off before the next pickup. Summing every
                // order's weight together produced a nonsensical >100%-of-capacity figure (a real
                // user report: "a truck with 69K lbs" against a ~45K lb capacity). The truck's
                // real peak load is its single HEAVIEST order, not the sum of a whole day's trips.
                const peakWeight = assignedOrders.length ? Math.max(...assignedOrders.map((o) => o.weight_lbs)) : 0
                const pct = Math.round((peakWeight / truck.capacity_lbs) * 100)
                const driver = a.driver_id != null ? board.drivers.find((d) => d.driver_id === a.driver_id) : null
                const isDragOver = dragOverTruck === truck.truck_number
                const isSelected = selectedTruck === truck.truck_number
                return (
                  <div
                    key={truck.truck_number}
                    onClick={() => setSelectedTruck((s) => (s === truck.truck_number ? null : truck.truck_number))}
                    onDragOver={(e) => { e.preventDefault(); if (dragOverTruck !== truck.truck_number) setDragOverTruck(truck.truck_number) }}
                    onDragLeave={() => setDragOverTruck((s) => (s === truck.truck_number ? null : s))}
                    onDrop={(e) => void handleDropOnTruck(truck, e)}
                    className={`cursor-pointer rounded-xl border bg-white p-3.5 shadow-sm transition-colors ${
                      isDragOver || isSelected ? 'border-brand-600 ring-3 ring-brand-200' : 'border-ink-200'
                    } ${isDragOver ? 'bg-brand-50' : ''} ${!truck.available ? 'opacity-60' : ''}`}
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <Truck className={`size-5 ${driver ? 'text-brand-600' : 'text-ink-400'}`} />
                        <span className="text-[15px] font-bold">Truck {truck.truck_number}</span>
                      </div>
                      <span className="rounded-full border border-ink-200 bg-ink-100 px-2.5 py-0.5 text-xs font-semibold text-ink-500">
                        {truck.truck_type}
                      </span>
                    </div>
                    <div className="mt-1 flex items-center gap-1.5 text-xs text-ink-400">
                      <MapPin className="size-3" />
                      <span className="font-semibold text-brand-600">{truck.hub} hub</span>
                      <span>· {truck.length_ft}ft · {truck.inside_height_ft || '—'}ft H · {truck.width_in}in W</span>
                    </div>
                    {!truck.available && <div className="mt-1.5 text-xs font-medium text-status-red-500">Unavailable — {truck.unavailable_reason}</div>}

                    <div className="mt-3.5">
                      <div className="mb-1 flex justify-between text-[13px] text-ink-500">
                        <span>
                          {formatLbs(peakWeight)} / {formatLbs(truck.capacity_lbs)}
                          {assignedOrders.length > 1 && <span className="ml-1 text-ink-400">(peak of {assignedOrders.length} trips)</span>}
                        </span>
                        <span className={`font-bold ${capacityColor(pct)}`}>{pct}%</span>
                      </div>
                      <div className="h-2 overflow-hidden rounded-full bg-ink-100">
                        <div
                          className={`h-full rounded-full transition-[width] ${pct >= CAPACITY_DANGER_PCT ? 'bg-status-red-500' : pct >= CAPACITY_WARN_PCT ? 'bg-status-amber-500' : 'bg-brand-600'}`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </div>

                    <div className="mt-3.5 border-t border-ink-200 pt-3.5">
                      {driver ? (
                        <div className="flex items-center gap-2 rounded-lg border border-brand-200 bg-brand-50 px-2.5 py-2">
                          <Users className="size-3.5 text-brand-700" />
                          <div className="min-w-0 flex-1">
                            <div className="text-[13.5px] font-semibold">Driver #{driver.driver_id}</div>
                            <div className="text-xs text-ink-500">{driver.hos_driving_hours_remaining}h drive · {driver.hos_duty_hours_remaining}h shift left</div>
                          </div>
                          <button
                            onClick={(e) => { e.stopPropagation(); void applyMutation(() => api.dispatchUnassignDriver(date, truck.truck_number)) }}
                            aria-label="Unassign driver"
                            className="p-0.5 text-ink-400 hover:text-ink-700"
                          >
                            <X className="size-3.5" />
                          </button>
                        </div>
                      ) : (
                        <div className="flex items-center gap-1.5 rounded-lg border border-dashed border-ink-300 px-2.5 py-2 text-[13px] text-ink-400">
                          <Users className="size-3.5" /> No driver assigned — drop one here
                        </div>
                      )}
                    </div>

                    <div className="mt-2.5 flex flex-col gap-1.5">
                      {assignedOrders.map((o, i) => (
                        <div key={o.order_id} className="flex flex-col gap-1 rounded-lg border border-ink-200 bg-ink-50 px-2.5 py-1.5">
                          <div className="flex items-center gap-2">
                            <Package className="size-3.5 shrink-0 text-ink-500" />
                            <div className="min-w-0 flex-1 text-[13px]">
                              {assignedOrders.length > 1 && <span className="font-semibold text-ink-600">Trip {i + 1}: </span>}
                              {o.pickup_city} <span className="text-ink-400">→</span> {o.dest_city} <span className="text-ink-400">· {formatLbs(o.weight_lbs)}</span>
                            </div>
                            <button
                              onClick={(e) => { e.stopPropagation(); void applyMutation(() => api.dispatchUnassignOrder(date, truck.truck_number, o.order_id)) }}
                              aria-label="Unassign order"
                              className="shrink-0 p-0.5 text-ink-400 hover:text-ink-700"
                            >
                              <X className="size-3.5" />
                            </button>
                          </div>
                          {/* Real user ask: "show the timeline based info there... Pickup: Sobeys
                              Vaughan @ 11:00" -- exactly what pickup_at/delivery_eta already carry,
                              just never surfaced here (only in the Order Book's unassigned cards). */}
                          <div className="flex items-center gap-3 pl-5.5 text-[11px] text-ink-400">
                            <span>P: {o.pickup_city} @ {format(new Date(o.pickup_at), 'HH:mm')}</span>
                            <span>D: {o.dest_city} @ {format(new Date(o.delivery_eta), 'HH:mm')}</span>
                            {/* Real user ask: "how does the dispatch know the driver have
                                accepted trips" -- set by the driver's own Accept button in
                                Driver Assist, surfaced here so a manager can see it without
                                a separate page. */}
                            {o.accepted_at ? (
                              <span className="font-medium text-status-green-500">✓ Accepted {format(new Date(o.accepted_at), 'HH:mm')}</span>
                            ) : (
                              <span className="text-status-amber-500">Not yet accepted</span>
                            )}
                          </div>
                        </div>
                      ))}
                      {assignedOrders.length === 0 && (
                        <div className="rounded-lg border border-dashed border-ink-200 py-2.5 text-center text-xs text-ink-400">Drop orders here</div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>

          {/* ORDERS */}
          <div className="flex min-h-0 flex-col bg-ink-50">
            <div className="border-b border-ink-200 bg-white px-4 pt-4 pb-3">
              <div className="flex items-center gap-2">
                <Package className="size-4.5 text-brand-600" />
                <span className="text-[15px] font-bold">Order Book</span>
                <span className="ml-auto rounded-full border border-ink-200 bg-ink-100 px-2.5 py-0.5 text-xs font-semibold text-ink-500">
                  {unassignedOrders.length} unassigned
                </span>
              </div>
              <div className="mt-1 text-sm text-ink-500">Drag an order onto a truck on the left</div>
            </div>
            <div className="grid flex-1 grid-cols-[repeat(auto-fill,minmax(250px,1fr))] content-start gap-3 overflow-y-auto p-4">
              {unassignedOrders.map((order) => (
                <div
                  key={order.order_id}
                  draggable
                  onDragStart={(e) => e.dataTransfer.setData('application/json', JSON.stringify({ type: 'order', id: order.order_id }))}
                  className="flex cursor-grab flex-col gap-2 rounded-lg border border-ink-200 bg-white p-3.5 shadow-sm"
                >
                  <div className="flex items-center justify-between">
                    <span className="rounded-full border border-ink-200 bg-ink-100 px-2.5 py-0.5 text-xs font-semibold text-ink-500">{order.load_type}</span>
                    <span className="text-[13px] font-bold text-brand-600">${Math.round(order.rate).toLocaleString()}</span>
                  </div>
                  <div className="flex flex-col gap-1.5 text-sm">
                    <div className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-ink-400" /> {order.pickup_city}</div>
                    <div className="flex items-center gap-1.5"><MapPin className="size-3 text-brand-600" /> {order.dest_city}</div>
                  </div>
                  <div className="flex items-center justify-between border-t border-ink-200 pt-2 text-xs text-ink-500">
                    <span>{formatLbs(order.weight_lbs)}</span>
                    <span>{order.pallets} plt</span>
                  </div>
                  <div className="flex items-center justify-between text-xs text-ink-400">
                    <span>{pickupLabel(order)}</span>
                    <span>{etaLabel(order)}</span>
                  </div>
                  {/* Real user ask: when an order can't be matched (equipment type, capacity,
                      driver-hub reach), show WHY -- computed server-side, not a guess. */}
                  {order.unassigned_reason && (
                    <div className="flex items-start gap-1.5 rounded-md bg-status-amber-100 px-2 py-1.5 text-[11.5px] leading-snug text-[#7a5416]">
                      <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                      <span>{order.unassigned_reason}</span>
                    </div>
                  )}
                </div>
              ))}
              {unassignedOrders.length === 0 && (
                <div className="col-span-full py-10 text-center text-sm text-ink-500">All orders assigned to trucks.</div>
              )}
            </div>
            <div className="flex justify-center gap-3 border-t border-ink-200 bg-white p-3.5">
              <Button
                variant="outline"
                onClick={() => void handleAiAssign()}
                disabled={aiRunning}
                className="border-brand-300 px-5 py-2.5 text-[15px] text-brand-700 hover:bg-brand-50"
              >
                <Sparkles className="size-4" /> Dispatch with AI
              </Button>
              <Button
                variant="outline"
                onClick={() => void handleReset()}
                disabled={aiRunning}
                className="px-4 py-2.5 text-[15px] text-ink-500 hover:bg-ink-100"
                title="Unassign every truck and start over"
              >
                <RotateCcw className="size-4" /> Reset
              </Button>
              <Button
                onClick={() => void setStatus(() => api.dispatchFinalize(date))}
                className="bg-[#d97757] px-6 py-2.5 text-[15px] shadow-md hover:bg-[#c2603f]"
              >
                Finalize Dispatch
              </Button>
            </div>
          </div>

          {/* DRIVERS */}
          <div className="flex min-h-0 flex-col bg-ink-50">
            <div className="border-b border-ink-200 bg-white px-4 pt-4 pb-3">
              <div className="flex items-center gap-2">
                <Users className="size-4.5 text-brand-600" />
                <span className="text-[15px] font-bold">Drivers</span>
                <span className="ml-auto rounded-full border border-ink-200 bg-ink-100 px-2.5 py-0.5 text-xs font-semibold text-ink-500">
                  {board.drivers.filter((d) => showUnavailableDrivers || d.available).length}
                </span>
              </div>
              <div className="mt-1 text-sm text-ink-500">Drag a driver onto a truck</div>
              {selectedTruckObj && (
                <div className="mt-2 rounded-lg border border-brand-200 bg-brand-50 px-2.5 py-1.5 text-[12.5px] font-semibold text-brand-700">
                  Showing drivers for Truck {selectedTruckObj.truck_number} · {selectedTruckObj.hub}
                </div>
              )}
              <label className="mt-2.5 flex items-center gap-1.5 text-[12.5px] text-ink-500">
                <input type="checkbox" checked={showUnavailableDrivers} onChange={(e) => setShowUnavailableDrivers(e.target.checked)} className="accent-brand-600" />
                Show unavailable drivers ({board.drivers.filter((d) => !d.available).length})
              </label>
            </div>
            <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-3">
              {board.drivers.filter((d) => showUnavailableDrivers || d.available).map((driver) => {
                const assignedTruck = Object.entries(board.assignments).find(([, a]) => a.driver_id === driver.driver_id)?.[0]
                const hubMismatch = !!selectedTruckObj && driver.hub !== selectedTruckObj.hub
                const draggable = driver.available && !hubMismatch
                return (
                  <div
                    key={driver.driver_id}
                    draggable={draggable}
                    onDragStart={draggable ? (e) => e.dataTransfer.setData('application/json', JSON.stringify({ type: 'driver', id: driver.driver_id })) : undefined}
                    className="rounded-lg border border-ink-200 bg-white p-3.5 shadow-sm"
                    style={{ cursor: draggable ? 'grab' : 'not-allowed', opacity: !driver.available ? 0.5 : hubMismatch ? 0.4 : assignedTruck ? 0.75 : 1 }}
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <Users className="size-4.5 text-brand-600" />
                        <span className="text-[15px] font-semibold">Driver #{driver.driver_id}</span>
                      </div>
                      <Badge tone={driver.available ? 'green' : 'gray'}>{driver.available ? 'Available' : 'Off today'}</Badge>
                    </div>
                    <div className="mt-1 flex items-center gap-1.5 text-xs text-ink-400">
                      <MapPin className="size-3" /> <span className="font-semibold">{driver.hub} hub</span>
                    </div>
                    {!driver.available && <div className="mt-1 text-xs font-medium text-status-red-500">{driver.unavailable_reason}</div>}
                    {hubMismatch && <div className="mt-1 text-xs font-medium text-status-red-500">Different hub — not available for this truck</div>}
                    {assignedTruck && <div className="mt-1 text-xs font-semibold text-brand-600">Assigned · Truck {assignedTruck}</div>}
                    <div className="mt-3 flex flex-col gap-2">
                      {([
                        ['11h Drive', driver.hos_driving_hours_remaining, 11],
                        ['14h Shift', driver.hos_duty_hours_remaining, 14],
                        ['70h/7-day Cycle', driver.hos_cycle1_hours_remaining, 70],
                      ] as const).map(([label, remaining, max]) => (
                        <div key={label}>
                          <div className="mb-0.5 flex justify-between text-[12.5px] text-ink-500">
                            <span>{label}</span>
                            <span className={`font-bold ${hosColor(remaining)}`}>{remaining.toFixed(1)}h left</span>
                          </div>
                          <div className="h-1.5 overflow-hidden rounded-full bg-ink-100">
                            <div
                              className={`h-full rounded-full ${remaining <= HOS_DANGER_HOURS ? 'bg-status-red-500' : remaining <= HOS_WARN_HOURS ? 'bg-status-amber-500' : 'bg-status-green-500'}`}
                              style={{ width: `${Math.max(0, Math.min(100, (remaining / max) * 100))}%` }}
                            />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className="fixed bottom-6 left-1/2 z-80 flex max-w-md -translate-x-1/2 items-center gap-2 rounded-lg border border-status-amber-500/30 bg-white px-4.5 py-3 text-[13.5px] shadow-lg">
          {toast}
        </div>
      )}

      <ItineraryDialog board={board} open={itineraryOpen} onOpenChange={setItineraryOpen} />
      <TripDetailDialog tripId={selectedTripId} onOpenChange={(v) => setSelectedTripId(v ? selectedTripId : null)} />
    </div>
  )
}

// Real user ask: put fleet size, hub mix, truck-type mix, and order volume directly in the fleet
// manager's hands, up front, for this POC -- instead of a fixed app default (20 trucks, Milton 55/
// London 30/Barrie 15, calibrated order volume) buried in calibration scripts a manager can't see
// or change. One "Simulate" click rebuilds trucks, drivers, AND the order book for the date from
// these numbers; everything else on the board (assignment, AI Assign, Finalize, geofencing) works
// off those tables completely unchanged -- this panel only decides how many of what exist, not how
// dispatch itself runs.
function DispatchSetupPanel({
  date, initial, onCancel, onSimulated,
}: {
  date: string
  initial?: { hub_counts: Record<string, number>; type_shares: Record<string, number>; num_orders: number }
  onCancel?: () => void
  onSimulated: (data: DispatchBoardData) => void
}) {
  const [hubCounts, setHubCounts] = React.useState<Record<string, number>>(initial?.hub_counts ?? DEFAULT_HUB_COUNTS)
  const [typeShares, setTypeShares] = React.useState<Record<string, number>>(initial?.type_shares ?? DEFAULT_TYPE_SHARES)
  const [numOrders, setNumOrders] = React.useState(initial?.num_orders ?? DEFAULT_NUM_ORDERS)
  const [simulating, setSimulating] = React.useState(false)
  const [err, setErr] = React.useState<string | null>(null)

  const totalFleet = HUBS.reduce((sum, hub) => sum + (hubCounts[hub] ?? 0), 0)
  const typeTotal = Object.values(typeShares).reduce((sum, v) => sum + v, 0)

  async function handleSimulate() {
    if (totalFleet <= 0) {
      setErr('At least one hub needs at least one truck & driver.')
      return
    }
    setSimulating(true)
    setErr(null)
    try {
      const shares: Record<string, number> = {}
      for (const [type, pct] of Object.entries(typeShares)) shares[type] = pct / 100
      const data = await api.dispatchSimulate(date, { hub_counts: hubCounts, type_shares: shares, num_orders: numOrders })
      onSimulated(data)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Simulate failed')
    } finally {
      setSimulating(false)
    }
  }

  return (
    <div className="flex h-screen items-center justify-center bg-ink-100 p-6">
      <div className="w-full max-w-xl rounded-xl border border-ink-200 bg-white p-7 shadow-lg">
        <div className="flex items-center gap-2">
          <Sparkles className="size-5 text-brand-600" />
          <h1 className="text-lg font-bold text-ink-900">Set up {format(new Date(`${date}T00:00:00`), 'EEEE, MMM d')}&apos;s fleet</h1>
        </div>
        <p className="mt-1.5 text-sm text-ink-500">
          Pick how many trucks &amp; drivers run out of each hub, the truck-type mix, and how many orders come in --
          Simulate builds the fleet and order book from these numbers, then the board works off them like any other day.
        </p>

        <div className="mt-6">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold tracking-wide text-ink-400 uppercase">Trucks &amp; drivers per hub</span>
            <span className="text-xs text-ink-400">Total: {totalFleet}</span>
          </div>
          <div className="mt-2 grid grid-cols-3 gap-3">
            {HUBS.map((hub) => (
              <label key={hub} className="flex flex-col gap-1">
                <span className="text-[13px] font-medium text-ink-600">{hub}</span>
                <input
                  type="number"
                  min={0}
                  value={hubCounts[hub] ?? 0}
                  onChange={(e) => setHubCounts((prev) => ({ ...prev, [hub]: Math.max(0, Number(e.target.value)) }))}
                  className="rounded-md border border-ink-200 px-2.5 py-1.5 text-sm focus:border-brand-500 focus:outline-none"
                />
              </label>
            ))}
          </div>
        </div>

        <div className="mt-5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold tracking-wide text-ink-400 uppercase">Truck type mix (% of fleet)</span>
            <span className={`text-xs ${typeTotal === 100 ? 'text-ink-400' : 'text-status-amber-500'}`}>Total: {typeTotal}%</span>
          </div>
          <div className="mt-2 grid grid-cols-3 gap-3">
            {(['Dry Van', 'Reefer', 'Flatbed'] as const).map((type) => (
              <label key={type} className="flex flex-col gap-1">
                <span className="text-[13px] font-medium text-ink-600">{type}</span>
                <div className="relative">
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={typeShares[type] ?? 0}
                    onChange={(e) => setTypeShares((prev) => ({ ...prev, [type]: Math.max(0, Number(e.target.value)) }))}
                    className="w-full rounded-md border border-ink-200 px-2.5 py-1.5 pr-6 text-sm focus:border-brand-500 focus:outline-none"
                  />
                  <span className="pointer-events-none absolute top-1/2 right-2.5 -translate-y-1/2 text-xs text-ink-400">%</span>
                </div>
              </label>
            ))}
          </div>
          {typeTotal !== 100 && (
            <p className="mt-1.5 text-[12px] text-ink-400">
              Real equipment is limited and hub-specific -- the mix above is a target, not a guarantee; Simulate gets as
              close as the real fleet on file allows and reports back what it actually built.
            </p>
          )}
        </div>

        <div className="mt-5">
          <span className="text-xs font-semibold tracking-wide text-ink-400 uppercase">Orders for the day</span>
          <input
            type="number"
            min={1}
            value={numOrders}
            onChange={(e) => setNumOrders(Math.max(1, Number(e.target.value)))}
            className="mt-2 w-32 rounded-md border border-ink-200 px-2.5 py-1.5 text-sm focus:border-brand-500 focus:outline-none"
          />
        </div>

        {err && <p className="mt-4 text-sm text-status-red-500">{err}</p>}

        <div className="mt-7 flex items-center justify-end gap-2.5">
          {onCancel && (
            <Button variant="outline" onClick={onCancel} disabled={simulating}>
              Cancel
            </Button>
          )}
          <Button onClick={() => void handleSimulate()} disabled={simulating} className="bg-[#d97757] px-5 py-2.5 hover:bg-[#c2603f]">
            {simulating ? (
              <>
                <Loader2 className="size-4 animate-spin" /> Simulating...
              </>
            ) : (
              <>
                <Sparkles className="size-4" /> Simulate
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  )
}

function FinalizedView({
  board, hasGaps, unassignedCount, onEdit, onSelectTrip,
}: {
  board: DispatchBoardData
  hasGaps: boolean
  unassignedCount: number
  onEdit: () => void
  onSelectTrip: (tripId: string) => void
}) {
  const ordersById = new Map(board.orders.map((o) => [o.order_id, o]))
  const finalizedTrucks = board.trucks
    .map((t) => ({ truck: t, a: board.assignments[t.truck_number] }))
    .filter(({ a }) => a?.driver_id != null && a.order_ids.length > 0)
  const assignedOrderIds = new Set(Object.values(board.assignments).flatMap((a) => a.order_ids))
  const unassignedOrders = board.orders.filter((o) => !assignedOrderIds.has(o.order_id))

  return (
    <div className="flex-1 overflow-y-auto p-7">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-xl font-bold">Final Dispatch Plan</div>
          <div className="mt-0.5 text-sm text-ink-500">
            {format(new Date(`${board.service_date}T00:00:00`), 'EEEE, MMM d')} · {finalizedTrucks.length} truck(s) with driver &amp; trips assigned
          </div>
        </div>
        <Button variant="outline" onClick={onEdit}>Edit Dispatch</Button>
      </div>

      {hasGaps && (
        <div className="mb-5 flex flex-col gap-2 rounded-lg border border-status-amber-500/30 bg-status-amber-100 px-4 py-3">
          {/* Real user ask: this used to open with "Dispatch is incomplete" -- reads as a failure
              banner even when the AI just placed 90%+ of the day on its own. Lead with what it DID
              do (the real number), frame the rest as a quick manual pass, not a shortfall -- that's
              the actual value being delivered: a manager checking a handful of edge cases instead
              of building the whole day by hand. */}
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="text-sm font-semibold text-[#7a5416]">
              AI matched {board.orders.length - unassignedCount} of {board.orders.length} orders
              ({Math.round(((board.orders.length - unassignedCount) / board.orders.length) * 100)}%) automatically —
            </span>
            <span className="rounded-full border border-status-amber-500/30 bg-white px-2.5 py-1 text-[13.5px] text-[#7a5416]">
              {unassignedCount} left for a quick manual look
            </span>
          </div>
          {/* Real user ask: show WHY each one couldn't be matched, not just the count. */}
          <div className="flex flex-col gap-1">
            {unassignedOrders.map((o) => (
              <div key={o.order_id} className="flex items-start gap-1.5 text-[12.5px] text-[#7a5416]">
                <AlertTriangle className="mt-0.5 size-3 shrink-0" />
                <span><span className="font-semibold">{o.pickup_city} → {o.dest_city}</span> ({o.load_type}, {o.pallets} plt) — {o.unassigned_reason}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {finalizedTrucks.length === 0 ? (
        <div className="rounded-xl border border-dashed border-ink-300 py-16 text-center text-sm text-ink-500">
          No truck has both a driver and at least one trip assigned yet. Go back to the board to finish assigning.
        </div>
      ) : (
        <div className="flex flex-col gap-4.5">
          {finalizedTrucks.map(({ truck, a }) => {
            const driver = board.drivers.find((d) => d.driver_id === a!.driver_id)!
            const orders = a!.order_ids.map((id) => ordersById.get(id)).filter((o): o is DispatchOrder => !!o)
            const revenue = orders.reduce((s, o) => s + o.rate, 0)
            // Peak single-order load, not summed across the day's chained trips -- see the
            // matching fix/comment on the draft board's own truck cards, above.
            const peakWeight = orders.length ? Math.max(...orders.map((o) => o.weight_lbs)) : 0
            const pct = Math.round((peakWeight / truck.capacity_lbs) * 100)
            return (
              <div key={truck.truck_number} className="rounded-xl border border-ink-200 bg-white p-5 shadow-sm">
                <div className="mb-4 flex flex-wrap items-center justify-between gap-4 border-b border-ink-200 pb-4">
                  <div className="flex flex-wrap items-center gap-6">
                    <div className="flex items-center gap-2.5">
                      <Truck className="size-5.5 text-brand-600" />
                      <div>
                        <div className="text-[15px] font-bold">Truck {truck.truck_number}</div>
                        <div className="text-xs text-ink-500">{truck.truck_type} · {truck.length_ft}ft</div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2.5">
                      <Users className="size-5 text-brand-600" />
                      <div>
                        <div className="text-[15px] font-bold">Driver #{driver.driver_id}</div>
                        <div className="text-xs text-ink-500">{driver.hos_driving_hours_remaining}h drive · {driver.hos_duty_hours_remaining}h shift left</div>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-6">
                    <div className="text-right">
                      <div className="text-xs text-ink-500">Load</div>
                      <div className={`text-[15px] font-bold ${capacityColor(pct)}`}>{pct}%</div>
                    </div>
                    <div className="text-right">
                      <div className="text-xs text-ink-500">Revenue</div>
                      <div className="text-[15px] font-bold text-brand-600">${revenue.toLocaleString()}</div>
                    </div>
                  </div>
                </div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Stop</TableHead>
                      <TableHead>Pickup</TableHead>
                      <TableHead>Dropoff</TableHead>
                      <TableHead>Pickup time</TableHead>
                      <TableHead>ETA</TableHead>
                      <TableHead>Weight</TableHead>
                      <TableHead>Pallets</TableHead>
                      <TableHead>Trailer</TableHead>
                      <TableHead>Rate</TableHead>
                      <TableHead>Geofence</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {orders.map((o, i) => (
                      <TableRow
                        key={o.order_id}
                        className={o.trip_id ? 'cursor-pointer hover:bg-ink-50' : undefined}
                        onClick={() => o.trip_id && onSelectTrip(o.trip_id)}
                        title={o.trip_id ? 'Click for full trip detail + geofence editor' : undefined}
                      >
                        <TableCell>{i + 1}</TableCell>
                        <TableCell>{o.pickup_city}</TableCell>
                        <TableCell>{o.dest_city}</TableCell>
                        <TableCell className="text-ink-500">{format(new Date(o.pickup_at), 'HH:mm')}</TableCell>
                        <TableCell className="text-ink-500">{format(new Date(o.delivery_eta), 'HH:mm')}</TableCell>
                        <TableCell>{formatLbs(o.weight_lbs)}</TableCell>
                        <TableCell>{o.pallets}</TableCell>
                        <TableCell><Badge>{o.load_type}</Badge></TableCell>
                        <TableCell className="font-semibold">${Math.round(o.rate).toLocaleString()}</TableCell>
                        <TableCell onClick={(e) => e.stopPropagation()}>
                          {/* Real user ask: a row-click-to-open geofence editor wasn't discoverable
                              enough (and briefly didn't work at all right after Finalize -- see the
                              setStatus() fix above) -- an explicit button per row, not just "the
                              whole row happens to be clickable," is the ask. */}
                          <div className="flex items-center gap-2">
                            {o.pickup_geofence_source === 'manual' || o.dropoff_geofence_source === 'manual' ? (
                              <Badge tone="blue">Manual</Badge>
                            ) : (
                              <Badge tone="gray">Default</Badge>
                            )}
                            {o.trip_id && (
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => onSelectTrip(o.trip_id!)}
                                className="h-7 px-2 text-xs text-ink-600 hover:bg-ink-100"
                              >
                                <PenLine className="size-3" /> Edit Geofence
                              </Button>
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// Real user ask: clicking a trip under a truck/driver on the Final Dispatch Plan opens its full
// detail -- pickup/dropoff, times, truck/load -- plus a map where the manager can draw a CUSTOM
// geofence for pickup or dropoff, overwriting the default radius circle for this trip only.
function TripDetailDialog({ tripId, onOpenChange }: { tripId: string | null; onOpenChange: (v: boolean) => void }) {
  const [trip, setTrip] = React.useState<TripDetail | null>(null)
  const [routeCoords, setRouteCoords] = React.useState<[number, number][] | null>(null)
  const [loading, setLoading] = React.useState(false)
  const [editingLocationId, setEditingLocationId] = React.useState<number | null>(null)
  const [drawnPoints, setDrawnPoints] = React.useState<[number, number][] | null>(null)
  const [saving, setSaving] = React.useState(false)
  // Real gap found directly: handleSave()/handleRevert() had no catch block -- a failed save
  // (network hiccup, a 400/500) threw silently into an unhandled promise rejection (browser
  // console only), with NO on-screen sign anything went wrong. From the user's seat that looks
  // exactly like "I drew a shape, there was no save option, and it just went back to default" --
  // the draw itself worked, but a failure had zero visible feedback either way.
  const [saveError, setSaveError] = React.useState<string | null>(null)
  const [loadError, setLoadError] = React.useState<string | null>(null)

  // Real bug found directly: this had no catch block at all -- a failed fetch (a redeploy mid-
  // rollout, a network blip) threw into an unhandled promise rejection with trip/routeCoords
  // stuck at their initial null. The dialog's own render guard (`loading || !trip`) then showed
  // the loading spinner FOREVER with no error, no retry -- indistinguishable from "broken."
  const load = React.useCallback(async (id: string) => {
    setLoading(true)
    setLoadError(null)
    try {
      const detail = await api.tripDetail(id)
      setTrip(detail)
      const route = await api.route(detail.pickup.location_id, detail.dropoff.location_id)
      setRouteCoords(route.coordinates)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Could not load this trip')
    } finally {
      setLoading(false)
    }
  }, [])

  React.useEffect(() => {
    setEditingLocationId(null)
    setDrawnPoints(null)
    setSaveError(null)
    setLoadError(null)
    if (tripId) {
      void load(tripId)
    } else {
      setTrip(null)
      setRouteCoords(null)
    }
  }, [tripId, load])

  async function handleSave() {
    if (!trip || editingLocationId == null || !drawnPoints || drawnPoints.length < 3) return
    setSaving(true)
    setSaveError(null)
    try {
      await api.saveTripGeofence(trip.trip_id, editingLocationId, drawnPoints)
      setEditingLocationId(null)
      setDrawnPoints(null)
      await load(trip.trip_id)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Could not save this geofence')
    } finally {
      setSaving(false)
    }
  }

  async function handleRevert(locationId: number) {
    if (!trip) return
    try {
      await api.clearTripGeofence(trip.trip_id, locationId)
      await load(trip.trip_id)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Could not revert this geofence')
    }
  }

  const editingStop: GeofenceStop | null =
    trip && editingLocationId != null ? (trip.pickup.location_id === editingLocationId ? trip.pickup : trip.dropoff) : null

  return (
    <Dialog open={!!tripId} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-5xl">
        <DialogHeader>
          <DialogTitle>Trip Detail{trip ? ` · Driver #${trip.driver_id} · Truck ${trip.truck_number ?? '—'}` : ''}</DialogTitle>
        </DialogHeader>
        {loading ? (
          <div className="flex h-96 items-center justify-center text-ink-400"><Loader2 className="size-5 animate-spin" /></div>
        ) : loadError || !trip ? (
          <div className="flex h-96 flex-col items-center justify-center gap-3 text-center">
            <AlertTriangle className="size-6 text-status-red-500" />
            <p className="text-sm text-ink-600">{loadError ?? 'Could not load this trip'}</p>
            <Button size="sm" variant="outline" onClick={() => tripId && void load(tripId)}>Try again</Button>
          </div>
        ) : (
          <div className="grid grid-cols-[320px_minmax(0,1fr)] gap-5">
            <div className="flex flex-col gap-3.5">
              <div className="rounded-lg border border-ink-200 p-3.5">
                <div className="text-xs font-semibold text-ink-400 uppercase">Load</div>
                <div className="mt-1 text-sm font-semibold">{trip.truck_type ?? 'Truck type unknown'}</div>
                <div className="text-xs text-ink-500">{formatLbs(trip.weight_lbs ?? 0)} · {trip.pallets ?? 0} plt · {trip.load_type}</div>
                <div className="mt-1 text-xs text-ink-400">
                  Capacity: {formatLbs(trip.capacity_lbs ?? 0)} / {trip.capacity_pallets ?? '—'} plt
                </div>
              </div>

              {([
                ['pickup', trip.pickup, '#1baf7a', trip.created_at],
                ['dropoff', trip.dropoff, '#eb6834', trip.eta],
              ] as const).map(([key, stop, color, when]) => (
                <div key={key} className="rounded-lg border border-ink-200 p-3.5">
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-xs font-semibold text-ink-400 uppercase">{key === 'pickup' ? 'Pickup' : 'Dropoff'}</div>
                    <Badge tone={stop.geofence_source === 'manual' ? 'blue' : 'gray'}>
                      {stop.geofence_source === 'manual' ? 'Manual geofence' : 'Default geofence'}
                    </Badge>
                  </div>
                  <div className="mt-1 text-sm font-semibold">{stop.city}</div>
                  <div className="text-xs text-ink-500">{stop.label}</div>
                  <div className="mt-1 text-xs text-ink-400">{when ? format(new Date(when), 'EEE MMM d, HH:mm') : 'Time unknown'}</div>
                  <div className="mt-2.5 flex flex-wrap gap-2">
                    {editingLocationId === stop.location_id ? (
                      <>
                        {/* Real user ask: make the points captured while drawing directly visible,
                            not just inferred from a button's enabled/disabled state -- and this
                            IS the button that persists the shape (POST /api/trips/{id}/geofence),
                            not the leaflet-draw toolbar's own icon-only controls on the map itself,
                            which only draw/edit the shape locally and never talk to the backend. */}
                        <div className="w-full text-xs font-medium text-ink-500">
                          {drawnPoints && drawnPoints.length >= 3
                            ? `${drawnPoints.length} point shape drawn — ready to save`
                            : 'Draw a shape on the map (3+ points), then Save Geofence appears here'}
                        </div>
                        <Button
                          onClick={() => void handleSave()} disabled={!drawnPoints || drawnPoints.length < 3 || saving}
                          className="w-full text-white justify-center py-2.5 text-[14px] font-semibold" style={{ backgroundColor: color }}
                        >
                          <PenLine className="size-4" /> {saving ? 'Saving…' : 'Save Geofence'}
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => { setEditingLocationId(null); setDrawnPoints(null) }}>Cancel</Button>
                      </>
                    ) : (
                      <>
                        <Button size="sm" variant="outline" onClick={() => { setEditingLocationId(stop.location_id); setDrawnPoints(null) }}>
                          <PenLine className="size-3.5" /> {stop.geofence_source === 'manual' ? 'Redraw Geofence' : 'Draw Custom Geofence'}
                        </Button>
                        {stop.geofence_source === 'manual' && (
                          <Button size="sm" variant="outline" onClick={() => void handleRevert(stop.location_id)}>
                            <Undo2 className="size-3.5" /> Revert to Default
                          </Button>
                        )}
                      </>
                    )}
                  </div>
                </div>
              ))}
              {editingLocationId != null && (
                <div className="rounded-lg bg-brand-50 px-3 py-2 text-xs text-brand-700">
                  Zoom in on the map, then use the draw tool (top-right) to trace the geofence —
                  click the first point again to close the shape, then Save.
                </div>
              )}
              {saveError && (
                <div className="flex items-start gap-1.5 rounded-lg bg-status-red-100 px-3 py-2 text-xs text-status-red-700">
                  <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                  <span>{saveError}</span>
                </div>
              )}
            </div>
            <div className="h-[520px] overflow-hidden rounded-lg border border-ink-200">
              <GeofenceMap
                pickup={trip.pickup} dropoff={trip.dropoff} routeCoords={routeCoords}
                editingStop={editingStop} hasDraftShape={!!drawnPoints && drawnPoints.length >= 3}
                onDrawnChange={setDrawnPoints}
              />
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function ItineraryDialog({ board, open, onOpenChange }: { board: DispatchBoardData; open: boolean; onOpenChange: (v: boolean) => void }) {
  const ordersById = new Map(board.orders.map((o) => [o.order_id, o]))
  const rows = board.trucks
    .map((t) => ({ truck: t, a: board.assignments[t.truck_number] }))
    .filter(({ a }) => a && (a.driver_id != null || a.order_ids.length > 0))
    .map(({ truck, a }) => {
      const driver = a!.driver_id != null ? board.drivers.find((d) => d.driver_id === a!.driver_id) : null
      const orders = a!.order_ids.map((id) => ordersById.get(id)).filter((o): o is DispatchOrder => !!o)
      const route = orders.length ? [orders[0].pickup_city, ...orders.map((o) => o.dest_city)].join(' → ') : '—'
      // Peak single-order load, not summed across the day's chained trips -- see the matching
      // fix/comment on the draft board's own truck cards.
      const peakWeight = orders.length ? Math.max(...orders.map((o) => o.weight_lbs)) : 0
      const pct = Math.round((peakWeight / truck.capacity_lbs) * 100)
      const revenue = orders.reduce((s, o) => s + o.rate, 0)
      const earliest = orders.length ? orders.map((o) => o.pickup_at).sort()[0] : null
      return { truck, driver, orders, route, pct, revenue, earliest }
    })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>Truck · Driver · Trips Itinerary</DialogTitle>
        </DialogHeader>
        <div className="max-h-[70vh] overflow-y-auto">
          {rows.length === 0 ? (
            <div className="py-8 text-center text-sm text-ink-500">Assign orders and drivers to trucks to build the itinerary.</div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Truck</TableHead>
                  <TableHead>Driver</TableHead>
                  <TableHead>Route</TableHead>
                  <TableHead>Orders</TableHead>
                  <TableHead>Load</TableHead>
                  <TableHead>Departure</TableHead>
                  <TableHead>Revenue</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map(({ truck, driver, orders, route, pct, revenue, earliest }) => (
                  <TableRow key={truck.truck_number}>
                    <TableCell>
                      <div className="font-semibold">{truck.truck_number}</div>
                      <div className="text-xs text-ink-400">{truck.truck_type}</div>
                    </TableCell>
                    <TableCell className={driver ? '' : 'text-ink-400 italic'}>{driver ? `Driver #${driver.driver_id}` : 'Unassigned'}</TableCell>
                    <TableCell className="max-w-56 text-[13px]">{route}</TableCell>
                    <TableCell>{orders.length}</TableCell>
                    <TableCell>
                      <span className={`font-bold ${capacityColor(pct)}`}>{pct}%</span>
                    </TableCell>
                    <TableCell className="text-[13px]">{earliest ? format(new Date(earliest), 'HH:mm') : '—'}</TableCell>
                    <TableCell className="font-semibold">{revenue ? `$${revenue.toLocaleString()}` : '—'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
