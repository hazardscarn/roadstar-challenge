import { format } from 'date-fns'
import { Download, FileText, Loader2, Send } from 'lucide-react'
import * as React from 'react'
import { PageHeader } from '@/components/page-header'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { supabase } from '@/lib/supabase'

// simulation.invoices (sim/sql/038) -- real CRA-itemized invoicing against the AI-dispatch
// replay's completed trips (real user pivot: the simulation is now the app's only data source,
// live.* is dropped). PDF generation + "mark as sent" only, per the confirmed decision: real
// email delivery is deferred until an email provider is chosen, stated in the UI, not silently
// faked.
const POLL_MS = 15000

interface TripToInvoice {
  trip_id: string
  driver_id: number
  truck_number: string
  completed_at: string
  loaded_miles: number | null
}
interface InvoiceRow {
  invoice_id: string
  invoice_number: string
  trip_id: string
  issued_at: string
  bill_to_name: string | null
  linehaul_amount: number
  detention_amount: number
  fuel_surcharge_amount: number
  accessorial_amount: number
  subtotal: number
  tax_amount: number
  total_amount: number
  status: string
}

const STATUS_TONE: Record<string, 'gray' | 'blue' | 'green'> = { draft: 'gray', sent: 'blue', paid: 'green' }

export default function Billing() {
  const [toInvoice, setToInvoice] = React.useState<TripToInvoice[]>([])
  const [invoices, setInvoices] = React.useState<InvoiceRow[]>([])
  const [generatingTripId, setGeneratingTripId] = React.useState<string | null>(null)
  const [sendingId, setSendingId] = React.useState<string | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  // Real user pivot: the simulation is now the app's only data source -- sim/live/
  // ai_dispatch_replay.py's generate() already auto-creates a draft invoice per trip right after
  // a replay, so "Ready to invoice" below should normally be empty (everything's already
  // invoiced); the Generate button stays as an idempotent fallback, not the primary path.
  const refresh = React.useCallback(async () => {
    const { data: invs } = await supabase.schema('simulation').from('invoices').select('*').order('issued_at', { ascending: false })
    setInvoices((invs as unknown as InvoiceRow[]) ?? [])
    const invoicedTripIds = new Set((invs ?? []).map((i) => i.trip_id))

    const { data: logs } = await supabase
      .schema('simulation')
      .from('trip_log')
      .select('trip_id,driver_id,truck_number,completed_at,loaded_miles')
      .order('completed_at', { ascending: false })
      .limit(50)
    setToInvoice(((logs as unknown as TripToInvoice[]) ?? []).filter((t) => !invoicedTripIds.has(t.trip_id)))
  }, [])

  React.useEffect(() => {
    refresh()
    const id = setInterval(refresh, POLL_MS)
    return () => clearInterval(id)
  }, [refresh])

  async function handleGenerate(tripId: string) {
    setGeneratingTripId(tripId)
    setError(null)
    try {
      const res = await fetch('/api/invoices/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trip_id: tripId }),
      })
      if (!res.ok) throw new Error(await res.text())
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Invoice generation failed')
    } finally {
      setGeneratingTripId(null)
    }
  }

  async function handleMarkSent(invoiceId: string) {
    setSendingId(invoiceId)
    await fetch(`/api/invoices/${invoiceId}/mark-sent`, { method: 'POST' })
    await refresh()
    setSendingId(null)
  }

  return (
    <div className="flex h-screen flex-col">
      <PageHeader title="Billing" description="CRA-itemized invoices from completed trips — linehaul, detention, fuel surcharge, accessorial kept separate." />
      <div className="flex-1 overflow-auto p-6">
        {error && <p className="mb-3 text-sm text-status-red-500">{error}</p>}

        <h2 className="mb-2 font-display text-sm font-semibold text-ink-900">Ready to invoice</h2>
        {toInvoice.length === 0 ? (
          <p className="mb-6 text-xs text-ink-400">Every completed trip has an invoice.</p>
        ) : (
          <div className="mb-6 flex flex-col gap-2">
            {toInvoice.map((t) => (
              <div key={t.trip_id} className="flex items-center justify-between rounded-lg border border-ink-200 bg-white p-3 text-sm">
                <div>
                  <span className="font-medium text-ink-800">Driver {t.driver_id}</span>
                  <span className="text-ink-400"> · Truck {t.truck_number} · {t.loaded_miles?.toFixed(1) ?? '—'} mi</span>
                  <div className="text-xs text-ink-400">Completed {format(new Date(t.completed_at), 'MMM d, HH:mm')}</div>
                </div>
                <Button size="sm" disabled={generatingTripId !== null} onClick={() => handleGenerate(t.trip_id)}>
                  {generatingTripId === t.trip_id ? <Loader2 className="size-4 animate-spin" /> : <FileText className="size-4" />}
                  Generate invoice
                </Button>
              </div>
            ))}
          </div>
        )}

        <h2 className="mb-2 font-display text-sm font-semibold text-ink-900">Invoices</h2>
        {invoices.length === 0 ? (
          <p className="text-xs text-ink-400">No invoices yet.</p>
        ) : (
          <div className="flex flex-col gap-2">
            {invoices.map((inv) => (
              <div key={inv.invoice_id} className="rounded-lg border border-ink-200 bg-white p-3 text-sm">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-ink-800">{inv.invoice_number}</span>
                    <Badge tone={STATUS_TONE[inv.status] ?? 'gray'}>{inv.status}</Badge>
                    <span className="text-xs text-ink-400">Bill to: {inv.bill_to_name}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <a href={`/api/invoices/${inv.invoice_id}/pdf`} target="_blank" rel="noreferrer">
                      <Button size="sm" variant="outline"><Download className="size-4" /> PDF</Button>
                    </a>
                    {inv.status === 'draft' && (
                      <Button size="sm" variant="secondary" disabled={sendingId !== null} onClick={() => handleMarkSent(inv.invoice_id)}>
                        {sendingId === inv.invoice_id ? <Loader2 className="size-4 animate-spin" /> : <Send className="size-4" />}
                        Mark as sent
                      </Button>
                    )}
                  </div>
                </div>
                <div className="mt-2 grid grid-cols-6 gap-2 border-t border-ink-100 pt-2 text-xs">
                  <div><div className="text-ink-400">Linehaul</div><div className="font-medium text-ink-800">${inv.linehaul_amount?.toFixed(2)}</div></div>
                  <div><div className="text-ink-400">Fuel surcharge</div><div className="font-medium text-ink-800">${inv.fuel_surcharge_amount?.toFixed(2) ?? '0.00'}</div></div>
                  <div><div className="text-ink-400">Detention</div><div className="font-medium text-ink-800">${inv.detention_amount?.toFixed(2) ?? '0.00'}</div></div>
                  <div><div className="text-ink-400">Accessorial</div><div className="font-medium text-ink-800">${inv.accessorial_amount?.toFixed(2) ?? '0.00'}</div></div>
                  <div><div className="text-ink-400">Subtotal</div><div className="font-medium text-ink-800">${inv.subtotal?.toFixed(2)}</div></div>
                  <div><div className="text-ink-400">Total (incl. HST ${inv.tax_amount?.toFixed(2)})</div><div className="font-semibold text-ink-900">${inv.total_amount?.toFixed(2)}</div></div>
                </div>
              </div>
            ))}
          </div>
        )}
        <p className="mt-4 text-[11px] text-ink-400">
          "Mark as sent" records the invoice as sent in the database — real email delivery isn't wired up yet (no provider chosen).
        </p>
      </div>
    </div>
  )
}
