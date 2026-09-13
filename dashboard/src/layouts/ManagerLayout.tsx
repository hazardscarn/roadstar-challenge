import { Map, Send, History, Wrench, Users, Receipt, Radar, Database, LogOut } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useAuth } from '@/lib/auth-context'

// Real user pivot: the always-on live.* telemetry feed (the old "Live Ops") is dropped --
// simulation.* (the AI-dispatch full-day replay) is now the app's only data source, so there's no
// separate "live" fleet to watch alongside it. Simulation Showcase takes over the Live Ops slot,
// relabeled "Live Ops Simulation"; Trip History and Billing now read from the same simulation.*
// tables that replay populates, and a new "Data" tab exposes those tables directly for the demo.
const NAV = [
  { to: '/manager', label: 'Live Ops Simulation', icon: Map, end: true },
  { to: '/manager/dispatch', label: 'Dispatch', icon: Send },
  { to: '/manager/trips', label: 'Trip History', icon: History },
  { to: '/manager/billing', label: 'Billing', icon: Receipt },
  { to: '/manager/data', label: 'Data', icon: Database },
  { to: '/manager/simulation-trip', label: 'Simulation Trip', icon: Radar },
  // Real user ask: these two moved to the bottom -- secondary now that Live Ops Simulation,
  // Dispatch, Trip History, Billing, and Data are the primary simulation-driven flow.
  { to: '/manager/fleet-health', label: 'Fleet Health', icon: Wrench },
  { to: '/manager/drivers', label: 'Drivers', icon: Users },
]

export default function ManagerLayout() {
  const { session, signOut } = useAuth()
  return (
    <div className="flex h-screen w-screen overflow-hidden bg-ink-50">
      <aside className="flex w-56 shrink-0 flex-col border-r border-ink-200 bg-white">
        <div className="flex items-center gap-2 border-b border-ink-200 px-4 py-4">
          <div className="flex size-8 items-center justify-center rounded-lg bg-brand-600 font-display text-sm font-bold text-white">
            A
          </div>
          <div>
            <div className="font-display text-sm font-bold text-ink-900 leading-tight">Alfred</div>
            <div className="text-[11px] text-ink-400 leading-tight">Dispatch — Manager</div>
          </div>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 p-2">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                  isActive ? 'bg-brand-50 text-brand-700' : 'text-ink-600 hover:bg-ink-100',
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-ink-200 p-3">
          <div className="mb-2 truncate text-xs text-ink-500">{session?.user.email}</div>
          <button
            onClick={() => void signOut()}
            className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-ink-500 hover:bg-ink-100"
          >
            <LogOut className="size-4" />
            Sign out
          </button>
        </div>
      </aside>
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}
