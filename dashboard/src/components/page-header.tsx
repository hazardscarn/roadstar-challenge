export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between border-b border-ink-200 bg-white px-6 py-4">
      <div>
        <h1 className="font-display text-lg font-bold text-ink-900">{title}</h1>
        {description && <p className="text-sm text-ink-500">{description}</p>}
      </div>
      {actions}
    </div>
  )
}
