import type { ReactNode } from "react"
import { AlertCircle, Inbox } from "lucide-react"

import { ApiError } from "@/lib/api"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

/** Skeleton rows shown while a page loads, instead of a spinner. */
export function LoadingRows({ rows = 6, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-2", className)} aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading</span>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-11 w-full" style={{ opacity: 1 - i * 0.1 }} />
      ))}
    </div>
  )
}

export function EmptyState({
  title,
  children,
  icon,
}: {
  title: string
  children?: ReactNode
  icon?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-12 text-center">
      <div className="text-muted-foreground">{icon ?? <Inbox className="size-6" strokeWidth={1.5} />}</div>
      <p className="font-medium">{title}</p>
      {children ? <div className="max-w-sm text-sm text-muted-foreground">{children}</div> : null}
    </div>
  )
}

/** An error, in the service's own words.
 *
 * The message and first detail come straight from the error envelope, not paraphrased, because they say what
 * was refused and why.  The request id is shown small; it's what finds the event in the log.
 */
export function ErrorState({ error, className }: { error: unknown; className?: string }) {
  const api = error instanceof ApiError ? error : null
  const message = api ? api.summary : error instanceof Error ? error.message : "Something went wrong."
  return (
    <div
      role="alert"
      className={cn(
        "rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-foreground",
        className,
      )}
    >
      <div className="flex gap-2.5">
        <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
        <div className="space-y-1">
          <p>{message}</p>
          {api?.requestId ? (
            <p className="font-mono text-xs text-muted-foreground">{api.requestId}</p>
          ) : null}
        </div>
      </div>
    </div>
  )
}
