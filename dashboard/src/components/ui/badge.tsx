import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'
import { cn } from '@/lib/utils'

// Status tones mirror the semantic map: green/amber/red/gray/blue -- see index.css @theme.
const badgeVariants = cva('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium', {
  variants: {
    tone: {
      neutral: 'bg-ink-100 text-ink-700',
      green: 'bg-status-green-100 text-status-green-500',
      amber: 'bg-status-amber-100 text-status-amber-500',
      red: 'bg-status-red-100 text-status-red-500',
      gray: 'bg-status-gray-100 text-status-gray-500',
      blue: 'bg-status-blue-100 text-status-blue-500',
    },
  },
  defaultVariants: { tone: 'neutral' },
})

function Badge({ className, tone, ...props }: React.ComponentProps<'span'> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ tone, className }))} {...props} />
}

export { Badge, badgeVariants }
