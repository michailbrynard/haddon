import { Fragment } from "react"
import { useParams, useSearchParams } from "react-router-dom"
import { Check, Download } from "lucide-react"

import { useCanExport, usePrecision, useStatementsDocument, useUnrecoveredTax } from "@/lib/queries"
import { amount, isZero, pluralise, shortDate } from "@/lib/format"
import type { Period } from "@/lib/period"
import {
  dateWords,
  hasBegun,
  periodFromParams,
  periodName,
  periodParams,
  shortDateWords,
  throughToday,
  today,
} from "@/lib/period"
import { toSection } from "@/lib/statement"
import type { StatementsDocument, UnrecoveredItem } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ExportJournal } from "@/components/ExportJournal"
import { PeriodNav } from "@/components/PeriodNav"
import {
  StatementCard,
  StatementHead,
  StatementResult,
  StatementSectionRows,
} from "@/components/Statement"
import { EmptyState, ErrorState, LoadingRows } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"

/** The income statement and balance sheet over a period the reviewer chooses.
 *
 * Accounting points that a wrong screen would get plausibly wrong rather than visibly broken:
 *
 * * An income statement covers a period; a balance sheet is at a date.  One selection drives both, so the
 *   balance sheet is drawn as at the period end and headed with that date, never a range.
 * * The balance sheet carries the result to date inside equity.  Assets, liabilities and equity net to the
 *   profit or loss accumulated since the journal began, which nothing has posted to equity yet, so equity gets
 *   an explicit result line and only then do the two sides agree.  That agreement is shown as a check.
 * * The journal is continuous, so any valid date range can be reported; there is no book year to stay inside.
 *
 * Since issue 0008 none of this is decided here: `/reports/statements` returns the shaped sections, signs,
 * subtotals, result row and balance check, and this page draws them as served.  The service builds the PDF
 * from the same view, so the screen and the PDF can't drift apart.
 */

export function Reports() {
  const { entity = "" } = useParams()
  const [params, setParams] = useSearchParams()
  const fallbackYear = new Date().getFullYear()

  const asked = periodFromParams(params, fallbackYear)
  const setPeriod = (next: Period) => setParams(periodParams(next), { replace: true })

  // Cut a running period off at today so a current quarter isn't headed as a whole quarter.
  const now = today()
  const begun = hasBegun(asked, now)
  const period = throughToday(asked, now)

  // Request the period as chosen, not the cut-down one; the service works out "to date" from the bounds.
  const statements = useStatementsDocument(entity, begun ? asked.start : null, begun ? asked.end : null)
  // Unrecovered tax is inside the statements' figures, so it needs its own read.
  const unrecovered = useUnrecoveredTax(entity, begun ? period.start : null, begun ? period.end : null)

  const precision = usePrecision(entity)
  const canExport = useCanExport(entity)

  return (
    <div className="space-y-5">
      <div className="space-y-4">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Reports</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {begun
              ? `The income statement for ${statements.data?.period.name ?? periodName(period)}, and the position it left behind.`
              : `These books can be reported through ${dateWords(now)}.`}
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <PeriodNav period={asked} onChange={setPeriod} today={now} />
          {begun ? (
            <div className="flex flex-wrap items-center gap-2">
              {/* Both downloads cover the period on screen. */}
              <DownloadStatements entity={entity} period={asked} />
              {canExport ? <ExportJournal entity={entity} period={asked} /> : null}
            </div>
          ) : null}
        </div>
      </div>

      {!begun ? (
        <EmptyState title={`${periodName(asked)} has not begun`}>
          Step back to a period these books have reached.
        </EmptyState>
      ) : null}

      {begun ? (
        <>
          <IncomeStatementCard entity={entity} precision={precision} document={statements} />

          <BalanceSheetCard entity={entity} precision={precision} document={statements} />

          {unrecovered.data?.items.length ? (
            <UnrecoveredTax
              items={unrecovered.data.items}
              totals={unrecovered.data.totals}
              precision={precision}
              periodLabel={periodName(period)}
            />
          ) : null}
        </>
      ) : null}

      <p className="px-1 text-xs text-muted-foreground">
        Figures are shown in statement sign: revenue, liabilities and equity read positive when they are what
        their name says, so an account standing the other way round shows as a negative inside its own section.
        The ledger itself stores every amount debit-positive, which is what the trial balance and the registers
        show.
      </p>
    </div>
  )
}

/** Recoverable tax the period didn't reclaim (docs/design/decisions.md, "Recoverable tax that was not recovered").
 *
 * The statements can't show it because the amount is inside their figures, folded into an expense or
 * capitalised into an asset.
 *
 * Collapsed to its total by default; most periods have none.  When there is some, the reasons matter more
 * than the total, since they decide whether it can still be recovered.
 */
function UnrecoveredTax({
  items,
  totals,
  precision,
  periodLabel,
}: {
  items: UnrecoveredItem[]
  totals: Record<string, string>
  precision: (commodity: string) => number
  periodLabel: string
}) {
  const codes = Object.keys(totals).sort()
  return (
    <StatementCard
      title="Recoverable tax not recovered"
      subtitle={`${pluralise(items.length, "booking")} in ${periodLabel}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2 px-4 py-3.5 sm:px-5">
        {codes.map((code) => (
          <div key={code}>
            <div className="tnum text-xl font-semibold tracking-tight">
              {amount(totals[code], precision(code))} {code}
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">not reclaimed</div>
          </div>
        ))}
      </div>
      <Collapsible>
        <CollapsibleTrigger className="w-full border-t px-4 py-2.5 text-left text-xs text-muted-foreground transition-colors hover:text-foreground sm:px-5">
          Why each was not recovered
        </CollapsibleTrigger>
        <CollapsibleContent className="border-t bg-muted/30 px-4 py-3 sm:px-5">
          <ul className="space-y-3 text-xs">
            {items.map((item) => (
              <li key={item.record.id}>
                <div className="flex flex-wrap items-baseline justify-between gap-x-4">
                  <span className="font-medium text-foreground">
                    {shortDate(item.entry.dt, true)} · {item.entry.narration}
                  </span>
                  <span className="tnum font-medium text-foreground">
                    {amount(item.record.amount_micros, precision(item.record.commodity))}{" "}
                    {item.record.commodity}
                  </span>
                </div>
                <div className="text-muted-foreground">{item.record.reason}</div>
                <div className="mt-0.5 text-muted-foreground/80">
                  {item.record.tax_code ? `${item.record.tax_code} · ` : ""}
                  {item.record.recoverable_until
                    ? `recoverable until ${shortDate(item.record.recoverable_until, true)}`
                    : "no last date recorded"}
                  {" · recorded by "}
                  {item.record.created_by}
                </div>
              </li>
            ))}
          </ul>
        </CollapsibleContent>
      </Collapsible>
    </StatementCard>
  )
}

/** Download both statements as a PDF.
 *
 * A plain link rather than a fetch: the service renders the PDF and the browser saves it, so the page holds no
 * blob and the file is exactly what any other API caller gets for the same period.  Goes through `/ui/v1` like
 * every other read; the proxy strips the prefix.
 */
function DownloadStatements({ entity, period }: { entity: string; period: Period }) {
  const href =
    `/ui/v1/entities/${encodeURIComponent(entity)}/reports/statements.pdf` +
    `?from=${period.start}&to=${period.end}`
  return (
    <Button asChild variant="outline" size="sm">
      <a href={href} download>
        <Download className="size-4" />
        Download PDF
      </a>
    </Button>
  )
}

type DocumentQuery = {
  data: StatementsDocument | undefined
  error: unknown
  isPending: boolean
}

/** Whether any commodity in the period has an income or expense movement to show. */
function hasMovements(document: StatementsDocument): boolean {
  return document.statements.some((per) =>
    per.income_statement.sections.some((section) =>
      section.groups.some((group) => group.lines.length > 0),
    ),
  )
}

function IncomeStatementCard({
  entity,
  precision,
  document,
}: {
  entity: string
  precision: (commodity: string) => number
  document: DocumentQuery
}) {
  return (
    <StatementCard title="Income statement" subtitle={document.data?.period.heading ?? ""}>
      {document.isPending ? (
        <div className="p-4 sm:p-5">
          <LoadingRows rows={6} />
        </div>
      ) : document.error ? (
        <div className="p-4 sm:p-5">
          <ErrorState error={document.error} />
        </div>
      ) : !document.data ? null : !hasMovements(document.data) ? (
        <div className="p-4 sm:p-5">
          <EmptyState title="No income or expense is posted in this period">
            Nothing was booked to an income or expense account in this period.
          </EmptyState>
        </div>
      ) : (
        <IncomeStatementBody entity={entity} precision={precision} document={document.data} />
      )}
    </StatementCard>
  )
}

function IncomeStatementBody({
  entity,
  precision,
  document,
}: {
  entity: string
  precision: (commodity: string) => number
  document: StatementsDocument
}) {
  const showComparative = document.comparative !== null
  const named = document.statements.length > 1

  return (
    <>
      {document.statements.map((per) => {
        const view = per.income_statement
        const from = view.start ?? ""
        const to = view.end ?? ""
        return (
          <Fragment key={per.commodity}>
            {named ? <CommodityBar commodity={per.commodity} /> : null}
            <table className="w-full">
              <StatementHead
                current={document.period.name}
                comparative={document.comparative?.name ?? null}
                showComparative={showComparative}
              />
              <tbody>
                {view.sections.map((section) => (
                  <StatementSectionRows
                    key={section.key}
                    section={toSection(section)}
                    entity={entity}
                    from={from}
                    to={to}
                    precision={precision(per.commodity)}
                    commodity={per.commodity}
                    showComparative={showComparative}
                  />
                ))}
              </tbody>
              <tfoot>
                <StatementResult
                  label={view.result_label}
                  micros={view.result_micros}
                  comparative={view.comparative_result_micros}
                  precision={precision(per.commodity)}
                  commodity={per.commodity}
                  showComparative={showComparative}
                />
              </tfoot>
            </table>
          </Fragment>
        )
      })}
    </>
  )
}

function BalanceSheetCard({
  entity,
  precision,
  document,
}: {
  entity: string
  precision: (commodity: string) => number
  document: DocumentQuery
}) {
  return (
    <StatementCard
      title="Balance sheet"
      subtitle={document.data ? `as at ${dateWords(document.data.as_of)}` : ""}
    >
      {document.isPending ? (
        <div className="p-4 sm:p-5">
          <LoadingRows rows={8} />
        </div>
      ) : document.error ? (
        <div className="p-4 sm:p-5">
          <ErrorState error={document.error} />
        </div>
      ) : document.data ? (
        <BalanceSheetBody entity={entity} precision={precision} document={document.data} />
      ) : null}
    </StatementCard>
  )
}

function BalanceSheetBody({
  entity,
  precision,
  document,
}: {
  entity: string
  precision: (commodity: string) => number
  document: StatementsDocument
}) {
  const named = document.statements.length > 1

  return (
    <>
      {document.statements.map((per) => {
        const sheet = per.balance_sheet
        const showComparative = sheet.comparative_as_of !== null
        // A balance sheet has no period, so account links open the register over the year containing its date.
        const from = `${sheet.as_of.slice(0, 4)}-01-01`
        return (
          <Fragment key={per.commodity}>
            {named ? <CommodityBar commodity={per.commodity} /> : null}
            <table className="w-full">
              <StatementHead
                current={shortDateWords(sheet.as_of)}
                comparative={sheet.comparative_as_of ? shortDateWords(sheet.comparative_as_of) : null}
                showComparative={showComparative}
              />
              <tbody>
                {sheet.sections.map((section) => (
                  <StatementSectionRows
                    key={section.key}
                    section={toSection(section)}
                    entity={entity}
                    from={from}
                    to={sheet.as_of}
                    precision={precision(per.commodity)}
                    commodity={per.commodity}
                    showComparative={showComparative}
                  />
                ))}
              </tbody>
              <tfoot>
                <StatementResult
                  label="Total liabilities and equity"
                  micros={sheet.total_funding_micros}
                  comparative={sheet.comparative_total_funding_micros}
                  precision={precision(per.commodity)}
                  commodity={per.commodity}
                  showComparative={showComparative}
                />
              </tfoot>
            </table>
            <BalanceCheck
              differences={sheet.checks.map((check) => ({
                label: shortDateWords(check.date),
                difference: check.difference_micros,
              }))}
              commodity={per.commodity}
              precision={precision(per.commodity)}
            />
          </Fragment>
        )
      })}
    </>
  )
}

/** Whether the balance sheet balances in every column shown, and by how much it doesn't.
 *
 * The differences come from the service's check (`/reports/statements`), not a subtraction here, so the screen
 * can't disagree with the PDF.  Every column is checked, including the comparative. */
function BalanceCheck({
  differences,
  commodity,
  precision,
}: {
  differences: { label: string; difference: string }[]
  commodity: string
  precision: number
}) {
  const off = differences.filter((d) => !isZero(d.difference))
  return (
    <div
      className={cn(
        "flex items-start gap-2 border-t px-4 py-3 text-sm sm:px-5",
        off.length ? "bg-destructive/5" : "bg-settled-surface",
      )}
    >
      {off.length === 0 ? (
        <>
          <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-settled/15 text-settled">
            <Check className="size-3.5" />
          </span>
          <span>
            {/* "at both dates" rather than "in both columns": on a phone the comparative column is hidden, but
                the check still covers it */}
            Assets equal liabilities plus equity
            {differences.length > 1 ? " at both dates" : ""}.
          </span>
        </>
      ) : (
        <span className="font-medium">
          {off.map((d) => (
            <span key={d.label} className="block">
              At {d.label}, assets and the two sides funding them differ by{" "}
              <span className="tnum">
                {amount(d.difference, precision)} {commodity}
              </span>
              .
            </span>
          ))}
        </span>
      )}
    </div>
  )
}

function CommodityBar({ commodity }: { commodity: string }) {
  return (
    <div className="border-t bg-muted/30 px-4 py-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground first:border-t-0 sm:px-5">
      {commodity}
    </div>
  )
}
