import { format } from 'date-fns'
import { ArrowDown, ArrowUp, ArrowUpDown, Loader2, X } from 'lucide-react'
import * as React from 'react'
import { PageHeader } from '@/components/page-header'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

// Real user ask: "a page that will show this data tables from simulation showcase in detail...
// like a sql data view page where on top we have tabs for tables and this page shows those tables
// with description on top what this table is". Real user pivot: the simulation is now the app's
// only data source (no more live.* telemetry) -- this reads the exact tables sim/live/
// ai_dispatch_replay.py's generate() populates on every replay (dashboard/server/main.py's
// DATA_TABLES is the single source of truth for the list/descriptions, not duplicated here).

interface TableMeta {
  table: string
  label: string
  description: string
}

interface TableRows {
  table: string
  columns: string[]
  rows: unknown[][]
}

// A value is "date-ish" if its column name suggests a timestamp -- rendered as a real formatted
// date instead of a raw ISO string, without needing a per-table column-type map.
const DATE_COLUMN_HINTS = ['_at', 'created_at', 'issued_at', 'due_at']

function isDateColumn(col: string): boolean {
  return DATE_COLUMN_HINTS.some((hint) => col.includes(hint))
}

function formatCell(col: string, value: unknown): string {
  if (value == null) return '—'
  if (isDateColumn(col) && typeof value === 'string') {
    const d = new Date(value)
    if (!Number.isNaN(d.getTime())) return format(d, 'MMM d, HH:mm:ss')
  }
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2)
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (Array.isArray(value)) return value.join(', ')
  return String(value)
}

// Real user ask: "filter for a trip and see it... all tables like Excel sheets." Debounced so
// typing doesn't fire a request per keystroke; filtering happens server-side (see main.py's
// data_table_rows) rather than on just the already-fetched page, since the default view is only
// the most recent 200 rows across every run -- an older trip's snapshots could be well past that.
const FILTER_DEBOUNCE_MS = 350

function DataTableView({ table }: { table: string }) {
  const [data, setData] = React.useState<TableRows | null>(null)
  const [loading, setLoading] = React.useState(true)
  const [error, setError] = React.useState<string | null>(null)
  const [filterInputs, setFilterInputs] = React.useState<Record<string, string>>({})
  const [activeFilters, setActiveFilters] = React.useState<Record<string, string>>({})
  // Real user ask: "options to sort in descending or ascending order." Three-state per column,
  // cycled by clicking its header: none (table's own default recency order) -> asc -> desc -> none.
  const [sortCol, setSortCol] = React.useState<string | null>(null)
  const [sortDir, setSortDir] = React.useState<'asc' | 'desc'>('asc')

  function toggleSort(col: string) {
    if (sortCol !== col) {
      setSortCol(col)
      setSortDir('asc')
    } else if (sortDir === 'asc') {
      setSortDir('desc')
    } else {
      setSortCol(null)
    }
  }

  // New table -> drop whatever filters/sort were set for the previous one's columns.
  React.useEffect(() => {
    setFilterInputs({})
    setActiveFilters({})
    setSortCol(null)
    setSortDir('asc')
  }, [table])

  React.useEffect(() => {
    const id = setTimeout(() => setActiveFilters(filterInputs), FILTER_DEBOUNCE_MS)
    return () => clearTimeout(id)
  }, [filterInputs])

  React.useEffect(() => {
    setLoading(true)
    setError(null)
    const nonEmpty = Object.fromEntries(Object.entries(activeFilters).filter(([, v]) => v.trim() !== ''))
    const params = new URLSearchParams()
    if (Object.keys(nonEmpty).length > 0) params.set('filters', JSON.stringify(nonEmpty))
    if (sortCol) {
      params.set('sort_col', sortCol)
      params.set('sort_dir', sortDir)
    }
    const qs = params.toString() ? `?${params.toString()}` : ''
    fetch(`/api/data/${table}${qs}`)
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        return res.json()
      })
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : 'Could not load this table'))
      .finally(() => setLoading(false))
  }, [table, activeFilters, sortCol, sortDir])

  const hasActiveFilter = Object.values(activeFilters).some((v) => v.trim() !== '')

  if (error) return <p className="p-6 text-sm text-status-red-500">{error}</p>
  if (!data) return <div className="flex items-center gap-2 p-6 text-sm text-ink-400"><Loader2 className="size-4 animate-spin" /> Loading…</div>

  return (
    <div className="overflow-auto rounded-xl border border-ink-200 bg-white">
      <Table>
        <TableHeader>
          <TableRow>
            {data.columns.map((col) => (
              <TableHead key={col} className="whitespace-nowrap font-mono text-[11px] uppercase tracking-wide">
                <button onClick={() => toggleSort(col)} className="flex items-center gap-1 hover:text-ink-900">
                  {col}
                  {sortCol === col ? (
                    sortDir === 'asc' ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />
                  ) : (
                    <ArrowUpDown className="size-3 text-ink-300" />
                  )}
                </button>
              </TableHead>
            ))}
          </TableRow>
          {/* Excel-style filter row -- one text box per column, substring-matched server-side. */}
          <TableRow>
            {data.columns.map((col) => (
              <TableHead key={col} className="p-1">
                <div className="relative">
                  <input
                    type="text"
                    value={filterInputs[col] ?? ''}
                    onChange={(e) => setFilterInputs((f) => ({ ...f, [col]: e.target.value }))}
                    placeholder="Filter…"
                    className="w-full rounded-md border border-ink-200 bg-white px-2 py-1 pr-5 text-[11px] font-normal normal-case tracking-normal text-ink-700 focus:border-brand-500 focus:outline-none"
                  />
                  {filterInputs[col] && (
                    <button
                      onClick={() => setFilterInputs((f) => ({ ...f, [col]: '' }))}
                      className="absolute right-1 top-1/2 -translate-y-1/2 text-ink-300 hover:text-ink-600"
                    >
                      <X className="size-3" />
                    </button>
                  )}
                </div>
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {loading ? (
            <TableRow>
              <TableCell colSpan={data.columns.length} className="p-6 text-center text-sm text-ink-400">
                <Loader2 className="mr-2 inline size-4 animate-spin" /> Loading…
              </TableCell>
            </TableRow>
          ) : data.rows.length === 0 ? (
            <TableRow>
              <TableCell colSpan={data.columns.length} className="p-10 text-center text-sm text-ink-400">
                {hasActiveFilter
                  ? 'No rows match these filters.'
                  : 'No rows yet — run "AI Assign" on the Dispatch page and open Live Ops Simulation to generate a replay.'}
              </TableCell>
            </TableRow>
          ) : (
            data.rows.map((row, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <TableRow key={i}>
                {row.map((value, j) => (
                  // eslint-disable-next-line react/no-array-index-key
                  <TableCell key={j} className="whitespace-nowrap text-xs">{formatCell(data.columns[j], value)}</TableCell>
                ))}
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  )
}

export default function Data() {
  const [tables, setTables] = React.useState<TableMeta[]>([])
  const [active, setActive] = React.useState<string | null>(null)

  React.useEffect(() => {
    fetch('/api/data/tables')
      .then((res) => res.json())
      .then((list: TableMeta[]) => {
        setTables(list)
        if (list.length > 0) setActive(list[0].table)
      })
      .catch(() => setTables([]))
  }, [])

  const activeMeta = tables.find((t) => t.table === active)

  return (
    <div className="flex h-screen flex-col">
      <PageHeader
        title="Data"
        description="Every real table the AI-dispatch simulation writes to, in one place — a direct look at what's actually stored, not a summarized view."
      />
      <div className="flex-1 overflow-auto p-6">
        {tables.length === 0 ? (
          <p className="text-sm text-ink-400">Loading table list…</p>
        ) : (
          <Tabs value={active ?? undefined} onValueChange={setActive}>
            <TabsList>
              {tables.map((t) => (
                <TabsTrigger key={t.table} value={t.table}>{t.label}</TabsTrigger>
              ))}
            </TabsList>
            {activeMeta && (
              <p className="mt-3 max-w-3xl text-sm text-ink-500">{activeMeta.description}</p>
            )}
            {tables.map((t) => (
              <TabsContent key={t.table} value={t.table} className="mt-3">
                {active === t.table && <DataTableView table={t.table} />}
              </TabsContent>
            ))}
          </Tabs>
        )}
      </div>
    </div>
  )
}
