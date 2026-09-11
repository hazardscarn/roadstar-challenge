// Real user feedback: a selected trip's map/detail needs a green/amber/red read on whether it's
// running early, on schedule, or late -- compares now() against the ETA captured at assignment
// time (live.trips.eta). This is an interim signal (a real system would recompute ETA from live
// remaining distance/time, which needs the full telemetry loop already driving movement here) --
// good enough to be honest and useful today, not fabricated precision.
export type ScheduleStatus = 'on_time' | 'at_risk' | 'late'

export function scheduleStatus(eta: string | null): ScheduleStatus | null {
  if (!eta) return null
  const diffMin = (new Date(eta).getTime() - Date.now()) / 60000
  if (diffMin < 0) return 'late'
  if (diffMin < 15) return 'at_risk'
  return 'on_time'
}

export const SCHEDULE_COLOR: Record<ScheduleStatus, string> = {
  on_time: '#1f9d55',
  at_risk: '#e0940f',
  late: '#d9342b',
}

export const SCHEDULE_LABEL: Record<ScheduleStatus, string> = {
  on_time: 'On schedule',
  at_risk: 'Arriving soon — tight',
  late: 'Running late',
}
