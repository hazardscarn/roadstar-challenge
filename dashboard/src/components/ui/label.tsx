import * as LabelPrimitive from '@radix-ui/react-label'
import * as React from 'react'
import { cn } from '@/lib/utils'

function Label({ className, ...props }: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      className={cn('text-xs font-medium text-ink-600 select-none', className)}
      {...props}
    />
  )
}

export { Label }
