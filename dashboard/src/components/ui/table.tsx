import * as React from 'react'
import { cn } from '@/lib/utils'

// Plain styled primitives -- TanStack Table supplies the logic (sort/filter/paginate), these
// just give every data grid in the app the same look (see plan: "one grid component").
function Table({ className, ...props }: React.ComponentProps<'table'>) {
  return (
    <div className="w-full overflow-auto rounded-lg border border-ink-200">
      <table className={cn('w-full caption-bottom text-sm', className)} {...props} />
    </div>
  )
}
function TableHeader({ className, ...props }: React.ComponentProps<'thead'>) {
  return <thead className={cn('bg-ink-50 [&_tr]:border-b', className)} {...props} />
}
function TableBody({ className, ...props }: React.ComponentProps<'tbody'>) {
  return <tbody className={cn('[&_tr:last-child]:border-0', className)} {...props} />
}
function TableRow({ className, ...props }: React.ComponentProps<'tr'>) {
  return <tr className={cn('border-b border-ink-100 transition-colors hover:bg-ink-50/70', className)} {...props} />
}
function TableHead({ className, ...props }: React.ComponentProps<'th'>) {
  return (
    <th
      className={cn('h-9 px-3 text-left align-middle text-xs font-semibold text-ink-500 uppercase tracking-wide', className)}
      {...props}
    />
  )
}
function TableCell({ className, ...props }: React.ComponentProps<'td'>) {
  return <td className={cn('px-3 py-2 align-middle', className)} {...props} />
}

export { Table, TableHeader, TableBody, TableRow, TableHead, TableCell }
