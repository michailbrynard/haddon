import { Plus, Scale, X } from "lucide-react"

import { usePrecision } from "@/lib/queries"
import { addMicros, amount, decimalForWire, isZero, negate } from "@/lib/format"
import type { Leg } from "@/lib/api"
import { AccountPicker } from "@/components/AccountPicker"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/** A leg as the form holds it: an account id and a typed decimal string.  `legsForWire` converts it to what
 *  the service takes. */
export interface DraftLeg {
  accountId: string | null
  amount: string
  memo: string
}

export const emptyLeg = (): DraftLeg => ({ accountId: null, amount: "", memo: "" })

/** Whether the leg has been started: it has an account or an amount.  A memo alone doesn't count. */
const begun = (leg: DraftLeg) => Boolean(leg.accountId) || leg.amount.trim() !== ""

/** Whether the leg has both an account and an amount.  `legsForWire` drops legs that don't, so forms must not
 *  submit while one is on screen. */
const finished = (leg: DraftLeg) => Boolean(leg.accountId) && leg.amount.trim() !== ""

/** A decimal string as micros, or null if it isn't a number this commodity can hold.
 *
 * String arithmetic, not `parseFloat`: a running total in floating point could show a rounding artefact in the
 * figure that is meant to show the entry balances.  The service refuses floats for the same reason.
 */
export function microsOf(decimal: string, precision: number): string | null {
  const text = decimal.trim()
  if (!/^-?\d+(\.\d+)?$/.test(text)) return null
  const negative = text.startsWith("-")
  const [whole, fraction = ""] = text.replace("-", "").split(".")
  if (fraction.length > precision) return null
  const scaled = `${whole}${fraction.padEnd(6, "0").slice(0, 6)}`.replace(/^0+(?=\d)/, "")
  return negative && scaled !== "0" ? `-${scaled}` : scaled
}

/** An integer micro string as an exact decimal, or null if it can't be written at this precision.
 *
 * `decimalForWire` throws on a figure finer than the commodity holds.  That's right for a value going to the
 * service but not inside a render or click handler: a caller-supplied `fixed` leg isn't guaranteed to be round,
 * and the page shouldn't crash over it.
 */
function exactly(micros: string, precision: number): string | null {
  try {
    return decimalForWire(micros, precision)
  } catch {
    return null
  }
}

/** The legs as the service takes them: account paths and exact decimal amounts (issue 0006).
 *
 * Half-filled legs are dropped.  That's only safe because `legsAreComplete` blocks submitting while one is on
 * screen; otherwise the posted entry would differ from the one the reviewer saw.
 */
export function legsForWire(
  legs: DraftLeg[],
  accounts: { id: string; path: string }[],
  commodity: string,
): Leg[] {
  return legs
    .filter(finished)
    .map((leg) => ({
      account: accounts.find((a) => a.id === leg.accountId)?.path ?? leg.accountId!,
      commodity,
      amount: leg.amount.trim(),
      memo: leg.memo.trim() || null,
    }))
}

/**
 * Editor for an entry's legs, showing the running balance as amounts are typed.
 *
 * Shared by the screens that write an entry from scratch (a manual adjustment, a split booking), so they can't
 * drift apart on what "balanced" means.
 *
 * The running total is only a help.  `unbalanced` is the refusal people hit most on a multi-leg form, so the
 * residual is shown as they type and "balance the last leg" fills the last row with what's outstanding.  But
 * the service owns the balancing rule (per commodity, at its declared precision), and there should be only one
 * implementation of it.  So callers gate submit on `legsAreComplete`, which only asks about the form, and let
 * the service refuse an unbalanced entry.
 *
 * An empty form reports nothing rather than "the legs balance".  An amount the commodity can't hold is flagged
 * on its own field instead of being left out of the sum, so the total never covers only part of what is shown.
 *
 * Fields have no example text inside them; each says what it wants in its label.  Example values in an input
 * can be mistaken for something already typed.
 */
export function LegEditor({
  entity,
  commodity,
  legs,
  onChange,
  /** A caller-fixed leg (a statement line's cash side), shown read-only.  It counts in the residual but is
   *  never returned by `legsForWire`; the service takes it from the line. */
  fixed,
  idPrefix = "leg",
}: {
  entity: string
  commodity: string
  legs: DraftLeg[]
  onChange: (legs: DraftLeg[]) => void
  fixed?: { label: string; amountMicros: string } | null
  idPrefix?: string
}) {
  const precision = usePrecision(entity)(commodity)

  // Parse every amount on screen, per leg, regardless of whether the leg has an account (that's
  // `legsAreComplete`'s job).  An unreadable amount is flagged on its own field rather than dropped from the sum.
  const micros = legs.map((leg) => (leg.amount.trim() === "" ? null : microsOf(leg.amount, precision)))
  const unreadable = legs.map((leg, i) => leg.amount.trim() !== "" && micros[i] === null)
  const readable = micros.filter((m): m is string => m !== null)
  const anyUnreadable = unreadable.some(Boolean)

  const total = addMicros([...(fixed ? [fixed.amountMicros] : []), ...readable])
  const balanced = isZero(total)
  // Say nothing about balance until something is entered.  A fixed leg counts as entered.
  const arithmeticToReport = Boolean(fixed) || legs.some((leg) => leg.amount.trim() !== "")

  // The amount the last row needs for the entry to net to zero.  Not `-total`: the total already includes the
  // last row's current amount, so that would double-count it.
  const lastMicros = legs.length > 0 ? (micros[legs.length - 1] ?? "0") : "0"
  const settlement = legs.length > 0 ? exactly(addMicros([lastMicros, negate(total)]), precision) : null

  const update = (index: number, patch: Partial<DraftLeg>) =>
    onChange(legs.map((leg, i) => (i === index ? { ...leg, ...patch } : leg)))

  return (
    <div className="space-y-3">
      {fixed ? (
        <div className="flex items-baseline justify-between gap-3 rounded-md border bg-muted/40 px-3 py-2 text-sm">
          <span className="text-muted-foreground">{fixed.label}</span>
          <span className="tnum font-medium">{amount(fixed.amountMicros, precision)}</span>
        </div>
      ) : null}

      <ul className="space-y-3">
        {legs.map((leg, index) => (
          <li key={index} className="space-y-2 rounded-md border px-3 py-3">
            <div className="flex items-end gap-2">
              <div className="min-w-0 flex-1 space-y-1">
                <Label htmlFor={`${idPrefix}-account-${index}`} className="text-xs">
                  Account
                </Label>
                <AccountPicker
                  id={`${idPrefix}-account-${index}`}
                  entity={entity}
                  commodity={commodity}
                  value={leg.accountId}
                  onChange={(accountId) => update(index, { accountId })}
                />
              </div>
              <div className="w-36 space-y-1">
                <Label htmlFor={`${idPrefix}-amount-${index}`} className="text-xs">
                  Amount
                </Label>
                <Input
                  id={`${idPrefix}-amount-${index}`}
                  value={leg.amount}
                  inputMode="decimal"
                  autoComplete="off"
                  aria-invalid={unreadable[index] || undefined}
                  aria-describedby={unreadable[index] ? `${idPrefix}-amount-${index}-problem` : undefined}
                  onChange={(e) => update(index, { amount: e.target.value })}
                  className="tnum text-right"
                />
              </div>
              {legs.length > 1 ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove leg ${index + 1}`}
                  onClick={() => onChange(legs.filter((_, i) => i !== index))}
                >
                  <X className="size-4" />
                </Button>
              ) : null}
            </div>
            {/* Flag the unreadable amount on its own row; otherwise it would silently drop out of the sum. */}
            {unreadable[index] ? (
              <p id={`${idPrefix}-amount-${index}-problem`} className="text-xs text-destructive">
                This amount is not in the sum. {commodity} is kept to {precision} decimal places, written with
                digits, a full stop and a leading minus for a credit.
              </p>
            ) : null}
            <div className="space-y-1">
              <Label htmlFor={`${idPrefix}-memo-${index}`} className="text-xs">
                Note on this leg, if it needs one
              </Label>
              <Input
                id={`${idPrefix}-memo-${index}`}
                value={leg.memo}
                autoComplete="off"
                onChange={(e) => update(index, { memo: e.target.value })}
              />
            </div>
          </li>
        ))}
      </ul>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button type="button" variant="outline" size="sm" onClick={() => onChange([...legs, emptyLeg()])}>
          <Plus className="size-3.5" />
          Another account
        </Button>
        <div className="flex items-center gap-2 text-sm">
          {!arithmeticToReport ? null : anyUnreadable ? (
            <span className="text-attention">The amounts do not all read as numbers yet</span>
          ) : balanced ? (
            <span className="text-muted-foreground">The legs balance.</span>
          ) : (
            <>
              <span className="text-attention">
                <span className="tnum">{amount(total, precision)}</span> is unaccounted for
              </span>
              {settlement !== null ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    onChange(
                      legs.map((leg, i) =>
                        i === legs.length - 1 ? { ...leg, amount: settlement } : leg,
                      ),
                    )
                  }
                >
                  <Scale className="size-3.5" />
                  Balance the last leg
                </Button>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * Whether the form is complete: at least one full leg, and no half-filled ones.
 *
 * Gate submit buttons on this, not on the arithmetic.  Half-filled rows matter because `legsForWire` drops
 * them, so submitting would post an entry different from the one on screen.
 *
 * Whether the legs balance is the service's call, per commodity and at its declared precision.
 */
export function legsAreComplete(legs: DraftLeg[]): boolean {
  const started = legs.filter(begun)
  return started.length > 0 && started.every(finished)
}

/** Whether the legs net to zero at this precision, as far as the form can tell.  For display only; the
 *  service decides.  Returns false if any amount can't be read, rather than leaving that row out. */
export function legsBalance(legs: DraftLeg[], precision: number, fixedMicros?: string | null): boolean {
  const typed = legs.filter((leg) => leg.amount.trim() !== "")
  if (typed.length === 0) return false
  const micros = typed.map((leg) => microsOf(leg.amount, precision))
  if (micros.some((m) => m === null)) return false
  return isZero(addMicros([...(fixedMicros ? [fixedMicros] : []), ...(micros as string[])]))
}

/** Complete and locally balanced.  The service still decides whether it balances. */
export function legsArePostable(
  legs: DraftLeg[],
  precision: number,
  fixedMicros?: string | null,
): boolean {
  return legsAreComplete(legs) && legsBalance(legs, precision, fixedMicros)
}
