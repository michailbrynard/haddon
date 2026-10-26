import { useState } from "react"
import { useParams, useSearchParams } from "react-router-dom"
import { AlertTriangle, Check, CircleHelp, FileLock2, Lock, LockOpen } from "lucide-react"
import { toast } from "sonner"

import { api, uiHref } from "@/lib/api"
import { useCanClose, useCloseChecks, useCloseRecord, useEntity, useEntityMutation } from "@/lib/queries"
import { longDate, timestamp } from "@/lib/format"
import { today } from "@/lib/period"
import { cn } from "@/lib/utils"
import type { CloseCheck, CloseRecord } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ErrorState, LoadingRows } from "@/components/states"

/**
 * Closing and reopening periods.
 *
 * A separate page from the month review because the checks cover the whole span a close would newly close,
 * from the current lock through the chosen date (with no prior lock, every month in the journal).  Next to one
 * month's lines, findings about January would appear under a March heading.
 *
 * Closing used to be API-only, to avoid a second close path with a different record behind it.  Since issue
 * 0005 there is one `close_period` handler and the browser uses the same route as any other caller, so that
 * concern no longer applies.
 *
 * The page decides nothing itself.  Checks are shown as the service evaluated them, an override is a reason
 * the reviewer writes and the service stores, and a refused close is explained in the service's words.
 */
export function Close() {
  const { entity = "" } = useParams()
  const [params, setParams] = useSearchParams()
  const through = params.get("through") ?? today()
  const checks = useCloseChecks(entity, through)
  const { data: summary } = useEntity(entity)
  const canWrite = useCanClose(entity)

  const [reason, setReason] = useState("")
  const [overrides, setOverrides] = useState<Record<string, string>>({})
  const [reopenTo, setReopenTo] = useState("")
  const [reopenReason, setReopenReason] = useState("")

  const lockThrough = summary?.lock_through ?? checks.data?.prior_through ?? null
  const standingClose = useCloseRecord(entity, summary?.lock_close_id)
  const blocking = (checks.data?.checks ?? []).filter((g) => g.blocking)
  const unanswered = blocking.filter((g) => !overrides[g.check]?.trim())

  const close = useEntityMutation(entity, () =>
    api.closePeriod(entity, {
      through,
      reason: reason.trim(),
      expected_version: summary?.lock_version ?? 1,
      overrides: blocking.map((g) => ({ check: g.check, reason: overrides[g.check].trim() })),
    }),
  )
  const reopen = useEntityMutation(entity, () =>
    api.reopenPeriod(entity, {
      through: reopenTo,
      reason: reopenReason.trim(),
      expected_version: summary?.lock_version ?? 1,
    }),
  )

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight">Closing</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {lockThrough
              ? `The books are shut through ${longDate(lockThrough)}.`
              : "Nothing is closed yet, so a first close covers everything the journal holds."}
          </p>
        </div>
        <div className="flex items-end gap-2">
          <div className="space-y-1">
            <Label htmlFor="through" className="text-xs text-muted-foreground">
              Close through
            </Label>
            <Input
              id="through"
              type="date"
              value={through}
              max={today()}
              min={lockThrough ?? undefined}
              onChange={(e) => e.target.value && setParams({ through: e.target.value })}
              className="w-[10.5rem] tnum"
            />
          </div>
        </div>
      </div>

      {standingClose.data ? (
        <ClosedOn
          record={standingClose.data}
          titles={Object.fromEntries((checks.data?.checks ?? []).map((g) => [g.check, g.title]))}
        />
      ) : null}

      {checks.isPending ? (
        <LoadingRows rows={4} />
      ) : checks.error ? (
        <ErrorState error={checks.error} />
      ) : checks.data ? (
        <>
          <Verdict eligible={checks.data.eligible} blocking={blocking.length} />

          <p className="text-sm text-muted-foreground">
            This close would newly shut {longDate(checks.data.window_start)} through {longDate(through)} —{" "}
            {checks.data.months.length} {checks.data.months.length === 1 ? "month" : "months"}.
          </p>

          <ul className="space-y-3">
            {checks.data.checks.map((check) => (
              <GateCard
                key={check.check}
                check={check}
                canWrite={canWrite === true}
                override={overrides[check.check] ?? ""}
                onOverride={(value) => setOverrides((o) => ({ ...o, [check.check]: value }))}
              />
            ))}
          </ul>

          {canWrite === false ? (
            <p className="text-sm text-muted-foreground">
              This credential can read these checks but cannot close.
            </p>
          ) : (
            <section className="space-y-3 rounded-xl border p-5">
              <h3 className="font-medium">Close the period</h3>
              <div className="space-y-1">
                <Label htmlFor="close-reason">Why</Label>
                <Input id="close-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
                <p className="text-xs text-muted-foreground">
                  Why this period is being closed. Kept verbatim on the record: it is the sentence somebody
                  reads years from now.
                </p>
              </div>
              {unanswered.length > 0 ? (
                <p className="text-sm text-muted-foreground">
                  {unanswered.length === 1
                    ? "One check is blocking and has no reason to close over it yet."
                    : `${unanswered.length} checks are blocking and have no reason to close over them yet.`}
                </p>
              ) : null}
              <Button
                disabled={!reason.trim() || unanswered.length > 0 || close.isPending}
                onClick={() =>
                  close.mutate(undefined as never, {
                    onSuccess: (result) => {
                      setReason("")
                      setOverrides({})
                      toast.success("Period closed", {
                        description: `The lock is at ${longDate(result.lock.through ?? through)}, against `
                          + `snapshot ${result.snapshot.id}.`,
                      })
                    },
                  })
                }
              >
                <Lock className="size-4" />
                {blocking.length > 0 ? "Close on a recorded exception" : "Close"}
              </Button>
              {close.error ? <ErrorState error={close.error} /> : null}
            </section>
          )}

          {canWrite && lockThrough ? (
            <section className="space-y-3 rounded-xl border p-5">
              <h3 className="font-medium">Reopen a closed period</h3>
              <p className="text-sm text-muted-foreground">
                Exceptional, and not gated on the checks above — the month that most needs reopening is the one
                that cannot pass them. Nothing is undone: the close and its snapshot stay exactly where they are.
              </p>
              <div className="flex flex-wrap items-end gap-3">
                <div className="space-y-1">
                  <Label htmlFor="reopen-to">Back to</Label>
                  <Input
                    id="reopen-to"
                    type="date"
                    value={reopenTo}
                    max={lockThrough}
                    onChange={(e) => setReopenTo(e.target.value)}
                    className="w-[10.5rem] tnum"
                  />
                </div>
                <div className="flex-1 space-y-1">
                  <Label htmlFor="reopen-reason">Why this period is being opened again</Label>
                  <Input
                    id="reopen-reason"
                    value={reopenReason}
                    onChange={(e) => setReopenReason(e.target.value)}
                  />
                </div>
                <Button
                  variant="outline"
                  disabled={!reopenTo || !reopenReason.trim() || reopen.isPending}
                  onClick={() =>
                    reopen.mutate(undefined as never, {
                      onSuccess: (result) => {
                        setReopenTo("")
                        setReopenReason("")
                        toast.success("Period reopened", {
                          description: `The lock is back at ${longDate(result.lock.through ?? "")}.`,
                        })
                      },
                    })
                  }
                >
                  <LockOpen className="size-4" />
                  Reopen
                </Button>
              </div>
              {reopen.error ? <ErrorState error={reopen.error} /> : null}
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

/**
 * The evidence behind the current lock.
 *
 * Shows the close that set it, the reason given, any checks overridden, and a link to the immutable snapshot
 * the journal was hashed and verified into in the same transaction (issue 0005, acceptance criterion 6).  The
 * snapshot is linked, not summarised.
 *
 * A waiver stores the check's stable key, not its title, since titles can be reworded.  The title is looked up
 * from the currently loaded checks, falling back to the key when the current policy no longer runs that check.
 */
function ClosedOn({ record, titles }: { record: CloseRecord; titles: Record<string, string> }) {
  const waived = record.overrides ?? []
  return (
    <section className="space-y-2 rounded-xl border p-5">
      <div className="flex flex-wrap items-baseline gap-2">
        <FileLock2 className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <h3 className="font-medium">
          {record.kind === "reopen" ? "Reopened" : "Closed"} through {longDate(record.through)}
        </h3>
        <span className="text-xs text-muted-foreground">
          {record.created_by} · {timestamp(record.created_at)}
        </span>
      </div>
      <p className="text-sm">{record.reason}</p>
      {waived.length > 0 ? (
        <ul className="space-y-1 text-sm text-muted-foreground">
          {waived.map((w) => (
            <li key={w.check}>
              Closed over <span className="font-medium">{titles[w.check] ?? w.check}</span> ({w.status_at_close},{" "}
              {w.outstanding_at_close} outstanding) — {w.reason}
            </li>
          ))}
        </ul>
      ) : null}
      <p className="text-sm text-muted-foreground">
        Verified against{" "}
        <a className="underline underline-offset-4" href={uiHref(record.links.snapshot)}>
          snapshot {record.snapshot_id}
        </a>
        , the state of the journal as it stood at this date.
      </p>
    </section>
  )
}

function Verdict({ eligible, blocking }: { eligible: boolean; blocking: number }) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-xl border px-5 py-3.5 text-sm",
        eligible ? "border-settled-border bg-settled-surface" : "border-destructive/30 bg-destructive/5",
      )}
    >
      {eligible ? (
        <>
          <span className="flex size-5 items-center justify-center rounded-full bg-settled/15 text-settled">
            <Check className="size-3.5" />
          </span>
          <span className="font-medium">Every check passed. This period closes on its own merits.</span>
        </>
      ) : (
        <span className="font-medium">
          {blocking === 1 ? "One check is blocking" : `${blocking} checks are blocking`} this close.
        </span>
      )}
    </div>
  )
}

const ICONS: Record<string, typeof Check> = { pass: Check, fail: AlertTriangle, unevaluable: CircleHelp }

/**
 * One check and what it found.
 *
 * `unevaluable` is drawn differently from `fail`: "couldn't tell" is weaker than "here's what's wrong", and
 * drawing them alike would invite overriding a check that hadn't found anything.
 */
function GateCard({
  check,
  canWrite,
  override,
  onOverride,
}: {
  check: CloseCheck
  canWrite: boolean
  override: string
  onOverride: (value: string) => void
}) {
  const Icon = ICONS[check.status] ?? CircleHelp
  return (
    <li className="rounded-xl border">
      <div className="flex items-start gap-3 p-5">
        <Icon
          className={cn(
            "mt-0.5 size-4 shrink-0",
            check.status === "pass" ? "text-settled" : check.status === "fail" ? "text-destructive" : "text-muted-foreground",
          )}
        />
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="font-medium">{check.title}</span>
            <span className="text-xs text-muted-foreground">{check.status}</span>
          </div>
          <p className="text-sm text-muted-foreground">{check.requirement}</p>
          <p className="text-sm">{check.reason}</p>

          {check.outstanding.length > 0 ? (
            <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
              {check.outstanding.map((item, i) => (
                <li key={i} className="tnum">
                  {item.message}
                </li>
              ))}
              {check.outstanding_truncated > 0 ? (
                <li className="italic">and {check.outstanding_truncated} more</li>
              ) : null}
            </ul>
          ) : null}

          {check.blocking && canWrite ? (
            <div className="mt-3 space-y-1">
              <Label htmlFor={`override-${check.check}`} className="text-xs">
                Close over this check, and say why
              </Label>
              <Input
                id={`override-${check.check}`}
                value={override}
                onChange={(e) => onOverride(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Stored on the close with what this check found, and by whom. It applies to this close and no
                other.
              </p>
            </div>
          ) : null}
          {check.waiver ? (
            <p className="mt-2 text-sm text-muted-foreground">Closed over: {check.waiver.reason}</p>
          ) : null}
        </div>
      </div>
    </li>
  )
}
