import { useState } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import { toast } from "sonner"

import { ApiError, api } from "@/lib/api"
import { useAccounts, useCapabilities, useCommand, useEntity, useEntityMutation } from "@/lib/queries"
import { amount } from "@/lib/format"
import { cn } from "@/lib/utils"
import { LegEditor, emptyLeg, legsAreComplete, legsForWire } from "@/components/LegEditor"
import type { DraftLeg } from "@/components/LegEditor"
import { EmptyState, ErrorState, LoadingRows } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/** Today in local time, formatted for the date input.  Not `toISOString`, which is UTC and can be a day off
 *  from the writer's own date (e.g. any evening in the Americas). */
function today(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`
}

/**
 * Write an entry with no statement line behind it.
 *
 * Every other write in the UI starts from a bank line, which only covers cash.  Invoices raised, payroll
 * accrued or withholding moved between liabilities had no way in, so a period's profit could only ever be its
 * cash flow (issue 0007 records the gap).
 *
 * Things to keep in mind on this page:
 *
 * 1. It doesn't decide whether the entry balances.  The residual under the legs is a help; the service sums
 *    per commodity at its declared precision and refuses with `unbalanced`.  The submit button only checks
 *    that the form is filled in, and a refusal is shown in the service's words.
 * 2. A refusal leaves the form as it was.  Nothing is cleared until something is posted.  The usual refusals
 *    (locked period, unbalanced entry) are fixed by changing one field, so losing the legs would be painful.
 * 3. It sends account paths and decimal amounts, never ids or micros (the command vocabulary from issue 0006).
 *    `LegEditor` holds account ids while picking; `legsForWire` converts them.
 *
 * The page is gated on `post_entry` being published (issue 0006), so an installation that doesn't serve it
 * doesn't show a form that can only 404.
 */
export function NewEntry() {
  const { entity = "" } = useParams()
  const navigate = useNavigate()
  const canPost = useCommand(entity, "post_entry")
  const capabilities = useCapabilities(entity)
  const { data: summary } = useEntity(entity)
  const { data: accounts = [] } = useAccounts(entity)

  const [dt, setDt] = useState(today)
  const [narration, setNarration] = useState("")
  const [reason, setReason] = useState("")
  const [legs, setLegs] = useState<DraftLeg[]>([emptyLeg(), emptyLeg()])
  const [chosen, setChosen] = useState<string | null>(null)

  const commodities = capabilities.data?.commodities ?? []
  // Default to the entity's base currency, or the first declared commodity if the entity listing hasn't loaded.
  const commodity = chosen ?? summary?.base_currency ?? commodities[0]?.code ?? ""

  const post = useEntityMutation(entity, () =>
    api.postEntry(entity, {
      dt,
      narration: narration.trim(),
      reason: reason.trim(),
      legs: legsForWire(legs, accounts, commodity),
    }),
  )

  if (canPost === undefined || capabilities.isPending) return <LoadingRows rows={8} />
  if (capabilities.error) return <ErrorState error={capabilities.error} />
  if (!canPost) {
    return (
      <EmptyState title="These books are not written to from here">
        This installation does not serve the command that writes an entry, so there is nothing this page could
        post. Entries reach the books through whatever does write to them.
      </EmptyState>
    )
  }

  // Deliberately doesn't check that the legs balance (see the component comment); only that the form is
  // filled in.
  const ready = Boolean(dt) && narration.trim() !== "" && reason.trim() !== "" && legsAreComplete(legs)

  return (
    <div className="space-y-6">
      <Link
        to={`/e/${entity}/month?month=${dt.slice(0, 7)}`}
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        Back to the month
      </Link>

      <div>
        <h2 className="text-2xl font-semibold tracking-tight">Write an entry</h2>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
          For something the books have to say that no bank statement says: an invoice raised, a cost accrued,
          an amount moved from one account to another. It is posted when you write it, and changed afterwards
          the only way a posted entry ever is — reversed, or corrected, with both halves left standing.
        </p>
      </div>

      <form
        className="space-y-6"
        onSubmit={(event) => {
          event.preventDefault()
          if (!ready || post.isPending) return
          post.mutate(undefined as never, {
            onSuccess: (result) => {
              toast.success("Posted", { description: "It is in the books." })
              navigate(`/e/${entity}/entries/${result.entry.id}`)
            },
          })
        }}
      >
        <section className="space-y-4 rounded-xl border p-5">
          <div className="space-y-2">
            <Label htmlFor="entry-dt">The date it belongs on</Label>
            <Input
              id="entry-dt"
              type="date"
              value={dt}
              onChange={(e) => e.target.value && setDt(e.target.value)}
              className="w-[10.5rem] tnum"
            />
            <p className="text-xs text-muted-foreground">
              When the thing happened, not when you are recording it. A date inside a closed period is
              refused.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="entry-narration">What the entry says</Label>
            <Input
              id="entry-narration"
              value={narration}
              onChange={(e) => setNarration(e.target.value)}
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">
              How it will read in the register, the account and every export. Write it for whoever opens these
              books without you in the room.
            </p>
          </div>

          {commodities.length > 1 ? (
            <div className="space-y-2">
              <Label>The currency it is in</Label>
              <div className="flex flex-wrap gap-1.5">
                {commodities.map((option) => (
                  <button
                    key={option.code}
                    type="button"
                    onClick={() => setChosen(option.code)}
                    aria-pressed={option.code === commodity}
                    className={cn(
                      "min-h-9 rounded-md border px-3 text-sm transition-colors",
                      option.code === commodity
                        ? "border-primary bg-primary/10 font-medium"
                        : "border-border hover:bg-muted/50",
                    )}
                  >
                    {option.code}
                  </button>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                Every leg of one entry is in one currency here, and only accounts that hold it are offered.
                {commodity ? ` ${commodity} is kept to ${precisionOf(commodities, commodity)} decimal places.` : ""}
              </p>
            </div>
          ) : null}
        </section>

        <section className="space-y-3 rounded-xl border p-5">
          <div>
            <h3 className="font-medium">What it moves</h3>
            <p className="mt-0.5 text-sm text-muted-foreground">
              Two accounts at least, and as many as it takes. A positive amount is what the account receives
              and a negative one is what it gives up; the two sides net to nothing, which is the thing these
              books are checked against.
            </p>
          </div>
          <LegEditor
            entity={entity}
            commodity={commodity}
            legs={legs}
            onChange={setLegs}
            idPrefix="entry-leg"
          />
        </section>

        <section className="space-y-2 rounded-xl border p-5">
          <Label htmlFor="entry-reason">Why you are writing it</Label>
          <Input
            id="entry-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            autoComplete="off"
          />
          <p className="text-xs text-muted-foreground">
            Not the same sentence as the one above: that one is what the entry says, this one is why it exists.
            It is kept verbatim on the record and never edited afterwards
            {capabilities.data?.actor_recorded_as
              ? `, beside your name — this will be recorded as ${capabilities.data.actor_recorded_as}.`
              : "."}
          </p>
        </section>

        {post.error ? <Refusal error={post.error} commodities={commodities} /> : null}

        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={!ready || post.isPending}>
            {post.isPending ? "Posting…" : "Post it to the books"}
          </Button>
          <p className="text-sm text-muted-foreground">
            It goes straight into the books. Nothing here is a draft.
          </p>
        </div>
      </form>
    </div>
  )
}

function precisionOf(commodities: { code: string; precision: number }[], code: string): number {
  return commodities.find((c) => c.code === code)?.precision ?? 2
}

/**
 * A refusal in the service's words, plus its figures where it gave any.
 *
 * For `unbalanced`, the per-commodity residual comes from the error's `details`, computed by the code that
 * refused the write.  Recomputing it from the form would be a second definition of "unbalanced", which could
 * disagree as soon as an entry had legs in two currencies.
 */
function Refusal({
  error,
  commodities,
}: {
  error: unknown
  commodities: { code: string; precision: number }[]
}) {
  const refusal = error instanceof ApiError ? error : null
  const sums = refusal?.code === "unbalanced"
    ? refusal.details.filter((d) => typeof d.commodity === "string" && typeof d.sum === "string")
    : []

  return (
    <div className="space-y-2">
      <ErrorState error={error} />
      {sums.length > 0 ? (
        <ul className="space-y-1 pl-1 text-sm text-muted-foreground">
          {sums.map((d) => (
            <li key={String(d.commodity)}>
              The {String(d.commodity)} legs come to{" "}
              <span className="tnum">{amount(String(d.sum), precisionOf(commodities, String(d.commodity)))}</span>{" "}
              between them, and an entry has to come to nothing. Nothing was written.
            </li>
          ))}
        </ul>
      ) : null}
      {refusal?.code === "period_locked" ? (
        <p className="pl-1 text-sm text-muted-foreground">
          The books are shut through that date. Move the entry to an open one, or reopen the period first;
          what you have written here is kept either way.
        </p>
      ) : null}
    </div>
  )
}
