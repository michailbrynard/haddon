import { useEffect, useState } from "react"
import { ExternalLink } from "lucide-react"
import { toast } from "sonner"

import { api } from "@/lib/api"
import { useEntityMutation, usePrecision } from "@/lib/queries"
import { decimalForWire, longDate, money, negate, timestamp } from "@/lib/format"
import type { Entry, Line } from "@/lib/types"
import { Amount } from "@/components/Amount"
import { AccountPicker } from "@/components/AccountPicker"
import { ErrorState } from "@/components/states"
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

/** The statement line itself: date, amount and the bank's description.  Shown above both the read view and
 * the form. */
function StatementLine({ line, precision }: { line: Line; precision: number }) {
  return (
    <div className="rounded-md border bg-muted/40 px-3 py-2 text-left">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs text-muted-foreground">{longDate(line.dt)}</span>
        <span className="tnum text-sm font-medium text-foreground">
          {money(line.amount_micros, line.commodity, precision)}
        </span>
      </div>
      <p className="mt-0.5 text-sm text-foreground">{line.description}</p>
    </div>
  )
}

/** One entry as it stands: its narration, both sides, and who wrote it when.
 *
 * `created_by` is a recorded value, the same one the audit trail shows.  Reversed entries are shown (dimmed)
 * rather than hidden, since corrections never delete anything and the reader should be able to see that.
 */
function BookedEntry({ entity, entry }: { entity: string; entry: Entry }) {
  const precisionOf = usePrecision(entity)
  const reversed = Boolean(entry.reversed_by)
  const label = reversed
    ? "Reversed"
    : entry.status === "draft"
      ? "Written, not posted"
      : "In the books"

  return (
    <div className={reversed ? "opacity-60" : undefined}>
      {/* Stacked on a phone, where side by side they'd both wrap; one row from `sm` up. */}
      <div className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between sm:gap-3">
        <span
          className={
            reversed || entry.status !== "draft"
              ? "text-xs font-medium uppercase tracking-wide text-muted-foreground"
              : "text-xs font-medium uppercase tracking-wide text-attention"
          }
        >
          {label}
        </span>
        <span className="text-xs text-muted-foreground">
          {entry.created_by} · {timestamp(entry.created_at)}
        </span>
      </div>
      <p className="mt-1 text-sm text-foreground">{entry.narration}</p>
      <ul className="mt-1.5 space-y-0.5">
        {entry.postings.map((p) => (
          <li key={p.id} className="flex items-baseline justify-between gap-4 text-xs text-muted-foreground">
            <span className="truncate">{p.account_path}</span>
            <Amount
              micros={p.amount_micros}
              precision={precisionOf(p.commodity)}
              commodity={p.commodity}
            />
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Show what a line is booked as, with the option to change it.
 *
 * Opens on the booking as it stands, in read-only text, with "Change the booking" one press away.  Clicking a
 * line used to open the form directly, and prefilled inputs are hard to tell apart from hints.  A line with
 * nothing booked yet opens straight into the form.
 *
 * Only the contra side is chosen.  The cash leg is the statement line (same account, currency and amount), so
 * the booking always agrees with the bank.
 *
 * "What is this?" is recorded as both the entry's description and the reason for the booking; the helper text
 * says so.  Changing a posted booking also asks why the earlier one was wrong, which nobody can reconstruct
 * from the numbers later.
 *
 * No field has example text inside it.  An example sentence in an input looks like recorded content (a
 * reviewer could come away thinking a line is "a client refund" when it isn't).  Labels and helper text say
 * what each field wants.
 */
export function BookingDialog({
  entity,
  line,
  open,
  onOpenChange,
  startEditing = false,
}: {
  entity: string
  line: Line
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Open straight into the form, for menu items that already say "Change the booking". */
  startEditing?: boolean
}) {
  const precisionOf = usePrecision(entity)
  const standing = line.entries.find((e) => e.status === "posted" && !e.reversed_by)
  const draft = line.entries.find((e) => e.status === "draft")
  const current = draft ?? standing
  const isChange = Boolean(current)
  const wasPosted = Boolean(standing) && !draft

  // Nothing recorded against the line, so open on the form.
  const nothingRecorded = !line.explanation

  const contra = current?.postings.filter((p) => p.account_id !== line.account_id) ?? []
  // This form offers a single contra account, so saving a split booking would collapse it.  Refuse instead.
  const split = contra.length > 1

  // Standing entries first, then reversed ones, each newest first.
  const ordered = [...line.entries].sort((a, b) => {
    const superseded = (e: Entry) => (e.reversed_by ? 1 : 0)
    return superseded(a) - superseded(b) || b.created_at.localeCompare(a.created_at)
  })

  const [editing, setEditing] = useState(startEditing || nothingRecorded)
  const [accountId, setAccountId] = useState<string | null>(null)
  const [narration, setNarration] = useState("")
  const [why, setWhy] = useState("")

  useEffect(() => {
    if (!open) return
    setEditing(startEditing || nothingRecorded)
    setAccountId(contra.length === 1 ? contra[0].account_id : null)
    setNarration(current?.narration ?? "")
    setWhy("")
    // Reset when reopened or opened on a different line, so the last line's answers don't carry over.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, line.line_id, startEditing])

  const save = useEntityMutation(entity, async () => {
    if (!accountId) throw new Error("Choose an account first.")
    const legs = [
      {
        account: accountId,
        commodity: line.commodity,
        amount: decimalForWire(negate(line.amount_micros), precisionOf(line.commodity)),
      },
    ]
    if (isChange) {
      return api.reexplain(entity, line.line_id, {
        kind: "booked",
        legs,
        narration: narration.trim(),
        reason: wasPosted ? why.trim() : narration.trim(),
      })
    }
    return api.book(entity, line.line_id, {
      legs,
      narration: narration.trim(),
      reason: narration.trim(),
    })
  })

  const ready =
    !split && Boolean(accountId) && narration.trim().length > 0 && (!wasPosted || why.trim().length > 0)

  function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!ready || save.isPending) return
    save.mutate(undefined, {
      onSuccess: () => {
        toast.success(isChange ? "Booking changed" : "Booked", {
          description: wasPosted
            ? "The old entry was reversed and the replacement posted beside it."
            : "It will be in the books once this month's bookings are posted.",
        })
        onOpenChange(false)
      },
    })
  }

  const title = editing ? (isChange ? "Change this booking" : "Book this line") : "What this line is"

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* The form can be taller than a phone screen, so the dialog scrolls within the viewport. */}
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
        {editing ? (
          <form onSubmit={submit}>
            <DialogHeader>
              <DialogTitle>{title}</DialogTitle>
              <DialogDescription asChild>
                <div className="space-y-1.5 pt-1">
                  <StatementLine line={line} precision={precisionOf(line.commodity)} />
                </div>
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4 py-5">
              {split ? (
                <p className="rounded-md border border-attention-border bg-attention-surface px-3 py-2 text-sm">
                  This line is booked across {contra.length} accounts (
                  {contra.map((p) => p.account_path).join(", ")}). Changing it here would collapse that into
                  one, so it has to be done through the API, where the whole split can be given.
                </p>
              ) : null}
              <div className="space-y-2">
                <Label htmlFor="account">Account</Label>
                <AccountPicker
                  id="account"
                  entity={entity}
                  commodity={line.commodity}
                  excludeAccountId={line.account_id}
                  value={accountId}
                  onChange={setAccountId}
                />
                <p className="text-xs text-muted-foreground">
                  The other side of the booking. The bank side is this line, unchanged.
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="narration">What is this?</Label>
                <Input
                  id="narration"
                  value={narration}
                  onChange={(e) => setNarration(e.target.value)}
                  autoComplete="off"
                />
                <p className="text-xs text-muted-foreground">
                  {isChange
                    ? wasPosted
                      ? "What the standing booking says. Every word here is recorded; change it if it is wrong."
                      : "What the unposted booking says, kept as its description and as the reason it was booked this way."
                    : "Kept as the entry's description and as the reason it was booked this way."}
                </p>
              </div>

              {wasPosted ? (
                <div className="space-y-2">
                  <Label htmlFor="why">Why the earlier booking was wrong</Label>
                  <Input
                    id="why"
                    value={why}
                    onChange={(e) => setWhy(e.target.value)}
                    autoComplete="off"
                  />
                  <p className="text-xs text-muted-foreground">
                    In your own words, and kept verbatim. Nothing is deleted: the old entry is reversed, the
                    new one is posted beside it, and both stay in the books.
                  </p>
                </div>
              ) : null}

              {save.error ? <ErrorState error={save.error} /> : null}
            </div>

            <DialogFooter>
              <Button
                type="button"
                variant="ghost"
                onClick={() => (nothingRecorded ? onOpenChange(false) : setEditing(false))}
              >
                {nothingRecorded ? "Cancel" : "Back"}
              </Button>
              <Button type="submit" disabled={!ready || save.isPending}>
                {save.isPending ? "Saving…" : isChange ? "Save change" : "Book it"}
              </Button>
            </DialogFooter>
          </form>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>{title}</DialogTitle>
              <DialogDescription asChild>
                <div className="space-y-1.5 pt-1">
                  <StatementLine line={line} precision={precisionOf(line.commodity)} />
                </div>
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-4 py-1">
              {line.explanation?.kind === "ignored" ? (
                <div>
                  <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Not booked, on purpose
                  </div>
                  <p className="mt-1 text-sm text-foreground">{line.explanation.reason}</p>
                </div>
              ) : null}

              {line.entries.length ? (
                <div className="space-y-4 divide-y [&>*+*]:pt-4">
                  {ordered.map((entry) => (
                    <BookedEntry key={entry.id} entity={entity} entry={entry} />
                  ))}
                </div>
              ) : !line.explanation ? (
                <p className="text-sm text-attention">Nothing is booked for this line yet.</p>
              ) : null}

              <a
                href={`/e/${entity}/lines/${line.line_id}`}
                className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
              >
                Where did this come from?
                <ExternalLink className="size-3" />
              </a>
            </div>

            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
                Close
              </Button>
              <Button type="button" onClick={() => setEditing(true)}>
                {isChange ? "Change the booking" : "Give it an account"}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

/** A short dialog for writes that need only a reason.
 *
 * Deliberately has no in-field example text; put guidance in `description` or `label`.  An example sentence
 * inside the input reads as something already recorded.
 */
export function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  label,
  confirmLabel,
  onConfirm,
  destructive = false,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  label: string
  confirmLabel: string
  onConfirm: (reason: string) => Promise<unknown>
  destructive?: boolean
}) {
  const [reason, setReason] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    if (open) {
      setReason("")
      setError(null)
    }
  }, [open])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!reason.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await onConfirm(reason.trim())
      onOpenChange(false)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md">
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          <div className="space-y-2 py-5">
            <Label htmlFor="reason">{label}</Label>
            <Input
              id="reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">Kept verbatim in the record.</p>
            {error ? <ErrorState error={error} /> : null}
          </div>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant={destructive ? "destructive" : "default"} disabled={!reason.trim() || busy}>
              {busy ? "Saving…" : confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
