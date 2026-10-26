import { Link } from "react-router-dom"
import { ArrowRight, FileWarning, ListChecks, LockOpen } from "lucide-react"

import { ApiError } from "@/lib/api"
import { longDate, shortDate } from "@/lib/format"
import type { Line } from "@/lib/types"
import { Amount } from "@/components/Amount"
import { ErrorState } from "@/components/states"

/**
 * What blocks a statement replacement, shown as actionable links rather than a bare error.
 *
 * Replacement refusals are rarely the reviewer's mistake.  They reflect the state of the books (a booked line,
 * a closed month, a document already loaded), and each has something specific to go and do.  The raw message,
 * e.g. "immutable: 2 line(s) of statement stm_… are still booked", names a state but not the lines, and gives
 * the reviewer nothing to click.
 *
 * So each refusal is rendered with links built from the ids in its `details`.  The service still decides
 * whether a replacement is allowed; nothing here does.
 *
 * The same rendering is used at two points, which is why it lives outside the form:
 *
 * 1. before the attempt, derived from the statement's lines, so the screen says up front what has to happen
 *    first; and
 * 2. after a refusal, from its `details`, for when somebody booked a line in another window while the form was
 *    open.
 */

/** A booked statement line, which blocks replacing the statement. */
export interface BookedLine {
  lineId: string
  entryId: string | null
  /** The line itself, when the page has it loaded.  A refusal only carries ids. */
  line: Line | null
}

/** The booked lines of a statement, from the lines the page already loaded. */
export function bookedLinesOf(lines: Line[]): BookedLine[] {
  return lines
    .filter((line) => line.status === "booked" || line.explanation?.kind === "booked")
    .map((line) => ({
      lineId: line.line_id,
      entryId: line.explanation?.entry_id ?? null,
      line,
    }))
}

/** The same list, read from a refusal's details and matched against the lines the page holds. */
export function bookedLinesOfRefusal(error: ApiError, lines: Line[]): BookedLine[] {
  const byId = new Map(lines.map((line) => [line.line_id, line]))
  return error.details
    .filter((detail) => typeof detail.line_id === "string")
    .map((detail) => ({
      lineId: detail.line_id as string,
      entryId: typeof detail.entry_id === "string" ? detail.entry_id : null,
      line: byId.get(detail.line_id as string) ?? null,
    }))
}

/**
 * The booked lines, each with links to undo it.
 *
 * Uses the attention tone, not the destructive one.  These are ordinary, correct bookings that just need
 * undoing before the replacement; drawing a to-do list in red teaches people to ignore red.
 */
export function BookedLines({
  entity,
  booked,
  precision,
}: {
  entity: string
  booked: BookedLine[]
  precision: number
}) {
  if (booked.length === 0) return null
  return (
    <section className="rounded-xl border border-attention-border bg-attention-surface">
      <div className="flex gap-3 px-5 py-4">
        <ListChecks className="mt-0.5 size-4 shrink-0 text-attention" />
        <div className="space-y-1">
          <h3 className="font-medium">
            {booked.length === 1
              ? "One line of this statement is still booked"
              : `${booked.length} lines of this statement are still booked`}
          </h3>
          <p className="text-sm text-muted-foreground">
            A replacement retires the explanations written against the lines it supersedes. The{" "}
            {booked.length === 1 ? "entry" : "entries"} below would be left resting on a statement that is no
            longer there, so the service refuses the replacement while {booked.length === 1 ? "it" : "they"}{" "}
            stand. Undo {booked.length === 1 ? "it" : "them"} first — re-explain each line, or exclude it — and
            this statement becomes replaceable.
          </p>
        </div>
      </div>
      <ul className="border-t">
        {booked.map((item) => (
          <li key={item.lineId} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b px-5 py-3 last:border-0">
            {item.line ? (
              <>
                <span className="tnum text-sm text-muted-foreground">{shortDate(item.line.dt, true)}</span>
                <span className="min-w-0 flex-1 truncate text-sm">{item.line.description}</span>
                <Amount
                  micros={item.line.amount_micros}
                  precision={precision}
                  commodity={item.line.commodity}
                />
              </>
            ) : (
              /* A line this page hasn't loaded (booked elsewhere while the screen was open).  Only the id is
                 known, which is still enough to link to it. */
              <span className="min-w-0 flex-1 truncate font-mono text-xs">{item.lineId}</span>
            )}
            <span className="flex shrink-0 gap-3 text-sm">
              <Link
                to={`/e/${entity}/lines/${item.lineId}`}
                className="inline-flex items-center gap-1 underline underline-offset-4"
              >
                Open the line
                <ArrowRight className="size-3.5" />
              </Link>
              {item.entryId ? (
                <Link
                  to={`/e/${entity}/entries/${item.entryId}`}
                  className="inline-flex items-center gap-1 text-muted-foreground underline underline-offset-4"
                >
                  The entry it posted
                </Link>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * A replacement refusal, rendered according to what it's about.
 *
 * Every branch keeps the service's own message and adds the links its `details` allow.  An unrecognised code
 * falls through to the ordinary `ErrorState`, so a new refusal never loses its message.
 */
export function ReplacementRefusal({
  entity,
  error,
  lines,
  precision,
}: {
  entity: string
  error: unknown
  lines: Line[]
  precision: number
}) {
  if (!(error instanceof ApiError)) return <ErrorState error={error} />

  // `immutable` is also the code for a document size mismatch, which has no lines to undo, so branch on the
  // details as well as the code.
  const booked = bookedLinesOfRefusal(error, lines)
  if (error.code === "immutable" && booked.length > 0) {
    return <BookedLines entity={entity} booked={booked} precision={precision} />
  }

  if (error.code === "document_already_loaded") {
    const loaded = (error.details[0]?.statement_ids as string[] | undefined) ?? []
    const filename = error.details[0]?.filename as string | undefined
    return (
      <Blocked title="These bytes are already loaded as a statement" icon={<FileWarning className="size-4" />}>
        <p>{error.message}</p>
        <p>
          A document is identified by its contents, and one document is one live statement — so the same file
          cannot stand behind two statements at once, whichever accounts they are for. Replacing{" "}
          {loaded.length === 1 ? "that statement" : "one of those statements"} with a corrected reading of these
          same bytes is allowed, and is the ordinary way to fix a parse that went wrong.
        </p>
        {loaded.length > 0 ? (
          <ul className="space-y-1">
            {loaded.map((id) => (
              <li key={id}>
                <Link
                  to={`/e/${entity}/statements?statement=${id}`}
                  className="inline-flex items-center gap-1 underline underline-offset-4"
                >
                  {filename ? `${filename} is loaded as this statement` : "The statement it is loaded as"}
                  <ArrowRight className="size-3.5" />
                </Link>
              </li>
            ))}
          </ul>
        ) : null}
      </Blocked>
    )
  }

  if (error.code === "period_locked") {
    const through = error.details[0]?.locked_through as string | undefined
    return (
      <Blocked title="The books are closed over this statement" icon={<LockOpen className="size-4" />}>
        <p>{error.message}</p>
        <p>
          Nothing is undone by reopening: the close and the snapshot it was taken against stay exactly where
          they are, and closing again afterwards records a second, separate close.
        </p>
        <Link
          to={`/e/${entity}/close`}
          className="inline-flex items-center gap-1 underline underline-offset-4"
        >
          {through ? `Reopen the period, closed through ${longDate(through)}` : "Reopen the period"}
          <ArrowRight className="size-3.5" />
        </Link>
      </Blocked>
    )
  }

  if (error.code === "already_superseded" || error.code === "period_overlap") {
    const other = (error.details[0]?.replaced_by_id ?? error.details[0]?.statement_id) as string | undefined
    return (
      <Blocked title={error.code === "already_superseded" ? "This statement has already been replaced" : "Another statement already covers these days"}>
        <p>{error.message}</p>
        {other ? (
          <Link
            to={`/e/${entity}/statements?statement=${other}`}
            className="inline-flex items-center gap-1 underline underline-offset-4"
          >
            Go to that statement
            <ArrowRight className="size-3.5" />
          </Link>
        ) : null}
      </Blocked>
    )
  }

  return <ErrorState error={error} />
}

/** Shared layout for the actionable refusals. */
function Blocked({
  title,
  icon,
  children,
}: {
  title: string
  icon?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section
      role="alert"
      className="space-y-2 rounded-xl border border-attention-border bg-attention-surface px-5 py-4 text-sm"
    >
      <div className="flex items-center gap-2">
        {icon ? <span className="text-attention">{icon}</span> : null}
        <h3 className="font-medium">{title}</h3>
      </div>
      <div className="space-y-2 text-muted-foreground [&_a]:text-foreground">{children}</div>
    </section>
  )
}
