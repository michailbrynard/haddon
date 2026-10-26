import { useMemo, useState } from "react"
import { useNavigate, useParams, useSearchParams } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { Check, ChevronLeft, ChevronRight, Clock, ExternalLink, FileText, Plus } from "lucide-react"
import { toast } from "sonner"

import { api } from "@/lib/api"
import {
  useEntity,
  useMonthLines,
  usePrecision,
  useProvisional,
  useReconciliation,
  useReconciliationEnabled,
  useStatements,
  useEntityMutation,
} from "@/lib/queries"
import {
  addMicros,
  amount,
  isZero,
  longDate,
  magnitude,
  monthBounds,
  monthTitle,
  negate,
  pluralise,
  shiftMonth,
  shortDate,
} from "@/lib/format"
import type { Line, ProvisionalItem, ReconciliationAccount, Statement } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Amount } from "@/components/Amount"
import { BookingDialog, ReasonDialog } from "@/components/BookingDialog"
import { EmptyState, ErrorState, LoadingRows } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

/** Line statuses where no posted entry covers the line. */
const OUTSTANDING_STATES = ["unexplained", "partly_explained", "ignored"]

function needsAttention(line: Line): boolean {
  return line.status === "unexplained" || line.status === "partly_explained"
}

function MonthNav({ entity, month }: { entity: string; month: string }) {
  const navigate = useNavigate()
  const go = (next: string) => navigate(`/e/${entity}/month?month=${next}`)
  const options = useMemo(() => {
    const year = Number(month.slice(0, 4))
    return Array.from({ length: 12 }, (_, i) => `${year}-${String(i + 1).padStart(2, "0")}`)
  }, [month])

  return (
    <div className="flex items-center gap-1">
      <Button variant="outline" size="icon" aria-label="Previous month" onClick={() => go(shiftMonth(month, -1))}>
        <ChevronLeft className="size-4" />
      </Button>
      <Select value={month} onValueChange={go}>
        <SelectTrigger className="w-[11rem]" aria-label="Month">
          <SelectValue />
        </SelectTrigger>
        <SelectContent className="max-h-80">
          {options.map((m) => (
            <SelectItem key={m} value={m}>
              {monthTitle(m)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button variant="outline" size="icon" aria-label="Next month" onClick={() => go(shiftMonth(month, 1))}>
        <ChevronRight className="size-4" />
      </Button>
    </div>
  )
}

function Figure({
  label,
  value,
  note,
  tone = "plain",
}: {
  label: string
  value: string
  note?: string
  tone?: "plain" | "attention" | "settled"
}) {
  return (
    <div className="min-w-0">
      <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</div>
      <div
        className={cn(
          "tnum mt-0.5 truncate text-xl font-semibold tracking-tight",
          tone === "attention" && "text-attention",
          tone === "settled" && "text-settled",
        )}
      >
        {value}
      </div>
      {note ? <div className="mt-0.5 truncate text-xs text-muted-foreground">{note}</div> : null}
    </div>
  )
}

function Todo({
  text,
  detail,
  action,
}: {
  text: string
  detail?: string
  action?: { label: string; onClick: () => void }
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2.5">
      <div className="min-w-0">
        <p className="text-sm font-medium">{text}</p>
        {detail ? <p className="mt-0.5 text-sm text-muted-foreground">{detail}</p> : null}
      </div>
      {action ? (
        <Button variant="outline" size="sm" className="shrink-0" onClick={action.onClick}>
          {action.label}
        </Button>
      ) : null}
    </div>
  )
}

/** The month summary: does the month hold together, and if not, what to do about it.
 *
 * Two numbers and a short list.  It doesn't name close checks or eligibility rules (the service still
 * enforces them), because "nothing_partial is unevaluable" isn't actionable and makes an unfinished month
 * look broken.  Every item is something that can be done in a click or two.
 */
function Comparison({
  account,
  end,
  precision,
  labelled,
}: {
  account: ReconciliationAccount
  end: string
  precision: number
  labelled: boolean
}) {
  const difference = account.difference_micros
  return (
    <div>
      {labelled ? (
        <div className="mb-1.5 text-xs text-muted-foreground">{account.account_path}</div>
      ) : null}
      <div className="grid grid-cols-2 gap-6 sm:grid-cols-3">
        <Figure
          label="Bank"
          value={`${amount(account.statement_micros, precision)} ${account.commodity}`}
          note={`at ${longDate(end)}`}
        />
        <Figure
          label="Books"
          value={`${amount(account.ledger_micros, precision)} ${account.commodity}`}
          note={labelled ? undefined : account.account_path}
        />
        {difference !== null && !isZero(difference) ? (
          <Figure
            label="Difference"
            tone="attention"
            value={`${magnitude(difference, precision)} ${account.commodity}`}
            note={BigInt(difference) > 0n ? "the books hold more" : "the bank holds more"}
          />
        ) : (
          <Figure label="Difference" tone="settled" value="None" note="they agree to the cent" />
        )}
      </div>
    </div>
  )
}

function Standing({
  entity,
  month,
  lines,
  accounts,
  outstanding,
  statements,
  provisional,
  onReview,
}: {
  entity: string
  month: string
  lines: Line[]
  accounts: ReconciliationAccount[]
  outstanding: Line[]
  statements: Statement[]
  provisional: ProvisionalItem[]
  onReview: () => void
}) {
  const precisionOf = usePrecision(entity)
  const { end } = monthBounds(month)
  // Only accounts with a loaded statement can be compared.  The rest show up in the "no statement covers ..."
  // item below.
  const compared = accounts.filter((a) => a.statement_micros !== null)

  const unexplained = lines.filter((l) => l.status === "unexplained")
  const partial = lines.filter((l) => l.status === "partly_explained")

  // Split provisional bookings into this month's and earlier ones, so a count about this month isn't mixed
  // with older items.
  const { start: monthStart } = monthBounds(month)
  const provisionalHere = provisional.filter((p) => p.entry.dt >= monthStart && p.entry.dt <= end)
  const provisionalEarlier = provisional.filter((p) => p.entry.dt < monthStart).length

  // Only say "they come to exactly the difference" when it can be checked: with exactly one compared account.
  // With two, the arithmetic would be about one account but the sentence would read as covering both.
  const difference = compared.length === 1 ? compared[0].difference_micros : null
  const open = outstanding.filter((l) => l.account_id === compared[0]?.account_id)
  const gap = negate(addMicros(open.map((l) => l.unexplained_micros)))
  const gapExplainsDifference =
    difference !== null && !isZero(difference) && BigInt(gap) === BigInt(difference)
  // The difference is cumulative, so lines from earlier months can be part of it.  Count them so the sentence
  // doesn't attribute the whole difference to this month's lines.
  const earlier = open.filter((l) => l.dt < monthBounds(month).start).length

  const items: { text: string; detail?: string; action?: { label: string; onClick: () => void } }[] = []
  if (unexplained.length) {
    items.push({
      text: `${pluralise(unexplained.length, "line")} still ${unexplained.length === 1 ? "needs" : "need"} an account`,
      detail: !gapExplainsDifference
        ? "Money moved in the bank that the books have nothing for yet."
        : earlier > 0
          ? `With ${pluralise(earlier, "line")} still open from earlier months, they come to exactly the difference above.`
          : unexplained.length === 1
            ? "It comes to exactly the difference above."
            : "Together they come to exactly the difference above.",
      action: { label: "Show them", onClick: onReview },
    })
  }
  if (partial.length) {
    items.push({
      text: `${pluralise(partial.length, "line is", "lines are")} only part explained`,
      detail: "Some of the amount is matched and the rest is not.",
      action: { label: "Show them", onClick: onReview },
    })
  }
  // Bookings awaiting a document are listed here too, not just in the panel below, because the month isn't
  // finished while one is waiting.  The detail names what is awaited only when there is exactly one.
  if (provisionalHere.length) {
    items.push({
      text: `${pluralise(provisionalHere.length, "booking is", "bookings are")} waiting on a document`,
      detail:
        provisionalHere.length === 1
          ? `Waiting for ${provisionalHere[0].expectation.awaiting}.`
          : "Each stands at its best figure until the document that settles it arrives.",
    })
  }
  for (const a of accounts) {
    for (const gapSpan of a.uncovered) {
      items.push({
        text: `No statement covers ${shortDate(gapSpan.start)} to ${shortDate(gapSpan.end)}`,
        detail: `${a.account_path} — load it and the rest of this page becomes trustworthy.`,
      })
    }
  }

  const settled = items.length === 0 && compared.length > 0 && compared.every((a) => a.agrees)

  return (
    <>
      <section
        className={cn(
          "overflow-hidden rounded-xl border bg-card",
          settled ? "border-settled-border" : "border-attention-border",
        )}
      >
        <div className="px-5 py-4">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "flex size-5 items-center justify-center rounded-full",
                settled ? "bg-settled/15 text-settled" : "bg-attention/15 text-attention",
              )}
              aria-hidden
            >
              {settled ? <Check className="size-3.5" /> : <span className="size-1.5 rounded-full bg-current" />}
            </span>
            <h2 className="text-sm font-semibold">
              {settled
                ? `${monthTitle(month)} is booked and agrees with the bank`
                : items.length
                  ? `${pluralise(items.length, "thing needs", "things need")} your attention`
                  : "The books do not agree with the bank"}
            </h2>
          </div>

          {compared.length ? (
            <div className="mt-4 space-y-5">
              {compared.map((a) => (
                <Comparison
                  key={`${a.account_id}-${a.commodity}`}
                  account={a}
                  end={end}
                  precision={precisionOf(a.commodity)}
                  labelled={compared.length > 1}
                />
              ))}
            </div>
          ) : (
            <p className="mt-3 text-sm text-muted-foreground">
              No statement is loaded for this month, so there is nothing to compare the books against yet.
            </p>
          )}
        </div>

        {items.length ? (
          <div className="border-t border-attention-border bg-attention-surface/70 px-5 py-1.5">
            {items.map((item, i) => (
              <Todo key={i} {...item} />
            ))}
          </div>
        ) : null}

        {provisional.length ? (
          <Provisional entity={entity} items={provisional} earlier={provisionalEarlier} />
        ) : null}
        {statements.length ? <Provenance entity={entity} statements={statements} /> : null}
      </section>

    </>
  )
}

/** Posted bookings waiting on a document that may change them
 * (docs/design/decisions.md, "Booked, awaiting a document").
 *
 * Collapsed like the statement list, showing the count up front.  The one action available per row is saying
 * that no document is coming, with a reason.  Filing the document itself isn't done from the browser.
 */
function Provisional({
  entity,
  items,
  earlier,
}: {
  entity: string
  items: ProvisionalItem[]
  earlier: number
}) {
  const precisionOf = usePrecision(entity)
  const [withdrawing, setWithdrawing] = useState<ProvisionalItem | null>(null)
  const withdraw = useEntityMutation(entity, ({ id, reason }: { id: string; reason: string }) =>
    api.withdrawExpectation(entity, id, { reason }),
  )

  // The count covers the whole list, so the note says how much is from earlier months.  When the only booking is
  // from an earlier month, say "from an earlier month", not "one from an earlier month", which reads as two.
  const here = items.length - earlier
  const note =
    earlier === 0
      ? null
      : here === 0
        ? earlier === 1
          ? "from an earlier month"
          : "all from earlier months"
        : earlier === 1
          ? "one from an earlier month"
          : `${earlier} from earlier months`

  return (
    <>
      <Collapsible>
        <CollapsibleTrigger className="flex w-full items-center gap-2 border-t px-5 py-2.5 text-left text-xs text-muted-foreground transition-colors hover:text-foreground">
          <Clock className="size-3.5" />
          {items.length === 1
            ? "One booking is waiting on a document"
            : `${items.length} bookings are waiting on a document`}
          {note ? <span className="text-muted-foreground/70">· {note}</span> : null}
        </CollapsibleTrigger>
        <CollapsibleContent className="border-t bg-muted/30 px-5 py-3">
          <ul className="space-y-3 text-xs">
            {items.map((item) => (
              // Stacked on a phone, side by side from `sm` up; at phone width the button would squeeze the text.
              <li
                key={item.expectation.id}
                className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-4"
              >
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-foreground">
                    {shortDate(item.entry.dt, true)} · {item.entry.narration}
                  </div>
                  <div className="text-muted-foreground">Waiting for {item.expectation.awaiting}</div>
                  <div className="mt-0.5 text-muted-foreground/80">{item.expectation.reason}</div>
                  <div className="mt-0.5 tnum text-muted-foreground/70">
                    {item.entry.postings
                      .filter((p) => BigInt(p.amount_micros) > 0n)
                      .map((p) => `${amount(p.amount_micros, precisionOf(p.commodity))} ${p.commodity}`)
                      .join(" / ")}{" "}
                    · recorded by {item.expectation.created_by}
                  </div>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  className="shrink-0 self-start"
                  onClick={() => setWithdrawing(item)}
                >
                  No document is coming
                </Button>
              </li>
            ))}
          </ul>
        </CollapsibleContent>
      </Collapsible>

      <ReasonDialog
        open={Boolean(withdrawing)}
        onOpenChange={(open) => !open && setWithdrawing(null)}
        title="No document is coming"
        description="The booking stands exactly as it is. What changes is that it stops being listed as waiting."
        label="Why is none expected?"
        confirmLabel="Record it"
        onConfirm={async (reason) => {
          if (!withdrawing) return
          await withdraw.mutateAsync({ id: withdrawing.entry.id, reason })
          toast.success("Recorded", { description: "It is no longer waiting on anything." })
        }}
      />
    </>
  )
}

/** The statements behind the figures, collapsed by default. */
function Provenance({ entity, statements }: { entity: string; statements: Statement[] }) {
  return (
    <Collapsible>
      <CollapsibleTrigger className="flex w-full items-center gap-2 border-t px-5 py-2.5 text-left text-xs text-muted-foreground transition-colors hover:text-foreground">
        <FileText className="size-3.5" />
        {statements.length === 1
          ? "The statement behind these figures"
          : `The ${statements.length} statements behind these figures`}
      </CollapsibleTrigger>
      <CollapsibleContent className="border-t bg-muted/30 px-5 py-3">
        <ul className="space-y-3 text-xs">
          {statements.map((s) => (
            /* Keyed on `statement_id`; statements have no `id` field. */
            <li key={s.statement_id}>
              {/* Links to the statement page, where it can also be replaced. */}
              <a
                href={`/e/${entity}/statements?statement=${s.statement_id}`}
                className="font-medium text-foreground underline underline-offset-4"
              >
                {s.document.filename}
              </a>
              <div className="text-muted-foreground">
                {s.account_path} · {shortDate(s.period_start, true)} to {shortDate(s.period_end, true)} ·{" "}
                {pluralise(s.line_count, "line")} · loaded by {s.created_by}
              </div>
              {s.locator ? <div className="mt-0.5 text-muted-foreground/80">{s.locator}</div> : null}
              <div className="mt-0.5 font-mono text-[11px] text-muted-foreground/70">{s.document.sha256}</div>
            </li>
          ))}
        </ul>
      </CollapsibleContent>
    </Collapsible>
  )
}

function BookedCell({ line, precision }: { line: Line; precision: number }) {
  const standing = line.entries.find((e) => e.status === "posted" && !e.reversed_by)
  const draft = line.entries.find((e) => e.status === "draft")
  const entry = draft ?? standing
  // The booked entry was corrected separately; what stands now is its replacement.
  const corrected =
    !draft && standing && line.explanation?.entry_id && standing.id !== line.explanation.entry_id

  if (line.explanation?.kind === "ignored") {
    return (
      <div className="min-w-0">
        <div className="truncate text-sm text-muted-foreground">Not booked, on purpose</div>
        <div className="truncate text-xs text-muted-foreground/80">{line.explanation.reason}</div>
      </div>
    )
  }
  if (!entry) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-md border border-attention-border bg-attention/10 px-2 py-1 text-sm font-medium text-attention">
        <Plus className="size-3.5" />
        Give it an account
      </span>
    )
  }
  const contra = entry.postings.filter((p) => p.account_id !== line.account_id)
  return (
    <div className="min-w-0">
      <div className="flex items-baseline gap-2">
        <span className="truncate text-sm">{entry.narration}</span>
        {draft ? (
          <span className="shrink-0 rounded border border-attention-border bg-attention/10 px-1.5 py-px text-[11px] font-medium text-attention">
            not posted
          </span>
        ) : null}
        {corrected ? (
          <span className="shrink-0 rounded border px-1.5 py-px text-[11px] font-medium text-muted-foreground">
            corrected
          </span>
        ) : null}
      </div>
      <div className="truncate text-xs text-muted-foreground" title={contra.map((p) => p.account_path).join(", ")}>
        {contra.map((p) => p.account_path).join(", ")}
        {contra.length > 1 ? (
          <span className="ml-1 tnum">
            ({contra.map((p) => amount(p.amount_micros, precision)).join(" / ")})
          </span>
        ) : null}
      </div>
    </div>
  )
}

function LineRow({
  entity,
  line,
  precision,
  onOpen,
  onEdit,
  onExclude,
  onUndo,
}: {
  entity: string
  line: Line
  precision: number
  /** Open the line's read-only view. */
  onOpen: () => void
  /** Open straight into the form, from a menu item that says so. */
  onEdit: () => void
  onExclude: () => void
  /** Undo a booking: re-explain the line as excluded, which reverses the entry it posted. */
  onUndo: () => void
}) {
  const attention = needsAttention(line)

  return (
    <tr
      className={cn(
        "group border-b last:border-b-0",
        attention ? "bg-attention-surface/60" : "hover:bg-muted/40",
      )}
    >
      <td className="relative py-3 pl-4 pr-3 align-top">
        {attention ? (
          <span className="absolute inset-y-0 left-0 w-[3px] bg-attention" aria-hidden />
        ) : null}
        <span className="tnum whitespace-nowrap text-sm text-muted-foreground">{shortDate(line.dt)}</span>
      </td>
      <td className="max-w-0 py-3 pr-3 align-top">
        <div className="line-clamp-2 text-sm" title={line.description}>
          {line.description}
        </div>
        {line.reference ? (
          <div className="truncate font-mono text-xs text-muted-foreground">{line.reference}</div>
        ) : null}
      </td>
      <td className="py-3 pr-6 text-right align-top">
        <Amount micros={line.amount_micros} precision={precision} emphasis />
      </td>
      <td className="max-w-0 py-3 pr-3 align-top">
        <button
          type="button"
          onClick={onOpen}
          aria-label={`What ${line.description} is booked to`}
          className="w-full rounded text-left transition-colors hover:text-primary focus-visible:text-primary"
        >
          <BookedCell line={line} precision={precision} />
        </button>
      </td>
      <td className="w-10 py-3 pr-3 text-right align-top">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="size-7 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"
              aria-label={`Actions for ${line.description}`}
            >
              <span aria-hidden>⋯</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={onEdit}>
              {line.entries.length ? "Change the booking" : "Give it an account"}
            </DropdownMenuItem>
            {line.explanation?.kind === "booked" ? (
              <DropdownMenuItem onSelect={onUndo}>Undo the booking</DropdownMenuItem>
            ) : null}
            {!line.explanation ? (
              <DropdownMenuItem onSelect={onExclude}>Not ours to book</DropdownMenuItem>
            ) : null}
            <DropdownMenuItem asChild>
              <a href={`/e/${entity}/lines/${line.line_id}`}>
                Where did this come from?
                <ExternalLink className="ml-auto size-3.5 opacity-60" />
              </a>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </td>
    </tr>
  )
}

export function Month() {
  const { entity = "" } = useParams()
  const [params] = useSearchParams()
  const { data: summary } = useEntity(entity)
  const statementsQuery = useStatements(entity)

  const fallbackMonth = useMemo(() => {
    const latest = statementsQuery.data?.reduce<string | null>(
      (best, s) => (best === null || s.period_end > best ? s.period_end : best),
      null,
    )
    if (latest) return latest.slice(0, 7)
    return `${new Date().getFullYear()}-01`
  }, [statementsQuery.data])

  const month = params.get("month") ?? fallbackMonth
  const { start, end } = monthBounds(month)
  const [onlyAttention, setOnlyAttention] = useState(false)
  const [opened, setOpened] = useState<{ line: Line; editing: boolean } | null>(null)
  const [excluding, setExcluding] = useState<Line | null>(null)
  const [undoing, setUndoing] = useState<Line | null>(null)

  const linesQuery = useMonthLines(entity, month)
  const reconciliation = useReconciliation(entity, month)
  const provisional = useProvisional(entity)
  const precisionOf = usePrecision(entity)

  const reconciles = useReconciliationEnabled(entity)
  const outstanding = useQuery({
    queryKey: ["entity", entity, "outstanding", end],
    queryFn: () => api.lines(entity, { to: end, state: OUTSTANDING_STATES }),
    enabled: Boolean(entity && month) && reconciles === true,
  })

  const excludeLine = useEntityMutation(entity, ({ id, reason }: { id: string; reason: string }) =>
    api.ignore(entity, id, { reason }),
  )
  const undoBooking = useEntityMutation(entity, ({ id, reason }: { id: string; reason: string }) =>
    api.reexplain(entity, id, { kind: "ignored", reason }),
  )

  const lines = linesQuery.data ?? []
  const attentionCount = lines.filter(needsAttention).length
  const shown = onlyAttention ? lines.filter(needsAttention) : lines

  const statements = (statementsQuery.data ?? []).filter(
    (s) => s.period_start <= end && s.period_end >= start,
  )
  const cashAccounts = reconciliation.data?.accounts ?? []
  const compared = cashAccounts.find((a) => a.statement_micros !== null)
  const precision = precisionOf(compared?.commodity ?? summary?.base_currency ?? "")

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">{monthTitle(month)}</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {longDate(start)} to {longDate(end)}
          </p>
        </div>
        <MonthNav entity={entity} month={month} />
      </div>

      {reconciles === false ? (
        <EmptyState title="Reconciliation is switched off here" icon={<FileText className="size-6" strokeWidth={1.5} />}>
          This installation runs with <code>HADDON_RECONCILIATION=off</code>, so it loads no bank statements and
          nothing on this page compares the books against one. The journal, the reports and the close are
          unaffected.
        </EmptyState>
      ) : null}

      {reconciliation.error ? <ErrorState error={reconciliation.error} /> : null}

      {reconciles === false ? null : reconciliation.isPending || linesQuery.isPending ? (
        <LoadingRows rows={3} />
      ) : (
        <Standing
          entity={entity}
          month={month}
          lines={lines}
          accounts={cashAccounts}
          outstanding={outstanding.data ?? []}
          statements={statements}
          provisional={provisional.data ?? []}
          onReview={() => setOnlyAttention(true)}
        />
      )}

      <section className={cn("space-y-3", reconciles === false && "hidden")}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="text-sm font-semibold">
            {lines.length ? pluralise(lines.length, "line") : "Lines"}
          </h3>
          {attentionCount > 0 ? (
            <div className="flex items-center gap-1 rounded-lg bg-muted p-0.5 text-sm">
              <button
                type="button"
                onClick={() => setOnlyAttention(false)}
                className={cn(
                  "rounded-md px-2.5 py-1 transition-colors",
                  !onlyAttention ? "bg-card shadow-sm" : "text-muted-foreground hover:text-foreground",
                )}
              >
                All
              </button>
              <button
                type="button"
                onClick={() => setOnlyAttention(true)}
                className={cn(
                  "rounded-md px-2.5 py-1 transition-colors",
                  onlyAttention ? "bg-card shadow-sm" : "text-muted-foreground hover:text-foreground",
                )}
              >
                Needs attention
                <span className="ml-1.5 tnum text-xs text-muted-foreground">{attentionCount}</span>
              </button>
            </div>
          ) : null}
        </div>

        {linesQuery.isPending ? (
          <LoadingRows />
        ) : linesQuery.error ? (
          <ErrorState error={linesQuery.error} />
        ) : shown.length === 0 ? (
          <EmptyState title={lines.length ? "Nothing here needs attention" : "No lines this month"}>
            {lines.length
              ? "Every line is booked and accounted for."
              : "No statement covering this month has been loaded, or the month had no movements."}
          </EmptyState>
        ) : (
          <div className="overflow-hidden rounded-xl border bg-card">
            <table className="w-full table-fixed">
              <caption className="sr-only">
                Statement lines for {monthTitle(month)} and what each one was booked to
              </caption>
              <thead>
                <tr className="border-b bg-muted/40 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  <th scope="col" className="w-20 py-2 pl-4 pr-3 text-left font-medium">
                    Date
                  </th>
                  <th scope="col" className="py-2 pr-3 text-left font-medium">
                    From the statement
                  </th>
                  <th scope="col" className="w-36 py-2 pr-6 text-right font-medium">
                    Amount
                  </th>
                  <th scope="col" className="w-[38%] py-2 pr-3 text-left font-medium">
                    Booked to
                  </th>
                  <th scope="col" className="w-10" />
                </tr>
              </thead>
              <tbody>
                {shown.map((line) => (
                  <LineRow
                    key={line.line_id}
                    entity={entity}
                    line={line}
                    precision={precision}
                    onOpen={() => setOpened({ line, editing: false })}
                    onEdit={() => setOpened({ line, editing: true })}
                    onExclude={() => setExcluding(line)}
                    onUndo={() => setUndoing(line)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {opened ? (
        <BookingDialog
          entity={entity}
          line={opened.line}
          startEditing={opened.editing}
          open
          onOpenChange={(open) => !open && setOpened(null)}
        />
      ) : null}

      <ReasonDialog
        open={Boolean(excluding)}
        onOpenChange={(open) => !open && setExcluding(null)}
        title="Not ours to book"
        description="The line stays on the statement and stays visible; nothing is booked for it."
        label="Why is it not ours?"
        confirmLabel="Record it"
        onConfirm={async (reason) => {
          if (!excluding) return
          await excludeLine.mutateAsync({ id: excluding.line_id, reason })
          toast.success("Recorded")
        }}
      />

      <ReasonDialog
        open={Boolean(undoing)}
        onOpenChange={(open) => !open && setUndoing(null)}
        title="Undo the booking"
        description="The entry it posted is reversed — the original stays standing beside its mirror — and the line is recorded as not booked."
        label="Why was the booking wrong?"
        confirmLabel="Undo it"
        destructive
        onConfirm={async (reason) => {
          if (!undoing) return
          await undoBooking.mutateAsync({ id: undoing.line_id, reason })
          toast.success("Booking undone", { description: "The entry it posted has been reversed." })
        }}
      />
    </div>
  )
}
