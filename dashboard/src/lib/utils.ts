import { type ClassValue, clsx } from 'clsx'
import { format as dateFnsFormat } from 'date-fns'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// Real user-found bug: every simulation timestamp (sim_start, order pickup/delivery times, trip
// log entries -- anything built as sim_start + timedelta(...) in sim/live/ai_dispatch_replay.py)
// is constructed as a "naive local business-hour clock, tagged UTC" -- e.g. "3 AM" is stored as
// "...T03:00:00+00:00", a literal (incorrect) UTC claim. date-fns' plain `format()` always reads
// the BROWSER's real local timezone, so on a real Eastern-timezone machine it correctly converts
// that (mislabeled) UTC instant back 4 hours -- showing "11 PM the previous night" for an intended
// "3 AM," which is exactly the "sim clock shows midnight" bug. Fixing the root generation-side
// convention (real UTC, not mislabeled local) would touch dozens of places across the whole app
// and re-derive/re-verify this session's backtest numbers against it -- too large a change for
// the time available. This instead cancels the browser's OWN conversion before formatting, so the
// RAW stored clock digits are shown as-is (a well-known, dependency-free trick: shift the instant
// forward by the local UTC offset, so local-timezone formatting reads back the original numbers).
// Only for simulation-derived timestamps -- do NOT use this on a genuinely-correct UTC timestamp
// (e.g. a row's real `created_at` from `now()`), which needs real conversion, not cancellation.
export function formatSimTime(date: Date | string, formatStr: string): string {
  const d = typeof date === 'string' ? new Date(date) : date
  const shifted = new Date(d.getTime() + d.getTimezoneOffset() * 60000)
  return dateFnsFormat(shifted, formatStr)
}
