import { ChevronRight } from "lucide-react"

import { timestamp } from "@/lib/format"
import type { AuditEvent } from "@/lib/types"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"

/**
 * A short title for each recorded event kind.
 *
 * The log stores stable keys (`entry.corrected`) and the UI does the wording, as on every other page.
 *
 * An unmapped kind falls back to the key itself rather than something generic like "an event occurred", so a
 * newly added action is visible and can be looked up.
 */
const ACTIONS: Record<string, string> = {
  "entry.posted": "Posted",
  "entry.reversed": "Reversed",
  "entry.corrected": "Corrected",
  "entry.awaiting_document": "Marked as awaiting a document",
  "entry.document_arrived": "The document arrived",
  "entry.document_not_expected": "No document is coming",
  "line.booked": "Booked from a statement line",
  "line.linked": "Linked to an existing posting",
  "line.ignored": "Excluded from the books",
  "line.reexplained": "Re-explained",
  "account.created": "Account added to the chart",
  "account.archived": "Account retired",
  "account.restored": "Account put back in use",
  "statement.loaded": "Statement loaded",
  "statement.replaced": "Statement replaced",
  "period.closed": "Period closed",
  "period.reopened": "Period reopened",
  "tax.unrecovered": "Recoverable tax recorded as not recovered",
  "tax.recovered": "Unrecovered tax resolved",
  "entity.initialized": "Entity opened",
}

export function actionTitle(kind: string): string {
  return ACTIONS[kind] ?? kind
}

/**
 * What happened to this entry, in order, with who did it and why.
 *
 * Lead with the business action and keep technical ids behind a toggle (issue 0007, pitfall 3).  A reviewer
 * wants one sentence and an actor, not a sequence number, subject kind and request id.
 *
 * The actor is shown as recorded.  The system doesn't model what kind of caller it was, so the UI doesn't
 * either.
 *
 * Events whose subject is a correction of this entry are included (the service returns them); without them a
 * reversed entry would look untouched.
 */
export function AuditTimeline({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">Nothing has been recorded against this yet.</p>
  }
  return (
    <ol className="space-y-0">
      {events.map((event, i) => (
        <li key={event.seq} className="relative flex gap-3 pb-4 last:pb-0">
          {/* vertical line between dots, omitted after the last one */}
          {i < events.length - 1 ? (
            <span className="absolute left-[3.5px] top-3 h-full w-px bg-border" aria-hidden="true" />
          ) : null}
          <span className="relative mt-1.5 size-2 shrink-0 rounded-full bg-muted-foreground/60" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-sm font-medium">{actionTitle(event.kind)}</span>
              <span className="text-xs text-muted-foreground">
                {event.created_by} · {timestamp(event.created_at)}
              </span>
            </div>
            {event.reason ? <p className="mt-0.5 text-sm text-muted-foreground">{event.reason}</p> : null}
            <Collapsible>
              <CollapsibleTrigger className="group mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                <ChevronRight className="size-3 transition-transform group-data-[state=open]:rotate-90" />
                Record
              </CollapsibleTrigger>
              <CollapsibleContent>
                <dl className="mt-1 grid grid-cols-[7rem_1fr] gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                  <dt>Sequence</dt>
                  <dd className="tnum">{event.seq}</dd>
                  <dt>Action</dt>
                  <dd className="font-mono">{event.kind}</dd>
                  <dt>Subject</dt>
                  <dd className="break-all font-mono">
                    {event.subject_kind} {event.subject_id}
                  </dd>
                  {event.request_id ? (
                    <>
                      <dt>Request</dt>
                      <dd className="break-all font-mono">{event.request_id}</dd>
                    </>
                  ) : null}
                </dl>
              </CollapsibleContent>
            </Collapsible>
          </div>
        </li>
      ))}
    </ol>
  )
}
