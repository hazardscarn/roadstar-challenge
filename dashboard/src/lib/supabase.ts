import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined

if (!url || !anonKey) {
  throw new Error(
    'VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY missing -- check the repo-root .env (dashboard/ reads it via vite.config.ts envDir).',
  )
}

// The anon key + RLS is the ONLY thing the browser ever talks to Supabase with -- every
// driver/manager data boundary is enforced by the policies in sim/sql/009_rls.sql +
// 031_add_ui_support_columns.sql, not by anything client-side. See research/
// roadstar_platform_plan.md Section 8.3.
export const supabase = createClient(url, anonKey)

export type Role = 'manager' | 'driver'

export interface Profile {
  user_id: string
  role: Role
  driver_id: number | null
}
