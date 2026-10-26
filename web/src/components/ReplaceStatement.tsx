import { useMemo, useState } from "react"
import { FileUp, Plus, Replace, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { api } from "@/lib/api"
import type { DocumentSubmission, StatementLineSubmission } from "@/lib/api"
import { useEntityMutation } from "@/lib/queries"
import { addMicros, decimalForWire, longDate, microsFromDecimal } from "@/lib/format"
import type { Line, Statement } from "@/lib/types"
import { Amount } from "@/components/Amount"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ReplacementRefusal } from "@/components/StatementBlockers"

/**
 * Form for loading a corrected reading of a statement in place of the current one.
 *
 * A replacement restates the same subject: the service refuses a different account or commodity, so neither
 * is offered.  Everything read off the document can change: the period, balances, lines, the document itself
 * and where the closing balance is printed.  A statement loaded against the wrong account isn't a case for
 * replacement, and the screen says so.
 *
 * The lines start as the predecessor's, since the usual fix is one mis-parsed row and retyping forty lines to
 * fix one would put people off.  `raw` and `source_key` are carried forward even on corrected rows: `raw` is
 * what the document says, and the amount is the reading of it, so a corrected amount next to unchanged raw
 * text is the right provenance for a mis-parse.
 *
 * The arithmetic is shown but not enforced.  Lines must sum to closing minus opening, and both figures are
 * shown while typing, but the submit button doesn't depend on it; the service decides whether a submission
 * balances.
 *
 * Nothing is destroyed: the predecessor stays as it was, readable, and is referenced by the replacement.
 * Hence a form rather than an "are you sure?" dialog.
 */

/** One row of the editable table: the submission shape plus a React key. */
interface Row extends StatementLineSubmission {
  key: string
}

/** Which document the replacement cites: the predecessor's (`same`) or a newly chosen file (`file`). */
const NEW_FILE = "file"
const SAME_FILE = "same"

function rowsFrom(lines: Line[], precision: number): Row[] {
  return [...lines]
    .sort((a, b) => a.line_no - b.line_no)
    .map((line) => ({
      key: line.line_id,
      dt: line.dt,
      description: line.description,
      amount: decimalForWire(line.amount_micros, precision),
      currency: line.commodity,
      reference: line.reference,
      raw: line.raw,
      source_key: line.source_key,
    }))
}

/** The SHA-256 of the chosen file, computed from its bytes rather than typed in.
 *
 * Asking a person to paste a hash would mean nobody actually checked it.  `crypto.subtle` is available because
 * the UI is served over TLS (or loopback in the test harness), both secure contexts.
 */
async function sha256Of(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer())
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("")
}

export function ReplaceStatement({
  entity,
  statement,
  lines,
  precision,
  onReplaced,
}: {
  entity: string
  statement: Statement
  lines: Line[]
  precision: number
  onReplaced: (replacement: Statement) => void
}) {
  const [periodStart, setPeriodStart] = useState(statement.period_start)
  const [periodEnd, setPeriodEnd] = useState(statement.period_end)
  const [opening, setOpening] = useState(decimalForWire(statement.opening_micros, precision))
  const [closing, setClosing] = useState(decimalForWire(statement.closing_micros, precision))
  const [locator, setLocator] = useState(statement.locator)
  const [page, setPage] = useState(statement.page === null ? "" : String(statement.page))
  const [reason, setReason] = useState("")
  const [rows, setRows] = useState<Row[]>(() => rowsFrom(lines, precision))
  const [source, setSource] = useState<typeof SAME_FILE | typeof NEW_FILE>(SAME_FILE)
  const [chosen, setChosen] = useState<DocumentSubmission | null>(null)
  const [externalRef, setExternalRef] = useState("")
  const [reading, setReading] = useState(false)

  /** The lines' sum against closing minus opening.  Null while any amount can't be read. */
  const sums = useMemo(() => {
    const parsed = rows.map((row) => microsFromDecimal(row.amount))
    const unreadable = parsed.some((value) => value === null)
    const openingMicros = microsFromDecimal(opening)
    const closingMicros = microsFromDecimal(closing)
    if (unreadable || openingMicros === null || closingMicros === null) return null
    const total = addMicros(parsed as string[])
    const movement = (BigInt(closingMicros) - BigInt(openingMicros)).toString()
    return { total, movement, agrees: BigInt(total) === BigInt(movement) }
  }, [rows, opening, closing])

  /** Rows dated outside the declared period.  The service refuses these, so flag them before submitting. */
  const outside = rows.filter((row) => row.dt !== "" && (row.dt < periodStart || row.dt > periodEnd))

  const document: DocumentSubmission | null =
    source === SAME_FILE
      ? {
          sha256: statement.document.sha256,
          filename: statement.document.filename,
          media_type: statement.document.media_type,
          size_bytes: statement.document.size_bytes,
          // A submission needs exactly one location, and the UI never has a server path.  For a document the
          // service holds, use the URL of its bytes; for one held elsewhere, its registered reference.  Documents
          // are content-addressed, so this resolves to the row already registered under this hash and its
          // recorded location isn't changed.
          external_ref: statement.document.external_ref ?? `${statement.links.document}/content`,
        }
      : chosen && externalRef.trim()
        ? { ...chosen, external_ref: externalRef.trim() }
        : null

  const replace = useEntityMutation(entity, () =>
    api.replaceStatement(entity, statement.statement_id, {
      // Always the predecessor's account and commodity; a replacement restates the same subject.
      account: statement.account_id,
      commodity: statement.commodity,
      period_start: periodStart,
      period_end: periodEnd,
      opening,
      closing,
      lines: rows.map(({ key: _key, ...line }) => line),
      document: document!,
      locator,
      page: page.trim() === "" ? null : Number(page),
      reason: reason.trim(),
    }),
  )

  const ready = Boolean(reason.trim() && locator.trim() && document && rows.length > 0 && !reading)

  function edit(key: string, patch: Partial<Row>) {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)))
  }

  async function choose(file: File | undefined) {
    if (!file) return
    setReading(true)
    try {
      setChosen({
        sha256: await sha256Of(file),
        filename: file.name,
        media_type: file.type || "application/octet-stream",
        size_bytes: file.size,
      })
    } finally {
      setReading(false)
    }
  }

  return (
    <section className="space-y-5 rounded-xl border p-5">
      <div>
        <h3 className="font-medium">Replace this statement</h3>
        <p className="mt-0.5 text-sm text-muted-foreground">
          For a statement read wrongly: a mis-parsed line, a period that was not the one on the document, a
          download that stopped short. Nothing is deleted — the statement below stays exactly as it is and
          stays readable, and the replacement names it. It is loaded for{" "}
          <span className="font-medium text-foreground">{statement.account_path}</span> in {statement.commodity}{" "}
          and a replacement restates the same subject, so neither can be changed here.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor="replace-from">Period from</Label>
          <Input
            id="replace-from"
            type="date"
            value={periodStart}
            onChange={(e) => setPeriodStart(e.target.value)}
            className="tnum"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="replace-to">Period to</Label>
          <Input
            id="replace-to"
            type="date"
            value={periodEnd}
            onChange={(e) => setPeriodEnd(e.target.value)}
            className="tnum"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="replace-opening">Opening balance</Label>
          <Input
            id="replace-opening"
            value={opening}
            onChange={(e) => setOpening(e.target.value)}
            className="tnum"
          />
          <p className="text-xs text-muted-foreground">
            Checked against the closing balance of the statement ending the day before, when there is one.
          </p>
        </div>
        <div className="space-y-1">
          <Label htmlFor="replace-closing">Closing balance</Label>
          <Input
            id="replace-closing"
            value={closing}
            onChange={(e) => setClosing(e.target.value)}
            className="tnum"
          />
        </div>
      </div>

      <div className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h4 className="text-sm font-semibold">The lines, as they should have been read</h4>
          <span className="text-xs text-muted-foreground">
            Starting from the {rows.length === 1 ? "line" : `${lines.length} lines`} of the statement being
            replaced. Correct what was read wrongly.
          </span>
        </div>
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <caption className="sr-only">The lines of the replacement statement</caption>
            <thead>
              <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th scope="col" className="px-3 py-2 font-medium">Date</th>
                <th scope="col" className="px-3 py-2 font-medium">Description</th>
                <th scope="col" className="px-3 py-2 font-medium">Reference</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Amount</th>
                <th scope="col" className="px-3 py-2">
                  <span className="sr-only">Remove</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={row.key} className="border-b last:border-0">
                  <td className="px-3 py-1.5">
                    <Input
                      aria-label={`Date of line ${i + 1}`}
                      type="date"
                      value={row.dt}
                      onChange={(e) => edit(row.key, { dt: e.target.value })}
                      className="h-8 w-[9.5rem] tnum"
                    />
                  </td>
                  <td className="px-3 py-1.5">
                    <Input
                      aria-label={`Description of line ${i + 1}`}
                      value={row.description}
                      onChange={(e) => edit(row.key, { description: e.target.value })}
                      className="h-8 min-w-[12rem]"
                    />
                  </td>
                  <td className="px-3 py-1.5">
                    <Input
                      aria-label={`Reference of line ${i + 1}`}
                      value={row.reference ?? ""}
                      onChange={(e) => edit(row.key, { reference: e.target.value })}
                      className="h-8 w-32"
                    />
                  </td>
                  <td className="px-3 py-1.5">
                    <Input
                      aria-label={`Amount of line ${i + 1}`}
                      value={row.amount}
                      onChange={(e) => edit(row.key, { amount: e.target.value })}
                      className="h-8 w-32 text-right tnum"
                    />
                  </td>
                  <td className="px-3 py-1.5 text-right">
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      aria-label={`Drop line ${i + 1}`}
                      onClick={() => setRows((current) => current.filter((r) => r.key !== row.key))}
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() =>
              setRows((current) => [
                ...current,
                {
                  key: `new-${current.length}-${Date.now()}`,
                  dt: periodEnd,
                  description: "",
                  amount: "0",
                  currency: statement.commodity,
                  reference: null,
                  raw: null,
                  source_key: null,
                },
              ])
            }
          >
            <Plus className="size-3.5" />
            Add a line
          </Button>

          {/* Lines vs closing minus opening, updated while typing. */}
          {sums === null ? (
            <p className="text-sm text-muted-foreground">
              One of these amounts cannot be read as a number yet, so they cannot be added up.
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">
              These {rows.length === 1 ? "line sums" : "lines sum"} to{" "}
              <Amount micros={sums.total} precision={precision} commodity={statement.commodity} />, and closing
              less opening is{" "}
              <Amount micros={sums.movement} precision={precision} commodity={statement.commodity} />.{" "}
              {sums.agrees
                ? "The statement balances."
                : "A statement whose lines do not account for its own movement is refused."}
            </p>
          )}
        </div>

        {outside.length > 0 ? (
          <p className="text-sm text-muted-foreground">
            {outside.length === 1 ? "One line is" : `${outside.length} lines are`} dated outside{" "}
            {longDate(periodStart)} to {longDate(periodEnd)}. Every line has to fall inside the period the
            statement declares.
          </p>
        ) : null}
      </div>

      <div className="space-y-2">
        <h4 className="text-sm font-semibold">The document it was read off</h4>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            size="sm"
            variant={source === SAME_FILE ? "default" : "outline"}
            onClick={() => setSource(SAME_FILE)}
          >
            The same document
          </Button>
          <Button
            type="button"
            size="sm"
            variant={source === NEW_FILE ? "default" : "outline"}
            onClick={() => setSource(NEW_FILE)}
          >
            A different file
          </Button>
        </div>

        {source === SAME_FILE ? (
          <p className="text-sm text-muted-foreground">
            <span className="font-medium text-foreground">{statement.document.filename}</span> — the file this
            statement was already read off. A document is identified by its contents and stands behind one live
            statement at a time, so re-reading these same bytes is allowed here and only here: the statement
            being replaced is the one that releases them.
          </p>
        ) : (
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="replace-file">Choose the file</Label>
              <Input
                id="replace-file"
                type="file"
                onChange={(e) => choose(e.target.files?.[0])}
                className="h-auto py-1.5"
              />
              <p className="text-xs text-muted-foreground">
                Read here to take its name, its type, its length and the SHA-256 of its contents. The file is
                not uploaded: this ledger records what a document is and where it is kept, and never holds a
                copy it did not already have.
              </p>
            </div>
            {chosen ? (
              <p className="text-sm text-muted-foreground">
                <span className="font-medium text-foreground">{chosen.filename}</span> · {chosen.media_type} ·{" "}
                {chosen.size_bytes} bytes
                <br />
                <span className="break-all font-mono text-xs">{chosen.sha256}</span>
              </p>
            ) : null}
            <div className="space-y-1">
              <Label htmlFor="replace-ref">Where the original is kept</Label>
              <Input
                id="replace-ref"
                value={externalRef}
                onChange={(e) => setExternalRef(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                A URI or a document id — whatever lets somebody reading these books in five years find the file
                again. Recorded exactly as written and never fetched.
              </p>
            </div>
          </div>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-[1fr_8rem]">
        <div className="space-y-1">
          <Label htmlFor="replace-locator">Where the closing balance is printed</Label>
          <Input id="replace-locator" value={locator} onChange={(e) => setLocator(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="replace-page">Page</Label>
          <Input
            id="replace-page"
            type="number"
            min={1}
            value={page}
            onChange={(e) => setPage(e.target.value)}
            className="tnum"
          />
        </div>
      </div>

      <div className="space-y-1">
        <Label htmlFor="replace-reason">Why this statement is being replaced</Label>
        <Input id="replace-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        <p className="text-xs text-muted-foreground">
          Kept verbatim on the record, beside what was believed before it. It is the sentence that explains why
          two statements cover the same days.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          disabled={!ready || replace.isPending}
          onClick={() =>
            replace.mutate(undefined as never, {
              onSuccess: (result) => {
                setReason("")
                onReplaced(result.statement)
                toast.success("Statement replaced", {
                  description: `The books now read ${result.statement.line_count} lines for `
                    + `${longDate(result.statement.period_start)} to `
                    + `${longDate(result.statement.period_end)}.`,
                })
              },
            })
          }
        >
          {source === NEW_FILE ? <FileUp className="size-4" /> : <Replace className="size-4" />}
          Replace this statement
        </Button>
        {!reason.trim() ? (
          <p className="text-sm text-muted-foreground">A replacement is not recorded without a reason.</p>
        ) : source === NEW_FILE && !document ? (
          <p className="text-sm text-muted-foreground">
            Choose the file and say where it is kept, and this can be submitted.
          </p>
        ) : null}
      </div>

      {replace.error ? (
        <ReplacementRefusal entity={entity} error={replace.error} lines={lines} precision={precision} />
      ) : null}
    </section>
  )
}
