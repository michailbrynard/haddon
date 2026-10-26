import { Link, useParams, useSearchParams } from "react-router-dom"
import { Check } from "lucide-react"

import { usePrecision, useTrialBalance } from "@/lib/queries"
import { amount, isZero, longDate, prettyPath } from "@/lib/format"
import { today } from "@/lib/period"
import { cn } from "@/lib/utils"
import { Amount } from "@/components/Amount"
import { ErrorState, LoadingRows } from "@/components/states"
import { AsOfNav } from "@/components/DateNav"

/** The trial balance at a date, and whether it nets to zero. */
export function TrialBalance() {
  const { entity = "" } = useParams()
  const [params, setParams] = useSearchParams()
  const asOf = params.get("as_of") ?? today()

  const report = useTrialBalance(entity, asOf)
  const precision = usePrecision(entity)
  const totals = Object.entries(report.data?.totals ?? {})
  const balanced = totals.length > 0 && totals.every(([, v]) => isZero(v))

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Trial balance</h2>
          {report.data ? (
            <p className="mt-0.5 text-sm text-muted-foreground">
              Balances through {longDate(report.data.through)}
            </p>
          ) : null}
        </div>
        <AsOfNav value={asOf} onChange={(value) => setParams({ as_of: value })} />
      </div>

      {report.isPending ? (
        <LoadingRows rows={10} />
      ) : report.error ? (
        <ErrorState error={report.error} />
      ) : (
        <>
          <div
            className={cn(
              "flex items-center gap-2 rounded-xl border px-5 py-3.5 text-sm",
              balanced
                ? "border-settled-border bg-settled-surface"
                : "border-destructive/30 bg-destructive/5",
            )}
          >
            {balanced ? (
              <>
                <span className="flex size-5 items-center justify-center rounded-full bg-settled/15 text-settled">
                  <Check className="size-3.5" />
                </span>
                <span className="font-medium">Every debit has a matching credit. The book balances.</span>
              </>
            ) : (
              <span className="font-medium">
                The book does not net to zero:{" "}
                {totals
                  .filter(([, v]) => !isZero(v))
                  .map(([code, v]) => `${amount(v, precision(code))} ${code}`)
                  .join(", ")}
              </span>
            )}
          </div>

          <div className="overflow-hidden rounded-xl border bg-card">
            <table className="w-full">
              <thead>
                <tr className="border-b bg-muted/40 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  <th scope="col" className="py-2 pl-4 pr-3 text-left font-medium">
                    Account
                  </th>
                  <th scope="col" className="w-28 py-2 pr-3 text-left font-medium">
                    Type
                  </th>
                  <th scope="col" className="w-44 py-2 pr-4 text-right font-medium">
                    Balance
                  </th>
                </tr>
              </thead>
              <tbody>
                {report.data.lines.map((line) => (
                  <tr
                    key={`${line.account_id}-${line.commodity}`}
                    className="border-b last:border-b-0 hover:bg-muted/40"
                  >
                    <td className="py-2.5 pl-4 pr-3">
                      <Link
                        to={`/e/${entity}/accounts/${encodeURIComponent(line.account_id)}?from=${asOf.slice(0, 4)}-01-01&to=${asOf}`}
                        className="text-sm hover:text-primary hover:underline"
                      >
                        {prettyPath(line.account_path)}
                      </Link>
                    </td>
                    <td className="py-2.5 pr-3 text-sm text-muted-foreground">{line.type}</td>
                    <td className="py-2.5 pr-4 text-right">
                      <Amount
                        micros={line.amount_micros}
                        precision={precision(line.commodity)}
                        commodity={line.commodity}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t-2 bg-muted/40">
                  <td className="py-2.5 pl-4 pr-3 text-sm font-medium" colSpan={2}>
                    Total
                  </td>
                  <td className="py-2.5 pr-4 text-right">
                    <div className="flex flex-col items-end gap-0.5">
                      {totals.map(([code, value]) => (
                        <Amount key={code} micros={value} precision={precision(code)} commodity={code} emphasis />
                      ))}
                    </div>
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
