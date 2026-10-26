import { Fragment, type ReactNode } from "react"
import { Link } from "react-router-dom"

import type { StatementSection } from "@/lib/statement"
import { Amount } from "@/components/Amount"
import { cn } from "@/lib/utils"

/** Financial statement layout.
 *
 * Amounts are tabular figures, right aligned, at one precision per commodity, so columns line up on the
 * decimal point.  Weight and rules carry the hierarchy without colour: line items plain, subtotals with a rule
 * above, section totals with a heavier rule in medium weight, and the result row shaded.  Negatives use a
 * leading minus rather than parentheses, matching every other figure in the UI.
 *
 * The comparative column is hidden below `sm` rather than allowed to overflow, so a phone gets one readable
 * column instead of a horizontally scrolling balance sheet.
 */

export function StatementCard({
  title,
  subtitle,
  action,
  children,
}: {
  title: string
  subtitle: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="overflow-hidden rounded-xl border bg-card">
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b px-4 py-3.5 sm:px-5">
        <div>
          <h3 className="text-base font-semibold tracking-tight">{title}</h3>
          <p className="mt-0.5 text-sm text-muted-foreground">{subtitle}</p>
        </div>
        {action}
      </header>
      {children}
    </section>
  )
}

function Row({
  label,
  children,
  className,
}: {
  label: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <tr className={className}>
      <th scope="row" className="py-1.5 pl-4 pr-3 text-left text-sm font-normal sm:pl-5">
        {label}
      </th>
      {children}
    </tr>
  )
}

export function StatementSectionRows({
  section,
  entity,
  from,
  to,
  precision,
  commodity,
  showComparative,
}: {
  section: StatementSection
  entity: string
  from: string
  to: string
  precision: number
  commodity: string
  showComparative: boolean
}) {
  const cell = "w-32 py-1.5 pr-4 text-right sm:w-36 sm:pr-5"
  const comparativeCell = cn(cell, "hidden sm:table-cell")

  return (
    <>
      <tr>
        <th
          scope="colgroup"
          colSpan={showComparative ? 3 : 2}
          className="px-4 pb-1 pt-5 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground sm:px-5"
        >
          {section.title}
        </th>
      </tr>

      {section.groups.map((group) => (
        <Fragment key={group.key}>
          {group.heading ? (
            <tr>
              <th
                scope="rowgroup"
                colSpan={showComparative ? 3 : 2}
                className="pb-0.5 pl-4 pr-3 pt-2.5 text-left text-sm font-medium sm:pl-5"
              >
                {group.heading}
              </th>
            </tr>
          ) : null}

          {group.lines.map((line) => (
            <Row
              key={line.key}
              className="hover:bg-muted/40"
              label={
                line.accountId ? (
                  <Link
                    to={`/e/${entity}/accounts/${encodeURIComponent(line.accountId)}?from=${from}&to=${to}`}
                    className={cn("hover:text-primary hover:underline", group.heading && "pl-4")}
                  >
                    {line.label}
                  </Link>
                ) : (
                  <span className={cn("italic text-muted-foreground", group.heading && "pl-4")}>{line.label}</span>
                )
              }
            >
              <td className={cell}>
                <Amount micros={line.micros} precision={precision} />
              </td>
              {showComparative ? (
                <td className={cn(comparativeCell, "text-muted-foreground")}>
                  <Amount micros={line.comparative} precision={precision} />
                </td>
              ) : null}
            </Row>
          ))}

          {group.subtotal !== null ? (
            <Row label={<span className="pl-4 text-muted-foreground">Total {group.heading}</span>}>
              <td className={cn(cell, "border-t")}>
                <Amount micros={group.subtotal} precision={precision} />
              </td>
              {showComparative ? (
                <td className={cn(comparativeCell, "border-t text-muted-foreground")}>
                  <Amount micros={group.comparativeSubtotal} precision={precision} />
                </td>
              ) : null}
            </Row>
          ) : null}
        </Fragment>
      ))}

      <Row label={<span className="font-medium">{section.totalLabel}</span>}>
        <td className={cn(cell, "border-t-2 font-medium")}>
          <Amount micros={section.total} precision={precision} commodity={commodity} emphasis />
        </td>
        {showComparative ? (
          <td className={cn(comparativeCell, "border-t-2 text-muted-foreground")}>
            <Amount micros={section.comparativeTotal} precision={precision} emphasis />
          </td>
        ) : null}
      </Row>
    </>
  )
}

/** The statement's result row (e.g. net profit), ruled off from the rows above. */
export function StatementResult({
  label,
  micros,
  comparative,
  precision,
  commodity,
  showComparative,
}: {
  label: string
  micros: string
  comparative: string | null
  precision: number
  commodity: string
  showComparative: boolean
}) {
  const cell = "w-32 py-2.5 pr-4 text-right sm:w-36 sm:pr-5"
  return (
    <tr className="border-t-2 bg-muted/40">
      <th scope="row" className="py-2.5 pl-4 pr-3 text-left text-sm font-semibold sm:pl-5">
        {label}
      </th>
      <td className={cell}>
        <Amount micros={micros} precision={precision} commodity={commodity} emphasis />
      </td>
      {showComparative ? (
        <td className={cn(cell, "hidden text-muted-foreground sm:table-cell")}>
          <Amount micros={comparative} precision={precision} emphasis />
        </td>
      ) : null}
    </tr>
  )
}

/** Column headings: the current period and the comparative. */
export function StatementHead({
  current,
  comparative,
  showComparative,
}: {
  current: string
  comparative: string | null
  showComparative: boolean
}) {
  const head = "w-32 py-2 pr-4 text-right font-medium sm:w-36 sm:pr-5"
  return (
    <thead>
      <tr className="border-b bg-muted/40 text-xs font-medium uppercase tracking-wide text-muted-foreground">
        <th scope="col" className="py-2 pl-4 pr-3 text-left font-medium sm:pl-5">
          Account
        </th>
        <th scope="col" className={cn(head, "tnum")}>
          {current}
        </th>
        {showComparative ? (
          <th scope="col" className={cn(head, "tnum hidden sm:table-cell")}>
            {comparative}
          </th>
        ) : null}
      </tr>
    </thead>
  )
}
