import { Link, useParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"

import { useAccounts, useLine, usePrecision, useProvenance } from "@/lib/queries"
import { longDate, money, monthTitle, prettyPath, timestamp } from "@/lib/format"
import { Amount } from "@/components/Amount"
import { Evidence } from "@/components/Evidence"
import { ErrorState, LoadingRows } from "@/components/states"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-3 py-2">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </div>
  )
}

/** One statement line, traced back to the file it was read from and forward to whatever was booked for it.
 *
 * Actors are shown as recorded: who loaded the document, who booked the entry.
 */
export function LineDetail() {
  const { entity = "", lineId = "" } = useParams()
  const line = useLine(entity, lineId)
  const provenance = useProvenance(entity, lineId)
  const precisionOf = usePrecision(entity)
  const { data: accounts } = useAccounts(entity)

  if (line.isPending) return <LoadingRows rows={6} />
  if (line.error) return <ErrorState error={line.error} />

  const l = line.data
  const precision = precisionOf(l.commodity)
  const source = provenance.data?.lines[0]

  return (
    <div className="space-y-6">
      <Link
        to={`/e/${entity}/month?month=${l.dt.slice(0, 7)}`}
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        Back to {monthTitle(l.dt.slice(0, 7))}
      </Link>

      <div>
        <h2 className="text-2xl font-semibold tracking-tight">{l.description}</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          {longDate(l.dt)} · {money(l.amount_micros, l.commodity, precision)} ·{" "}
          {prettyPath(accounts?.find((a) => a.id === l.account_id)?.path ?? l.account_id)}
        </p>
      </div>

      <section className="rounded-xl border bg-card px-5 py-3">
        <h3 className="py-2 text-sm font-semibold">What became of it</h3>
        <dl className="divide-y">
          {!l.explanation ? (
            <Row label="Booked to">
              <span className="text-attention">Nothing yet — this line still needs an account.</span>
            </Row>
          ) : null}
          {l.explanation?.kind === "ignored" ? (
            <Row label="Not booked">{l.explanation.reason}</Row>
          ) : null}
          {l.entries.map((entry) => (
            <Row key={entry.id} label={entry.status === "posted" ? "Booked to" : "Written, not posted"}>
              <div className="space-y-1">
                {/* Links to the entry page for its audit history and correction chain (issue 0007). */}
                <Link
                  to={`/e/${entity}/entries/${entry.id}`}
                  className="underline-offset-4 hover:underline"
                >
                  {entry.narration}
                </Link>
                <ul className="space-y-0.5">
                  {entry.postings.map((p) => (
                    <li key={p.id} className="flex justify-between gap-4 text-muted-foreground">
                      <span className="truncate">{p.account_path}</span>
                      <Amount micros={p.amount_micros} precision={precisionOf(p.commodity)} commodity={p.commodity} />
                    </li>
                  ))}
                </ul>
                <div className="text-xs text-muted-foreground">
                  {entry.created_by} · {timestamp(entry.created_at)}
                  {entry.reversed_by ? " · reversed" : ""}
                </div>
              </div>
            </Row>
          ))}
        </dl>
      </section>

      <section className="rounded-xl border bg-card px-5 py-3">
        <h3 className="py-2 text-sm font-semibold">Where it came from</h3>
        {provenance.isPending ? (
          <LoadingRows rows={3} />
        ) : provenance.error ? (
          <ErrorState error={provenance.error} />
        ) : (
          <dl className="divide-y">
            {source ? (
              <>
                {/* The file itself.  Before issue 0007 this showed a hash and the server-side path, which a
                    reader can't open and which shouldn't be disclosed. */}
                <Row label="Document">
                  <Evidence
                    entity={entity}
                    documentId={source.document_id}
                    document={source.document}
                  />
                </Row>
                <Row label="Row">{l.line_no} of the file</Row>
              </>
            ) : (
              <Row label="Document">No document is linked to this line.</Row>
            )}
          </dl>
        )}
        <Collapsible>
          <CollapsibleTrigger className="w-full border-t py-2.5 text-left text-xs text-muted-foreground hover:text-foreground">
            The raw row, exactly as it was read
          </CollapsibleTrigger>
          <CollapsibleContent>
            <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-muted/60 p-3 font-mono text-xs">
              {l.raw}
            </pre>
          </CollapsibleContent>
        </Collapsible>
      </section>
    </div>
  )
}
