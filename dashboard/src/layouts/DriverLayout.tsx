import { Home, ClipboardCheck, BarChart3, Truck } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '@/lib/auth-context'
import { cn } from '@/lib/utils'

// 4 destinations, mobile-first: bottom tab bar under 640px (real driver-app pattern), left rail
// on desktop for the same responsive web view the brief explicitly allows in place of a native app.
const NAV = [
  { to: '/driver', label: 'Driver Assist', icon: Home, end: true },
  { to: '/driver/inspection', label: 'Inspection', icon: ClipboardCheck },
  { to: '/driver/stats', label: 'My Stats', icon: BarChart3 },
  { to: '/driver/truck', label: 'My Truck', icon: Truck },
]

export default function DriverLayout() {
  const { signOut } = useAuth()
  return (
    <div className="flex min-h-screen flex-col bg-ink-50 sm:flex-row">
      <header className="flex items-center justify-between border-b border-ink-200 bg-white px-4 py-3 sm:hidden">
        <div className="flex items-center gap-2">
          <div className="flex size-7 items-center justify-center rounded-lg bg-brand-600 font-display text-xs font-bold text-white">
            A
          </div>
          <span className="font-display text-sm font-bold text-ink-900">Alfred</span>
        </div>
        <button onClick={() => void signOut()} className="text-xs text-ink-500">
          Sign out
        </button>
      </header>

      <aside className="hidden w-52 shrink-0 flex-col border-r border-ink-200 bg-white sm:flex">
        <div className="flex items-center gap-2 border-b border-ink-200 px-4 py-4">
          <div className="flex size-8 items-center justify-center rounded-lg bg-brand-600 font-display text-sm font-bold text-white">
            A
          </div>
          <div>
            <div className="font-display text-sm font-bold text-ink-900 leading-tight">Alfred</div>
            <div className="text-[11px] text-ink-400 leading-tight">Driver</div>
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
        <button
          onClick={() => void signOut()}
          className="m-3 rounded-lg px-3 py-2 text-left text-sm text-ink-500 hover:bg-ink-100"
        >
          Sign out
        </button>
      </aside>

      <main className="flex-1 pb-16 sm:pb-0">
        <Outlet />
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-40 flex border-t border-ink-200 bg-white sm:hidden">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn(
                'flex flex-1 flex-col items-center gap-0.5 py-2 text-[11px] font-medium',
                isActive ? 'text-brand-700' : 'text-ink-400',
              )
            }
          >
            <Icon className="size-5" />
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
