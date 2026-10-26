import { Link, useParams, useSearchParams } from "react-router-dom"
import { ArrowRight, ArrowUpRight, CornerDownRight, FileText, History } from "lucide-react"

import {
  useCommand,
  usePrecision,
  useReconciliationEnabled,
  useStatement,
  useStatementLines,
  useStatements,
} from "@/lib/queries"
import { longDate, pluralise, shortDate, timestamp } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { Line, LineStatus, Statement } from "@/lib/types"
import { Amount } from "@/components/Amount"
import { Evidence } from "@/components/Evidence"
import { ReplaceStatement } from "@/components/ReplaceStatement"
import { BookedLines, bookedLinesOf } from "@/components/StatementBlockers"
import { EmptyState, ErrorState, LoadingRows } from "@/components/states"

/**
 * Loaded statements and their replacement chains.
 *
 * Its own page rather than part of the month review because a statement's history isn't tied to a month.  The
 * usual reason to replace one is that it declared the wrong period, so the original and its correction sit in
 * different months.  The month review links here for the statements behind its figures.
 *
 * The selection is in the query string (`?statement=…`), like `?month=` and `?through=`, so a pasted link
 * reopens the page at that statement and chain links navigate within the page.
 *
 * `replace_statement` has been published since issue 0006 but had no UI until this page (one of the gaps
 * issue 0007 lists).  Its refusals reflect the state of the books rather than form mistakes, so much of the
 * work here is turning each into something to act on; see `components/StatementBlockers.tsx`.
 */

/** Labels for each line status, matching the wording used elsewhere in the UI. */
const STATUS: Record<LineStatus, string> = {
  unexplained: "Needs an account",
  partly_explained: "Partly explained",
  booked: "Booked",
  linked: "Linked to a posting already in the books",
  ignored: "Not booked, on purpose",
  superseded_input: "From a statement that has been replaced",
}

export function Statements() {
  const { entity = "" } = useParams()
  const [params, setParams] = useSearchParams()
  const selectedId = params.get("statement")
  const reconciles = useReconciliationEnabled(entity)
  const statements = useStatements(entity)

  const select = (id: string | null) => setParams(id === null ? {} : { statement: id })

  if (reconciles === false) {
    return (
      <EmptyState title="Reconciliation is switched off here" icon={<FileText className="size-6" strokeWidth={1.5} />}>
        This installation runs with <code>HADDON_RECONCILIATION=off</code>, so it loads no bank statements and
        there is nothing here to replace. The journal, the reports and the close are unaffected.
      </EmptyState>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Statements</h2>
        <p className="mt-0.5 text-sm text-muted-foreground">
          Every statement these books have been read off, including the ones that have since been replaced. A
          statement read wrongly is restated, never edited: both readings stay, and the later one names the
          earlier.
        </p>
      </div>

      {statements.isPending ? (
        <LoadingRows rows={5} />
      ) : statements.error ? (
        <ErrorState error={statements.error} />
      ) : statements.data?.length ? (
        <StatementList
          entity={entity}
          statements={statements.data}
          selectedId={selectedId}
          onSelect={select}
        />
      ) : (
        <EmptyState title="No statement has been loaded yet">
          Until one is, there is nothing to compare the books against for any cash account.
        </EmptyState>
      )}

      {selectedId ? <Selected entity={entity} statementId={selectedId} onSelect={select} /> : null}
    </div>
  )
}

/** Every statement, newest period first, with whether each is live or replaced. */
function StatementList({
  entity,
  statements,
  selectedId,
  onSelect,
}: {
  entity: string
  statements: Statement[]
  selectedId: string | null
  onSelect: (id: string | null) => void
}) {
  const precisionOf = usePrecision(entity)
  const ordered = [...statements].sort(
    (a, b) => b.period_end.localeCompare(a.period_end) || a.account_path.localeCompare(b.account_path),
  )
  return (
    <section className="overflow-x-auto rounded-xl border bg-card">
      <table className="w-full text-sm">
        <caption className="sr-only">Loaded statements</caption>
        <thead>
          <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="px-5 py-2.5 font-medium">Period</th>
            <th scope="col" className="px-5 py-2.5 font-medium">Account</th>
            <th scope="col" className="px-5 py-2.5 text-right font-medium">Closing</th>
            <th scope="col" className="px-5 py-2.5 font-medium">Lines</th>
            <th scope="col" className="px-5 py-2.5 font-medium">Standing</th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((statement) => (
            <tr
              key={statement.statement_id}
              className={cn(
                "cursor-pointer border-b last:border-0 hover:bg-muted/40",
                statement.statement_id === selectedId && "bg-muted/60",
                !statement.live && "text-muted-foreground",
              )}
              onClick={() => onSelect(statement.statement_id)}
            >
              <td className="px-5 py-2.5">
                <button type="button" className="text-left underline-offset-4 hover:underline">
                  {shortDate(statement.period_start, true)} to {shortDate(statement.period_end, true)}
                </button>
              </td>
              <td className="px-5 py-2.5">{statement.account_path}</td>
              <td className="px-5 py-2.5 text-right">
                <Amount
                  micros={statement.closing_micros}
                  precision={precisionOf(statement.commodity)}
                  commodity={statement.commodity}
                />
              </td>
              <td className="px-5 py-2.5 tnum">{statement.line_count}</td>
              <td className="px-5 py-2.5">
                {statement.live ? (
                  <span className="text-muted-foreground">
                    {statement.replaces_id ? "Live — replaced an earlier reading" : "Live"}
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 rounded-md border border-attention-border bg-attention/10 px-2 py-0.5 text-xs font-medium text-attention">
                    <History className="size-3" />
                    Replaced
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

/** One statement: its figures, document, replacement chain, and whether it can be replaced. */
function Selected({
  entity,
  statementId,
  onSelect,
}: {
  entity: string
  statementId: string
  onSelect: (id: string | null) => void
}) {
  const statement = useStatement(entity, statementId)
  const lines = useStatementLines(entity, statementId)
  const canReplace = useCommand(entity, "replace_statement")
  const precisionOf = usePrecision(entity)

  if (statement.isPending) return <LoadingRows rows={6} />
  if (statement.error) return <ErrorState error={statement.error} />
  if (!statement.data) return null

  const s = statement.data
  const precision = precisionOf(s.commodity)
  const loaded = lines.data ?? []
  const booked = bookedLinesOf(loaded)

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold tracking-tight">
            {s.account_path} · {shortDate(s.period_start, true)} to {shortDate(s.period_end, true)}
          </h3>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Loaded by {s.created_by} on {timestamp(s.created_at)} · {pluralise(s.line_count, "line")} ·{" "}
            <Link
              to={`/e/${entity}/month?month=${s.period_end.slice(0, 7)}`}
              className="underline underline-offset-4"
            >
              the month it belongs to
            </Link>
          </p>
          {s.reason ? <p className="mt-1 text-sm">{s.reason}</p> : null}
        </div>
      </div>

      <Balances statement={s} precision={precision} />

      <Chain entity={entity} statement={s} onSelect={onSelect} />

      <section className="space-y-2">
        <h4 className="text-sm font-semibold">The document it was read off</h4>
        <Evidence
          entity={entity}
          documentId={s.document_id}
          document={s.document}
          page={s.page}
          locator={s.locator}
        />
      </section>

      <section className="space-y-2">
        <h4 className="text-sm font-semibold">What was read, and what became of it</h4>
        {lines.isPending ? (
          <LoadingRows rows={4} />
        ) : lines.error ? (
          <ErrorState error={lines.error} />
        ) : (
          <StatementLines entity={entity} lines={loaded} precision={precision} />
        )}
      </section>

      {/* Blockers come before the form, so a reviewer who can't replace this statement yet finds out without
          filling it in. */}
      {!s.live ? (
        <p className="text-sm text-muted-foreground">
          This statement has been replaced, so it is no longer what the books read for this period and it
          cannot be replaced again. Its successor is above.
        </p>
      ) : booked.length > 0 ? (
        <BookedLines entity={entity} booked={booked} precision={precision} />
      ) : canReplace === false ? (
        <p className="text-sm text-muted-foreground">
          This credential can read this statement but cannot replace it.
        </p>
      ) : canReplace === true && !lines.isPending ? (
        /* Keyed on the statement so selecting another (or the replacement just written) remounts the form.
           Its fields are seeded at mount, so otherwise they'd keep the previous statement's lines. */
        <ReplaceStatement
          key={s.statement_id}
          entity={entity}
          statement={s}
          lines={loaded}
          precision={precision}
          onReplaced={(replacement) => onSelect(replacement.statement_id)}
        />
      ) : null}
    </div>
  )
}

/**
 * What the statement declares, and whether its opening was checked against a prior closing.
 *
 * `opening_verified` is shown explicitly.  An unverified opening isn't a defect (the first statement for an
 * account has nothing before it), but it shouldn't look the same as a verified one.  It's never defaulted to
 * zero.
 */
function Balances({ statement, precision }: { statement: Statement; precision: number }) {
  const basis =
    statement.opening_basis === "prior_statement_closing"
      ? "It agrees with the closing balance of the statement ending the day before."
      : statement.opening_basis === "first_statement_for_this_account_and_commodity"
        ? "This is the first statement loaded for this account, so there is nothing on record to check it against."
        : "No statement ends the day before this period, so there is nothing on record to check it against."
  return (
    <section className="rounded-xl border bg-card px-5 py-4">
      <dl className="grid gap-4 sm:grid-cols-3">
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">Opening</dt>
          <dd className="mt-0.5">
            <Amount micros={statement.opening_micros} precision={precision} commodity={statement.commodity} emphasis />
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">Closing</dt>
          <dd className="mt-0.5">
            <Amount micros={statement.closing_micros} precision={precision} commodity={statement.commodity} emphasis />
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted-foreground">The lines sum to</dt>
          <dd className="mt-0.5">
            <Amount micros={statement.lines_micros} precision={precision} commodity={statement.commodity} emphasis />
          </dd>
        </div>
      </dl>
      <p className="mt-3 text-xs text-muted-foreground">
        {statement.opening_verified ? "The opening balance is verified. " : "The opening balance is not verified. "}
        {basis}
      </p>
    </section>
  )
}

/**
 * The replacement chain, in both directions.
 *
 * Forwards is at most one hop (the database allows one successor), so it's rendered flat.  Backwards can be
 * any length: a statement read wrongly twice has two predecessors.  Each link fetches its statement, so this
 * is a recursive component rather than a loop, since hooks can't be called in a loop.
 *
 * The depth limit isn't a cycle guard (`replaces_id` is UNIQUE and points strictly back in time, so there
 * can't be one).  It stops the page firing off dozens of requests; past it, the link can still be followed.
 */
function Chain({
  entity,
  statement,
  onSelect,
}: {
  entity: string
  statement: Statement
  onSelect: (id: string) => void
}) {
  if (!statement.replaces_id && !statement.replaced_by_id) return null
  return (
    <section className="space-y-2">
      <h4 className="text-sm font-semibold">What this statement supersedes, and what superseded it</h4>
      {statement.replaced_by_id ? (
        <div className="rounded-xl border border-attention-border bg-attention-surface px-5 py-3.5 text-sm">
          <p className="font-medium">This reading was replaced.</p>
          <p className="mt-0.5 text-muted-foreground">
            It stays in the record exactly as it was written, and the books read its successor for these days.
          </p>
          <button
            type="button"
            onClick={() => onSelect(statement.replaced_by_id as string)}
            className="mt-1 inline-flex items-center gap-1 underline underline-offset-4"
          >
            The statement that replaced it
            <ArrowRight className="size-3.5" />
          </button>
        </div>
      ) : null}
      {statement.replaces_id ? (
        <Predecessor entity={entity} statementId={statement.replaces_id} depth={0} onSelect={onSelect} />
      ) : null}
    </section>
  )
}

const MAX_CHAIN = 6

function Predecessor({
  entity,
  statementId,
  depth,
  onSelect,
}: {
  entity: string
  statementId: string
  depth: number
  onSelect: (id: string) => void
}) {
  const { data, isPending, error } = useStatement(entity, statementId)
  const precisionOf = usePrecision(entity)

  if (isPending) return <LoadingRows rows={1} />
  // Show the service's error rather than silently leaving a gap in the chain.
  if (error) return <ErrorState error={error} />
  if (!data) return null

  return (
    <>
      <div className="rounded-xl border px-5 py-3.5">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <CornerDownRight className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="text-sm font-medium">Replaces</span>
          <button
            type="button"
            onClick={() => onSelect(data.statement_id)}
            className="inline-flex items-center gap-1 text-sm underline underline-offset-4"
          >
            {shortDate(data.period_start, true)} to {shortDate(data.period_end, true)}
            <ArrowUpRight className="size-3.5" />
          </button>
          <span className="text-xs text-muted-foreground">
            {data.created_by} · {timestamp(data.created_at)}
          </span>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          Read as {pluralise(data.line_count, "line")} closing at{" "}
          <Amount
            micros={data.closing_micros}
            precision={precisionOf(data.commodity)}
            commodity={data.commodity}
          />
          , off {data.document.filename}.
        </p>
        {data.reason ? <p className="mt-0.5 text-sm">{data.reason}</p> : null}
      </div>
      {data.replaces_id ? (
        depth + 1 < MAX_CHAIN ? (
          <Predecessor
            entity={entity}
            statementId={data.replaces_id}
            depth={depth + 1}
            onSelect={onSelect}
          />
        ) : (
          <button
            type="button"
            onClick={() => onSelect(data.replaces_id as string)}
            className="text-sm text-muted-foreground underline underline-offset-4"
          >
            This one replaced an earlier reading too
          </button>
        )
      ) : null}
    </>
  )
}

/** The statement's lines in document order, each with its status. */
function StatementLines({
  entity,
  lines,
  precision,
}: {
  entity: string
  lines: Line[]
  precision: number
}) {
  if (lines.length === 0) {
    return <p className="text-sm text-muted-foreground">This statement has no lines.</p>
  }
  return (
    <div className="overflow-x-auto rounded-xl border bg-card">
      <table className="w-full text-sm">
        <caption className="sr-only">The lines of this statement</caption>
        <thead>
          <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="px-5 py-2.5 font-medium">Date</th>
            <th scope="col" className="px-5 py-2.5 font-medium">Description</th>
            <th scope="col" className="px-5 py-2.5 text-right font-medium">Amount</th>
            <th scope="col" className="px-5 py-2.5 font-medium">What became of it</th>
          </tr>
        </thead>
        <tbody>
          {[...lines]
            .sort((a, b) => a.line_no - b.line_no)
            .map((line) => (
              <tr key={line.line_id} className="border-b last:border-0">
                <td className="px-5 py-2.5 tnum">{shortDate(line.dt, true)}</td>
                <td className="px-5 py-2.5">
                  <Link
                    to={`/e/${entity}/lines/${line.line_id}`}
                    className="underline-offset-4 hover:underline"
                  >
                    {line.description}
                  </Link>
                  {line.reference ? (
                    <span className="ml-2 text-xs text-muted-foreground">{line.reference}</span>
                  ) : null}
                </td>
                <td className="px-5 py-2.5 text-right">
                  <Amount micros={line.amount_micros} precision={precision} commodity={line.commodity} />
                </td>
                <td className="px-5 py-2.5">
                  <span className={cn("text-sm", line.status === "booked" && "font-medium")}>
                    {STATUS[line.status] ?? line.status}
                  </span>
                  {line.explanation?.entry_id ? (
                    <>
                      {" "}
                      <Link
                        to={`/e/${entity}/entries/${line.explanation.entry_id}`}
                        className="text-sm underline underline-offset-4"
                      >
                        the entry
                      </Link>
                    </>
                  ) : null}
                </td>
              </tr>
            ))}
        </tbody>
      </table>
      <p className="border-t px-5 py-2 text-xs text-muted-foreground">
        {longDate(lines[0].dt)} onwards, in the order the document printed them.
      </p>
    </div>
  )
}
