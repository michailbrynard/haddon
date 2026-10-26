import { Link, Navigate } from "react-router-dom"

import { useEntities } from "@/lib/queries"
import { EmptyState, ErrorState, LoadingRows } from "@/components/states"

/** The landing page.  With one entity it redirects straight to it; with several it lists them. */
export function Entities() {
  const { data, isPending, error } = useEntities()

  if (isPending) {
    return (
      <div className="mx-auto max-w-2xl px-5 py-16">
        <LoadingRows rows={3} />
      </div>
    )
  }
  if (error) {
    return (
      <div className="mx-auto max-w-2xl px-5 py-16">
        <ErrorState error={error} />
      </div>
    )
  }
  if (data.length === 1) return <Navigate to={`/e/${data[0].entity_id}/month`} replace />

  return (
    <div className="mx-auto max-w-2xl px-5 py-16">
      <h1 className="text-2xl font-semibold tracking-tight">Haddon</h1>
      <p className="mt-1 text-sm text-muted-foreground">Choose a set of books.</p>
      <div className="mt-6 space-y-2">
        {data.length === 0 ? (
          <EmptyState title="No books are configured on this service" />
        ) : (
          data.map((e) => (
            <Link
              key={e.entity_id}
              to={`/e/${e.entity_id}/month`}
              className="block rounded-xl border bg-card px-5 py-4 transition-colors hover:border-primary/40 hover:bg-accent/40"
            >
              <div className="font-medium">{e.legal_name}</div>
              <div className="mt-0.5 text-sm text-muted-foreground">
                {e.legal_form} · {e.country} · {e.base_currency}
              </div>
            </Link>
          ))
        )}
      </div>
    </div>
  )
}
