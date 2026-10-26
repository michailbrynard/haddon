import { Link, useParams, useSearchParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"

import { usePrecision, useRegister } from "@/lib/queries"
import { longDate, prettyPath, shortDate } from "@/lib/format"
import { today } from "@/lib/period"
import { Amount } from "@/components/Amount"
import { EmptyState, ErrorState, LoadingRows } from "@/components/states"
import { RangeNav } from "@/components/DateNav"

/** One account's register: every posting in the range, with the balance after each. */
export function Account() {
  const { entity = "", accountId = "" } = useParams()
  const [params, setParams] = useSearchParams()
  const to = params.get("to") ?? today()
  const from = params.get("from") ?? `${to.slice(0, 4)}-01-01`

  const register = useRegister(entity, accountId, from, to)
  const precision = usePrecision(entity)

  return (
    <div className="space-y-6">
      <Link
        to={`/e/${entity}/accounts?as_of=${to}`}
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        All accounts
      </Link>

      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="truncate text-2xl font-semibold tracking-tight">
            {register.data ? prettyPath(register.data.account_path) : "Account"}
          </h2>
          {register.data ? (
            <p className="mt-0.5 text-sm text-muted-foreground">
              {longDate(register.data.start)} to {longDate(register.data.end)}
            </p>
          ) : null}
        </div>
        <RangeNav from={from} to={to} onChange={(f, t) => setParams({ from: f, to: t })} />
      </div>

      {register.isPending ? (
        <LoadingRows rows={10} />
      ) : register.error ? (
        <ErrorState error={register.error} />
      ) : register.data.items.length === 0 ? (
        <EmptyState title="Nothing posted to this account in this range" />
      ) : (
        <div className="overflow-hidden rounded-xl border bg-card">
          <table className="w-full table-fixed">
            <thead>
              <tr className="border-b bg-muted/40 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                <th scope="col" className="w-20 py-2 pl-4 pr-3 text-left font-medium">
                  Date
                </th>
                <th scope="col" className="py-2 pr-3 text-left font-medium">
                  Entry
                </th>
                <th scope="col" className="w-36 py-2 pr-6 text-right font-medium">
                  Amount
                </th>
                <th scope="col" className="w-40 py-2 pr-4 text-right font-medium">
                  Balance
                </th>
              </tr>
            </thead>
            <tbody>
              {register.data.items.map((line) => (
                <tr key={`${line.entry_id}-${line.seq}`} className="border-b last:border-b-0 hover:bg-muted/40">
                  <td className="py-2.5 pl-4 pr-3 align-top">
                    <span className="tnum whitespace-nowrap text-sm text-muted-foreground">
                      {shortDate(line.dt)}
                    </span>
                  </td>
                  <td className="max-w-0 py-2.5 pr-3 align-top">
                    <div className="truncate text-sm" title={line.narration}>
                      {line.narration}
                    </div>
                    {line.memo ? (
                      <div className="truncate text-xs text-muted-foreground">{line.memo}</div>
                    ) : null}
                  </td>
                  <td className="py-2.5 pr-6 text-right align-top">
                    <Amount micros={line.amount_micros} precision={precision(line.commodity)} />
                  </td>
                  <td className="py-2.5 pr-4 text-right align-top">
                    <Amount
                      micros={line.running_micros}
                      precision={precision(line.commodity)}
                      commodity={line.commodity}
                      emphasis
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
