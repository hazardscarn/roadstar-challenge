import { PageHeader } from '@/components/page-header'

/** Placeholder for a screen not yet built in this phase of the build plan -- real content lands
 * in a later phase per /home/davidacad10/.claude/plans/encapsulated-swinging-naur.md. */
export function ComingSoon({ title, description, phase }: { title: string; description: string; phase: string }) {
  return (
    <div>
      <PageHeader title={title} description={description} />
      <div className="flex flex-col items-center justify-center gap-2 px-6 py-24 text-center">
        <p className="text-sm font-medium text-ink-500">Built in {phase}</p>
        <p className="max-w-md text-xs text-ink-400">Scaffolding is live; this screen's data wiring comes next in the build order.</p>
      </div>
    </div>
  )
}
