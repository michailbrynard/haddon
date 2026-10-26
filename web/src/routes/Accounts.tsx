import { useMemo, useState } from "react"
import { Link, useParams, useSearchParams } from "react-router-dom"
import { Search } from "lucide-react"

import { usePrecision, useTrialBalance } from "@/lib/queries"
import { isZero, longDate, splitPath } from "@/lib/format"
import { today } from "@/lib/period"
import type { ReportLine } from "@/lib/types"
import { Amount } from "@/components/Amount"
import { EmptyState, ErrorState, LoadingRows } from "@/components/states"
import { Input } from "@/components/ui/input"
import { AsOfNav } from "@/components/DateNav"

// The five account types in balance sheet then income statement order, with plurals spelled out (a naive
// `+ "s"` gives "liabilitys").
const TYPES: { type: string; title: string }[] = [
  { type: "asset", title: "Assets" },
  { type: "liability", title: "Liabilities" },
  { type: "equity", title: "Equity" },
  { type: "income", title: "Income" },
  { type: "expense", title: "Expenses" },
]

/** The chart of accounts with each balance at a date.  Each account links to its register. */
export function Accounts() {
  const { entity = "" } = useParams()
  const [params, setParams] = useSearchParams()
  const asOf = params.get("as_of") ?? today()
  const [filter, setFilter] = useState("")

  const report = useTrialBalance(entity, asOf)
  const precision = usePrecision(entity)

  const grouped = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    const rows = (report.data?.lines ?? []).filter(
      (l) => !needle || l.account_path.toLowerCase().includes(needle),
    )
    const byAccount = new Map<string, ReportLine[]>()
    for (const line of rows) {
      const list = byAccount.get(line.account_id) ?? []
      list.push(line)
      byAccount.set(line.account_id, list)
    }
    return TYPES.map(({ type, title }) => ({
      type,
      title,
      accounts: [...byAccount.values()]
        .filter((lines) => lines[0].type === type)
        .sort((a, b) => a[0].account_path.localeCompare(b[0].account_path)),
    })).filter((g) => g.accounts.length)
  }, [report.data, filter])

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Accounts</h2>
          {report.data ? (
            <p className="mt-0.5 text-sm text-muted-foreground">
              Balances through {longDate(report.data.through)}
            </p>
          ) : null}
        </div>
        <AsOfNav value={asOf} onChange={(value) => setParams({ as_of: value })} />
      </div>

      <div className="relative max-w-sm">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter the chart"
          aria-label="Filter accounts"
          className="pl-9"
        />
      </div>

      {report.isPending ? (
        <LoadingRows rows={8} />
      ) : report.error ? (
        <ErrorState error={report.error} />
      ) : grouped.length === 0 ? (
        <EmptyState title="No account matches">Try a shorter search.</EmptyState>
      ) : (
        <div className="space-y-7">
          {grouped.map(({ type, title, accounts }) => (
            <section key={type}>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {title}
              </h3>
              <div className="overflow-hidden rounded-xl border bg-card">
                <table className="w-full">
                  <tbody>
                    {accounts.map((lines) => (
                      <tr key={lines[0].account_id} className="border-b last:border-b-0 hover:bg-muted/40">
                        <td className="py-2 pl-4 pr-3">
                          <Link
                            to={`/e/${entity}/accounts/${encodeURIComponent(lines[0].account_id)}?from=${asOf.slice(0, 4)}-01-01&to=${asOf}`}
                            className="text-sm hover:text-primary hover:underline"
                            title={lines[0].account_path}
                          >
                            {/* The section heading already names the type, so the first segment is dimmed rather
                                than dropped, keeping the path exactly as it is in the chart. */}
                            <span className="text-muted-foreground">
                              {splitPath(lines[0].account_path).head} ·{" "}
                            </span>
                            {splitPath(lines[0].account_path).rest}
                          </Link>
                        </td>
                        <td className="py-2 pr-4 text-right">
                          <div className="flex flex-col items-end gap-0.5">
                            {lines.map((l) => (
                              <Amount
                                key={l.commodity}
                                micros={l.amount_micros}
                                precision={precision(l.commodity)}
                                commodity={l.commodity}
                                className={isZero(l.amount_micros) ? "text-muted-foreground" : undefined}
                              />
                            ))}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  )
}
