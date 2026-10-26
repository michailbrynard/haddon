import { Link, useParams, useSearchParams } from "react-router-dom"
import { ArrowRight, FileClock, Landmark, Lock, ReceiptText, Waves } from "lucide-react"
import type { ReactNode } from "react"

import {
  useCloseChecks,
  useEntity,
  useEntityRead,
  useOpenLines,
  useOpenUnrecoveredTax,
  usePrecision,
  useProvisional,
  useRecentEvents,
} from "@/lib/queries"
import { longDate, pluralise, shortDate, timestamp } from "@/lib/format"
import { today } from "@/lib/period"
import type { AuditEvent, CloseCheck } from "@/lib/types"
import { Amount } from "@/components/Amount"
import { actionTitle } from "@/components/AuditTimeline"
import { ErrorState, LoadingRows } from "@/components/states"

/** Rows a register shows before it says how many more there are. */
const SHOWN = 6

/** How many log events "recently changed" reads. */
const RECENT = 12

/**
 * The review inbox: everything unfinished in the books, on one screen.
 *
 * It stores nothing.  There's no inbox table, task, assignment or "done"; each register is an existing read
 * without a month bound.  The service already knows what is unexplained, awaiting a document, unrecovered or
 * blocking a close.  A separate table of inbox items could disagree with those answers.
 *
 * Not on the month page because most of this isn't tied to a month: a line unexplained in January is still
 * unexplained in March, and close checks cover the whole span a close would newly shut.  Showing them under
 * "March" risks closing a period with January's problems still in it.
 *
 * Each register is shown only if the service publishes its read (`useEntityRead`).  With reconciliation off,
 * the unexplained-lines register is simply absent: no error, no misleading empty state, no 404.  Likewise
 * for installations without the tax or expectation extensions.
 *
 * Every row links on to the line, entry, account or document.  Links are router paths, never service links
 * (a `/v1` link would arrive without the user header; see `uiHref`).
 */
export function Review() {
  const { entity = "" } = useParams()
  const [params, setParams] = useSearchParams()
  // same default and query parameter as the close page, so following the link keeps the same date
  const through = params.get("through") ?? today()

  const servesLines = useEntityRead(entity, "/lines")
  const servesProvisional = useEntityRead(entity, "/provisional")
  const servesTax = useEntityRead(entity, "/unrecovered-tax")
  const servesChecks = useEntityRead(entity, "/close-checks")
  const servesEvents = useEntityRead(entity, "/events")

  return (
    <div className="space-y-6" data-testid="review-inbox">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Inbox</h2>
        <p className="mt-0.5 text-sm text-muted-foreground">
          What in these books is unexplained, waiting on evidence, or standing between them and a close.
          Nothing here is a copy — each register is the service's own answer, asked now.
        </p>
      </div>

      {servesLines ? <OpenLines entity={entity} /> : null}
      {servesProvisional ? <AwaitingDocuments entity={entity} /> : null}
      {servesTax ? <UnrecoveredTax entity={entity} /> : null}
      {servesChecks ? <BlockingTheClose entity={entity} through={through} onThrough={(d) => setParams({ through: d })} /> : null}
      {servesEvents ? <RecentlyChanged entity={entity} /> : null}
    </div>
  )
}

/**
 * One register: a heading with the count, the first few rows, and a link to the rest.
 *
 * The count is in the heading ("4 lines are unexplained") rather than a badge.  An empty register says so in
 * the same place.  A register that is absent means the service doesn't serve that read, which is different.
 */
function Register({
  icon,
  title,
  blurb,
  count,
  settled,
  children,
  footer,
}: {
  icon: ReactNode
  title: string
  blurb: string
  count?: number
  settled?: boolean
  children: ReactNode
  footer?: ReactNode
}) {
  return (
    <section className="rounded-xl border bg-card">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 border-b px-5 py-3.5">
        <span className="text-muted-foreground">{icon}</span>
        <h3 className="font-medium">{title}</h3>
        {count === undefined ? null : (
          <span
            className={
              settled
                ? "rounded-full bg-settled-surface px-2 py-0.5 text-xs text-muted-foreground"
                : "rounded-full bg-attention-surface px-2 py-0.5 text-xs font-medium tnum"
            }
          >
            {count}
          </span>
        )}
        <p className="w-full text-sm text-muted-foreground">{blurb}</p>
      </div>
      <div className="px-5 py-3.5">{children}</div>
      {footer ? <div className="border-t px-5 py-2.5 text-sm">{footer}</div> : null}
    </section>
  )
}

/** Shown when a register has nothing outstanding, rather than hiding the register. */
function Settled({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>
}

function More({ hidden, to, label }: { hidden: number; to: string; label: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 underline-offset-4 hover:underline">
      {hidden > 0 ? `${label} — ${hidden} more not shown` : label}
      <ArrowRight className="size-3.5" />
    </Link>
  )
}

/**
 * Statement lines not yet accounted for, across all statements and months.
 *
 * `partly_explained` is counted together with `unexplained`: both are money on a statement the books don't
 * fully account for.  Each row says which it is.
 */
function OpenLines({ entity }: { entity: string }) {
  const lines = useOpenLines(entity)
  const precisionOf = usePrecision(entity)

  if (lines.isPending) return <LoadingRows rows={4} />
  if (lines.error) return <ErrorState error={lines.error} />

  const items = lines.data ?? []
  return (
    <Register
      icon={<Waves className="size-4" />}
      title="Unexplained on a statement"
      blurb="Money that moved through an account these books have not yet said anything about."
      count={items.length}
      settled={items.length === 0}
      footer={
        items.length > 0 ? (
          <More hidden={items.length - SHOWN} to={`/e/${entity}/month`} label="Work them in the month review" />
        ) : null
      }
    >
      {items.length === 0 ? (
        <Settled>Every statement line is accounted for.</Settled>
      ) : (
        <ul className="divide-y">
          {items.slice(0, SHOWN).map((line) => (
            <li key={line.line_id} className="flex items-baseline justify-between gap-4 py-2 first:pt-0 last:pb-0">
              <div className="min-w-0">
                <Link
                  to={`/e/${entity}/lines/${line.line_id}`}
                  className="text-sm underline-offset-4 hover:underline"
                >
                  {line.description}
                </Link>
                <p className="text-xs text-muted-foreground">
                  {shortDate(line.dt, true)}
                  {line.reference ? ` · ${line.reference}` : ""}
                  {line.status === "partly_explained" ? " · partly explained" : ""}
                </p>
              </div>
              <Amount
                micros={line.status === "partly_explained" ? line.unexplained_micros : line.amount_micros}
                precision={precisionOf(line.commodity)}
                commodity={line.commodity}
              />
            </li>
          ))}
        </ul>
      )}
    </Register>
  )
}

/**
 * Posted bookings waiting on a document that could still change them.
 *
 * They count in every report; what's outstanding is the evidence, not the booking.  Hence "waiting on" rather
 * than wording that suggests the figures are provisional.
 */
function AwaitingDocuments({ entity }: { entity: string }) {
  const provisional = useProvisional(entity)

  if (provisional.isPending) return <LoadingRows rows={3} />
  if (provisional.error) return <ErrorState error={provisional.error} />

  const items = provisional.data ?? []
  return (
    <Register
      icon={<FileClock className="size-4" />}
      title="Waiting on a document"
      blurb="Posted bookings that named a document which has not arrived. They count in the reports; the evidence is what is missing."
      count={items.length}
      settled={items.length === 0}
      footer={items.length > SHOWN ? <More hidden={items.length - SHOWN} to={`/e/${entity}/month`} label="Review the month they sit in" /> : null}
    >
      {items.length === 0 ? (
        <Settled>Nothing is waiting on a document.</Settled>
      ) : (
        <ul className="divide-y">
          {items.slice(0, SHOWN).map((item) => (
            <li key={item.expectation.id} className="py-2 first:pt-0 last:pb-0">
              <Link
                to={`/e/${entity}/entries/${item.entry.id}`}
                className="text-sm underline-offset-4 hover:underline"
              >
                {item.entry.narration}
              </Link>
              <p className="text-xs text-muted-foreground">
                {shortDate(item.entry.dt, true)} · awaiting {item.expectation.awaiting}
              </p>
              {item.expectation.reason ? (
                <p className="mt-0.5 text-sm text-muted-foreground">{item.expectation.reason}</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Register>
  )
}

/**
 * Recoverable tax not recovered, with a total per commodity.
 *
 * Totals are never summed across commodities, matching the service; a sum over two currencies means nothing.
 */
function UnrecoveredTax({ entity }: { entity: string }) {
  const tax = useOpenUnrecoveredTax(entity)
  const precisionOf = usePrecision(entity)

  if (tax.isPending) return <LoadingRows rows={3} />
  if (tax.error) return <ErrorState error={tax.error} />

  const items = tax.data?.items ?? []
  const totals = Object.entries(tax.data?.totals ?? {})
  return (
    <Register
      icon={<ReceiptText className="size-4" />}
      title="Recoverable tax not recovered"
      blurb="Tax these books could have reclaimed and did not, with the reason recorded against each."
      count={items.length}
      settled={items.length === 0}
    >
      {items.length === 0 ? (
        <Settled>No recoverable tax is outstanding.</Settled>
      ) : (
        <>
          <ul className="divide-y">
            {items.slice(0, SHOWN).map((item) => (
              <li key={item.record.id} className="flex items-baseline justify-between gap-4 py-2 first:pt-0 last:pb-0">
                <div className="min-w-0">
                  <Link
                    to={`/e/${entity}/entries/${item.record.entry_id}`}
                    className="text-sm underline-offset-4 hover:underline"
                  >
                    {item.entry.narration}
                  </Link>
                  <p className="text-xs text-muted-foreground">
                    {shortDate(item.entry.dt, true)}
                    {item.record.tax_code ? ` · ${item.record.tax_code}` : ""}
                    {item.record.recoverable_until ? ` · recoverable until ${shortDate(item.record.recoverable_until, true)}` : ""}
                  </p>
                  <p className="mt-0.5 text-sm text-muted-foreground">{item.record.reason}</p>
                </div>
                <Amount
                  micros={item.record.amount_micros}
                  precision={precisionOf(item.record.commodity)}
                  commodity={item.record.commodity}
                />
              </li>
            ))}
          </ul>
          {totals.length > 0 ? (
            <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 border-t pt-3 text-sm">
              {totals.map(([commodity, micros]) => (
                <div key={commodity} className="flex items-baseline gap-2">
                  <dt className="text-muted-foreground">Total {commodity}</dt>
                  <dd>
                    <Amount micros={micros} precision={precisionOf(commodity)} commodity={commodity} emphasis />
                  </dd>
                </div>
              ))}
            </dl>
          ) : null}
        </>
      )}
    </Register>
  )
}

/**
 * What blocks closing the period, taken from the close checks.
 *
 * Uses each blocking check's `outstanding[]` as served (with its truncation count).  Rebuilding it from the
 * underlying records would be a second implementation of the close policy.
 *
 * Failing checks that don't block aren't shown, since they don't stand in the way.
 */
function BlockingTheClose({
  entity,
  through,
  onThrough,
}: {
  entity: string
  through: string
  onThrough: (date: string) => void
}) {
  const listing = useEntity(entity)
  const lockThrough = listing.data?.lock_through ?? null
  // For a date at or before the lock, the service refuses to run checks (`close must advance the lock`).
  // Don't ask; report the lock instead.  Otherwise closing the period on screen would show a validation error.
  //
  // Wait for the listing to settle, not just load.  The lock comes from the entity listing, and while it
  // refetches after a close `data` still holds the old lock, so asking then would get the same 422.
  const settling = listing.isPending || listing.isFetching
  const shut = lockThrough !== null && through <= lockThrough
  const checks = useCloseChecks(entity, settling || shut ? "" : through)

  if (settling || (!shut && checks.isPending)) return <LoadingRows rows={3} />
  if (!shut && checks.error) return <ErrorState error={checks.error} />

  if (shut && lockThrough) {
    return (
      <Register
        icon={<Lock className="size-4" />}
        title={`Blocking a close through ${longDate(through)}`}
        blurb="Nothing, because this period is already shut."
        count={0}
        settled
        footer={<ThroughPicker entity={entity} through={through} onThrough={onThrough} blocking={0} />}
      >
        <Settled>
          These books are closed through {longDate(lockThrough)}. Pick a later date to see what a further
          close would have to pass.
        </Settled>
      </Register>
    )
  }

  const blocking = (checks.data?.checks ?? []).filter((c) => c.blocking)
  return (
    <Register
      icon={<Lock className="size-4" />}
      title={`Blocking a close through ${longDate(through)}`}
      blurb={
        checks.data
          ? `Over ${longDate(checks.data.window_start)} to ${longDate(through)} — ${pluralise(checks.data.months.length, "month")} this close would newly shut.`
          : "The checks a close has to pass, as the service evaluates them."
      }
      count={blocking.length}
      settled={blocking.length === 0}
      footer={<ThroughPicker entity={entity} through={through} onThrough={onThrough} blocking={blocking.length} />}
    >
      {blocking.length === 0 ? (
        <Settled>
          {checks.data?.eligible
            ? "Every check passes, so this period would close."
            : "No check is blocking a close through this date."}
        </Settled>
      ) : (
        <ul className="space-y-3">
          {blocking.map((check) => (
            <BlockingCheck key={check.check} check={check} />
          ))}
        </ul>
      )}
    </Register>
  )
}

/** Footer for this register: a link to the close page, and the date the checks run through. */
function ThroughPicker({
  entity,
  through,
  onThrough,
  blocking,
}: {
  entity: string
  through: string
  onThrough: (date: string) => void
  blocking: number
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <Link
        to={`/e/${entity}/close?through=${through}`}
        className="inline-flex items-center gap-1 underline-offset-4 hover:underline"
      >
        {blocking > 0 ? "Answer these on the closing page" : "Go to closing"}
        <ArrowRight className="size-3.5" />
      </Link>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        Checked through
        <input
          type="date"
          value={through}
          max={today()}
          onChange={(e) => e.target.value && onThrough(e.target.value)}
          className="h-8 rounded-md border bg-transparent px-2 text-sm tnum"
          aria-label="Date the close checks are run through"
        />
      </label>
    </div>
  )
}

/** One blocking check with its outstanding items. */
function BlockingCheck({ check }: { check: CloseCheck }) {
  const shown = check.outstanding.slice(0, SHOWN)
  const hidden = check.outstanding.length - shown.length + check.outstanding_truncated
  return (
    <li>
      <p className="text-sm font-medium">{check.title}</p>
      <p className="text-sm text-muted-foreground">{check.reason}</p>
      {shown.length > 0 ? (
        <ul className="mt-1 space-y-0.5">
          {shown.map((item, i) => (
            <li key={`${item.kind}-${i}`} className="text-sm text-muted-foreground">
              · {item.message}
            </li>
          ))}
          {hidden > 0 ? (
            <li className="text-xs text-muted-foreground">and {hidden} more</li>
          ) : null}
        </ul>
      ) : null}
    </li>
  )
}

/** The router path for a recorded subject, if it has a page. */
function subjectPath(entity: string, event: AuditEvent): string | null {
  switch (event.subject_kind) {
    case "entry":
      return `/e/${entity}/entries/${event.subject_id}`
    case "statement_line":
      return `/e/${entity}/lines/${event.subject_id}`
    case "account":
      return `/e/${entity}/accounts/${event.subject_id}`
    default:
      return null
  }
}

/**
 * The most recent events in the books, newest first.
 *
 * The only register not about something being wrong.  Nothing else in the UI shows what changed recently; a
 * correction somebody else posted is invisible elsewhere unless you already know which entry to open.
 *
 * The log is only served forward from a sequence, so reading its tail takes two requests
 * (`api.recentEvents`) rather than reading the whole history.
 */
function RecentlyChanged({ entity }: { entity: string }) {
  const events = useRecentEvents(entity, RECENT)

  if (events.isPending) return <LoadingRows rows={4} />
  if (events.error) return <ErrorState error={events.error} />

  const items = events.data?.items ?? []
  return (
    <Register
      icon={<Landmark className="size-4" />}
      title="Recently changed"
      blurb="The last few things recorded against these books, whoever recorded them."
    >
      {items.length === 0 ? (
        <Settled>Nothing has been recorded against these books yet.</Settled>
      ) : (
        <ul className="divide-y">
          {items.slice(0, SHOWN).map((event) => {
            const to = subjectPath(entity, event)
            return (
              <li key={event.seq} className="py-2 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  {to ? (
                    <Link to={to} className="text-sm font-medium underline-offset-4 hover:underline">
                      {actionTitle(event.kind)}
                    </Link>
                  ) : (
                    <span className="text-sm font-medium">{actionTitle(event.kind)}</span>
                  )}
                  <span className="text-xs text-muted-foreground">
                    {event.created_by} · {timestamp(event.created_at)}
                  </span>
                </div>
                {event.reason ? (
                  <p className="mt-0.5 text-sm text-muted-foreground">{event.reason}</p>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </Register>
  )
}
