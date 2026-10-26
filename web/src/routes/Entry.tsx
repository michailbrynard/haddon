import { Link, useNavigate, useParams } from "react-router-dom"
import { useState } from "react"
import { ArrowLeft, ArrowRight, Undo2 } from "lucide-react"
import { toast } from "sonner"

import { api } from "@/lib/api"
import {
  useAccounts,
  useCommand,
  useEntityMutation,
  useEntry,
  useEntryEvents,
  useEntryProvenance,
  usePrecision,
} from "@/lib/queries"
import { addMicros, decimalForWire, isZero, longDate, prettyPath, timestamp } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { CorrectionLink, EntryDetail, EntryState } from "@/lib/types"
import { Amount } from "@/components/Amount"
import { AuditTimeline } from "@/components/AuditTimeline"
import { Evidence } from "@/components/Evidence"
import { ErrorState, LoadingRows } from "@/components/states"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/** A one-sentence explanation of each entry status, and how prominently to draw it.
 *
 * `corrected` and `reversed` differ: both leave the original standing with a mirror beside it, but only
 * `corrected` also posts a replacement.  Merging them would leave a reviewer unsure whether to look for one.
 */
const STATES: Record<EntryState, { label: string; blurb: string; tone: "settled" | "attention" | "muted" }> = {
  posted: {
    label: "In the books",
    blurb: "This entry stands as written. Nothing has superseded it.",
    tone: "settled",
  },
  corrected: {
    label: "Corrected",
    blurb:
      "This entry was restated. It is still in the books, an exact mirror is posted beside it, and a "
      + "replacement says what it should have said. Nothing was edited or deleted.",
    tone: "attention",
  },
  reversed: {
    label: "Reversed",
    blurb:
      "This entry was withdrawn. It is still in the books with an exact mirror beside it, and nothing was "
      + "posted in its place.",
    tone: "attention",
  },
  draft: { label: "Written, not posted", blurb: "This entry is not yet in the sums.", tone: "muted" },
  void: { label: "Void", blurb: "This entry was voided before it reached the books.", tone: "muted" },
}

/**
 * One entry, with its status, legs, evidence and history.
 *
 * Added in issue 0007.  Before it, the register, month review and line detail could show an entry's legs but
 * not what happened to it afterwards; following a register to a reversal showed a lone negative entry with no
 * link to what it undid.
 *
 * Four sections:
 *
 * 1. status: one word, with a sentence explaining it;
 * 2. the legs, with their total shown;
 * 3. evidence: the statement line and documents, openable; and
 * 4. history: the correction chain, then the audit log.
 *
 * Nothing here edits a posted row.  The only writes offered are reverse and correct, which preserve history,
 * and each is shown only if the service publishes it (issue 0006).
 */
export function Entry() {
  const { entity = "", entryId = "" } = useParams()
  const detail = useEntry(entity, entryId)
  const events = useEntryEvents(entity, entryId)
  const provenance = useEntryProvenance(entity, entryId)
  const [correcting, setCorrecting] = useState(false)

  if (detail.isPending) return <LoadingRows rows={8} />
  if (detail.error) return <ErrorState error={detail.error} />

  const d = detail.data
  const { entry, state } = d
  const meaning = STATES[state] ?? STATES.posted
  const reversible = state === "posted"

  return (
    <div className="space-y-6">
      <Link
        to={`/e/${entity}/month?month=${entry.dt.slice(0, 7)}`}
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        Back to the month
      </Link>

      <div>
        <h2 className="text-2xl font-semibold tracking-tight">{entry.narration}</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          {longDate(entry.dt)} · {entry.origin} · written by {entry.created_by} on{" "}
          {timestamp(entry.created_at)}
        </p>
      </div>

      <div
        className={cn(
          "rounded-xl border px-5 py-3.5 text-sm",
          meaning.tone === "settled"
            ? "border-settled-border bg-settled-surface"
            : meaning.tone === "attention"
              ? "border-attention-border bg-attention-surface"
              : "bg-muted/40",
        )}
      >
        <p className="font-medium">{meaning.label}</p>
        <p className="mt-0.5 text-muted-foreground">{meaning.blurb}</p>
      </div>

      <Postings entity={entity} detail={d} />

      {provenance.data && (provenance.data.lines.length > 0 || provenance.data.evidence.length > 0) ? (
        <section className="space-y-3">
          <h3 className="text-sm font-semibold">What it rests on</h3>
          {provenance.data.lines.map((line) => (
            <div key={line.source_row_id} className="space-y-2">
              <p className="text-sm text-muted-foreground">
                Row {line.row_no} of a statement, read as “{line.description}”
              </p>
              <Evidence
                entity={entity}
                documentId={line.document_id}
                document={line.document}
                caption={`The statement this line was read off`}
              />
            </div>
          ))}
          {/* Skip documents already shown as the statement behind a line.  Booking a line also files the
              statement's document as evidence on the entry, so the two lists always overlap. */}
          {provenance.data.evidence
            .filter((filed) => !provenance.data!.lines.some((l) => l.document_id === filed.document_id))
            .map((filed) => (
              <Evidence
                key={filed.evidence_id}
                entity={entity}
                documentId={filed.document_id}
                document={filed.document}
                caption={filed.caption}
                page={filed.page}
                locator={filed.locator}
              />
            ))}
        </section>
      ) : null}

      {d.corrections.length > 0 ? (
        <section className="space-y-3">
          <h3 className="text-sm font-semibold">The correction chain</h3>
          {d.corrections.map((link) => (
            <Chain key={link.id} entity={entity} link={link} />
          ))}
        </section>
      ) : null}

      <section className="space-y-3">
        <h3 className="text-sm font-semibold">What happened to it</h3>
        {events.isPending ? (
          <LoadingRows rows={3} />
        ) : events.error ? (
          <ErrorState error={events.error} />
        ) : (
          <AuditTimeline events={events.data?.items ?? []} />
        )}
      </section>

      <Actions
        entity={entity}
        detail={d}
        reversible={reversible}
        onCorrect={() => setCorrecting(true)}
      />
      <CorrectDialog entity={entity} detail={d} open={correcting} onOpenChange={setCorrecting} />
    </div>
  )
}

/**
 * The legs, with their total.
 *
 * The total is shown so the reviewer can see the entry balances without adding it up themselves.
 */
function Postings({ entity, detail }: { entity: string; detail: EntryDetail }) {
  const precisionOf = usePrecision(entity)
  const { data: accounts } = useAccounts(entity)
  const total = addMicros(detail.entry.postings.map((p) => p.amount_micros))

  return (
    <section className="rounded-xl border bg-card">
      <table className="w-full text-sm">
        <caption className="sr-only">The postings of this entry</caption>
        <thead>
          <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
            <th scope="col" className="px-5 py-2.5 font-medium">
              Account
            </th>
            <th scope="col" className="px-5 py-2.5 text-right font-medium">
              Amount
            </th>
          </tr>
        </thead>
        <tbody>
          {detail.entry.postings.map((posting) => (
            <tr key={posting.id} className="border-b last:border-0">
              <td className="px-5 py-2.5">
                <Link
                  to={`/e/${entity}/accounts/${posting.account_id}`}
                  className="underline-offset-4 hover:underline"
                >
                  {prettyPath(
                    accounts?.find((a) => a.id === posting.account_id)?.path ?? posting.account_path,
                  )}
                </Link>
                {posting.memo ? (
                  <span className="ml-2 text-xs text-muted-foreground">{posting.memo}</span>
                ) : null}
              </td>
              <td className="px-5 py-2.5 text-right">
                <Amount
                  micros={posting.amount_micros}
                  precision={precisionOf(posting.commodity)}
                  commodity={posting.commodity}
                />
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t">
            <td className="px-5 py-2.5 text-xs text-muted-foreground">
              {isZero(total) ? "The legs balance." : "These legs do not sum to nothing."}
            </td>
            <td className="px-5 py-2.5 text-right">
              <Amount micros={total} emphasis className={isZero(total) ? undefined : "text-destructive"} />
            </td>
          </tr>
        </tfoot>
      </table>
    </section>
  )
}

/** One correction-chain link, described from this entry's side. */
function Chain({ entity, link }: { entity: string; link: CorrectionLink }) {
  const other =
    link.role === "original"
      ? link.replacement_id
        ? { id: link.replacement_id, lead: "Replaced by" }
        : { id: link.mirror_id, lead: "Reversed by" }
      : link.role === "mirror"
        ? { id: link.original_id, lead: "This reverses" }
        : { id: link.original_id, lead: "This replaces" }

  return (
    <div className="rounded-xl border px-5 py-3.5">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-sm font-medium">{other.lead}</span>
        <Link
          to={`/e/${entity}/entries/${other.id}`}
          className="inline-flex items-center gap-1 text-sm underline underline-offset-4"
        >
          this entry
          <ArrowRight className="size-3.5" />
        </Link>
        <span className="text-xs text-muted-foreground">
          {link.created_by} · {timestamp(link.created_at)}
        </span>
      </div>
      {link.reason ? <p className="mt-1 text-sm text-muted-foreground">{link.reason}</p> : null}
      {link.role === "original" && link.replacement_id ? (
        <p className="mt-1 text-xs text-muted-foreground">
          The mirror that withdrew the original is{" "}
          <Link
            to={`/e/${entity}/entries/${link.mirror_id}`}
            className="underline underline-offset-4"
          >
            also in the books
          </Link>
          .
        </p>
      ) : null}
    </div>
  )
}

/**
 * Reverse and correct, the only ways a posted entry is changed, each shown only if the service publishes it.
 *
 * Gated on `/capabilities.commands` by name (issue 0006), not a hand-kept list; issue 0006 removed one that had
 * drifted.
 *
 * Neither is offered for an entry that already has a mirror, since the service refuses a second correction.
 */
function Actions({
  entity,
  detail,
  reversible,
  onCorrect,
}: {
  entity: string
  detail: EntryDetail
  reversible: boolean
  onCorrect: () => void
}) {
  const canReverse = useCommand(entity, "reverse_entry")
  const canCorrect = useCommand(entity, "correct_entry")
  const [reason, setReason] = useState("")
  const [confirming, setConfirming] = useState(false)

  const reverse = useEntityMutation(entity, () =>
    api.reverse(entity, detail.entry.id, { reason: reason.trim() }),
  )

  if (!reversible) {
    return (
      <p className="text-sm text-muted-foreground">
        This entry has already been superseded. Corrections apply to what stands in the books, so the
        replacement is where a further change goes.
      </p>
    )
  }
  if (canReverse === false && canCorrect === false) return null

  return (
    <section className="space-y-3 rounded-xl border p-5">
      <h3 className="font-medium">Disagree with this entry</h3>
      <p className="text-sm text-muted-foreground">
        Nothing is edited and nothing is deleted. Either way an exact mirror is posted beside this entry and
        both stay in the books; correcting also posts what the entry should have said, in the same
        transaction.
      </p>
      <div className="flex flex-wrap gap-2">
        {canCorrect ? (
          <Button onClick={onCorrect}>Correct it — say what it should have said</Button>
        ) : null}
        {canReverse ? (
          <Button variant="outline" onClick={() => setConfirming(true)}>
            <Undo2 className="size-4" />
            Reverse it
          </Button>
        ) : null}
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Reverse this entry</DialogTitle>
            <DialogDescription>
              The books will stop saying this, and nothing will be posted in its place. If the entry was
              wrong rather than mistaken, correcting it says what it should have said instead.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 py-2">
            <Label htmlFor="reverse-why">Why this entry is wrong</Label>
            <Input
              id="reverse-why"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">
              In your own words, and kept verbatim on the record. It is the sentence somebody reads years from
              now.
            </p>
          </div>
          {reverse.error ? <ErrorState error={reverse.error} /> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={!reason.trim() || reverse.isPending}
              onClick={() =>
                reverse.mutate(undefined as never, {
                  onSuccess: () => {
                    setConfirming(false)
                    setReason("")
                    toast.success("Reversed", {
                      description: "An exact mirror is posted beside it. Both stay in the books.",
                    })
                  },
                })
              }
            >
              Reverse it
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}

/**
 * Correct an entry: reverse it and post a replacement, in one transaction.
 *
 * The form starts from the entry as it stands, since a correction usually changes one thing.  Only the date and
 * narration can change here; the replacement keeps the legs exactly.  For different accounts or amounts the
 * page points to writing a new entry (`POST /entries`).
 */
function CorrectDialog({
  entity,
  detail,
  open,
  onOpenChange,
}: {
  entity: string
  detail: EntryDetail
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const navigate = useNavigate()
  const precisionOf = usePrecision(entity)
  const [dt, setDt] = useState(detail.entry.dt)
  const [narration, setNarration] = useState(detail.entry.narration)
  const [why, setWhy] = useState("")

  const correct = useEntityMutation(entity, () =>
    api.correctEntry(entity, detail.entry.id, {
      reason: why.trim(),
      replacement: {
        dt,
        narration: narration.trim(),
        legs: detail.entry.postings.map((p) => ({
          account: p.account_path,
          commodity: p.commodity,
          amount: decimalForWire(p.amount_micros, precisionOf(p.commodity)),
          memo: p.memo,
        })),
      },
    }),
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Correct this entry</DialogTitle>
          <DialogDescription>
            The original is reversed and a replacement is posted in one transaction, so there is no moment at
            which the books say neither.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-4">
          <div className="space-y-2">
            <Label htmlFor="correct-dt">Date it belongs on</Label>
            <Input
              id="correct-dt"
              type="date"
              value={dt}
              onChange={(e) => e.target.value && setDt(e.target.value)}
              className="w-[10.5rem] tnum"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="correct-narration">What the entry should say</Label>
            <Input
              id="correct-narration"
              value={narration}
              onChange={(e) => setNarration(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="correct-why">Why the original was wrong</Label>
            <Input
              id="correct-why"
              value={why}
              onChange={(e) => setWhy(e.target.value)}
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">
              Kept verbatim on the correction, beside who made it and when.
            </p>
          </div>
          <p className="text-sm text-muted-foreground">
            The replacement keeps these legs exactly. To book different accounts or different amounts, write
            the entry the books should have and reverse this one.
          </p>
          {correct.error ? <ErrorState error={correct.error} /> : null}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={!why.trim() || !narration.trim() || correct.isPending}
            onClick={() =>
              correct.mutate(undefined as never, {
                onSuccess: (result) => {
                  onOpenChange(false)
                  setWhy("")
                  toast.success("Corrected", {
                    description: "The original was reversed and the replacement posted beside it.",
                  })
                  navigate(`/e/${entity}/entries/${result.replacement_id}`)
                },
              })
            }
          >
            Post the correction
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}