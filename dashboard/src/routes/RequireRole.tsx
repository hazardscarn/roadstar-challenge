import * as React from 'react'
import { Navigate } from 'react-router-dom'
import type { Role } from '@/lib/supabase'
import { useAuth } from '@/lib/auth-context'

/** Client-side route gating -- convenience only. The real boundary is RLS (sim/sql/009_rls.sql,
 * 031_add_ui_support_columns.sql): every live.* query is scoped server-side by role/driver_id
 * regardless of what renders here. See research/roadstar_platform_plan.md Section 8.3. */
export function RequireRole({ role, children }: { role: Role; children: React.ReactNode }) {
  const { session, profile, loading } = useAuth()

  if (loading) return <FullScreenLoading />
  if (!session) return <Navigate to="/" replace />
  if (!profile) return <FullScreenLoading />
  if (profile.role !== role) return <Navigate to={profile.role === 'manager' ? '/manager' : '/driver'} replace />
  return <>{children}</>
}

function FullScreenLoading() {
  return (
    <div className="flex min-h-screen items-center justify-center text-sm text-ink-500">
      Loading…
    </div>
  )
}
