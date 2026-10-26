import { ChevronLeft, ChevronRight } from "lucide-react"

import type { Granularity, Period } from "@/lib/period"
import { hasBegun, periodContaining, periodName, shiftPeriod, yearOf } from "@/lib/period"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// `ytd` isn't a separate granularity: it's the current year, and the to-date rule already handles a year that
// hasn't finished.  It gets a shortcut because it's the period reviewers ask for most.
type Shortcut = Granularity | "ytd"

const SHORTCUTS: { value: Shortcut; label: string }[] = [
  { value: "ytd", label: "YTD" },
  { value: "year", label: "Year" },
  { value: "quarter", label: "Quarter" },
  { value: "month", label: "Month" },
  { value: "custom", label: "Custom" },
]

/** Picks the period the statements cover.
 *
 * Switching granularity keeps your place: `Q2 2026` → Month gives June 2026 (the month containing the old
 * period's end), not January.
 */
export function PeriodNav({
  period,
  onChange,
  today,
}: {
  period: Period
  onChange: (period: Period) => void
  today: string
}) {
  const custom = period.granularity === "custom"
  // no stepping into a period that hasn't begun
  const next = shiftPeriod(period, 1)
  const forward = hasBegun(next, today)
  // YTD is the year granularity on the current year.  Deriving the highlighted button from the dates (rather
  // than storing a fifth kind) means stepping back from YTD lands on a whole year and highlights "Year".
  const thisYear = period.granularity === "year" && yearOf(period.start) === yearOf(today)
  const active: Shortcut = thisYear ? "ytd" : period.granularity

  const pick = (choice: Shortcut) => {
    if (choice === active) return
    if (choice === "ytd") return onChange(periodContaining("year", today))
    // from YTD, "Year" means the last finished year
    if (choice === "year" && thisYear) return onChange(periodContaining("year", `${yearOf(today) - 1}-01-01`))
    // otherwise take the period of the new kind that contains the old period's end
    onChange(choice === "custom" ? { ...period, granularity: choice } : periodContaining(choice, period.end))
  }

  return (
    <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center sm:justify-between">
      <div role="group" aria-label="Period length" className="inline-flex rounded-lg border bg-card p-0.5">
        {SHORTCUTS.map((s) => (
          <button
            key={s.value}
            type="button"
            aria-pressed={active === s.value}
            onClick={() => pick(s.value)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm transition-colors",
              active === s.value
                ? "bg-primary font-medium text-primary-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {s.label}
          </button>
        ))}
      </div>

      {custom ? (
        <div className="flex items-center gap-2">
          <Input
            type="date"
            aria-label="Period start"
            value={period.start}
            max={period.end < today ? period.end : today}
            onChange={(e) => e.target.value && onChange({ ...period, start: e.target.value })}
            className="w-[10.5rem] tnum"
          />
          <span className="text-sm text-muted-foreground">to</span>
          <Input
            type="date"
            aria-label="Period end"
            value={period.end}
            min={period.start}
            max={today}
            onChange={(e) => e.target.value && onChange({ ...period, end: e.target.value })}
            className="w-[10.5rem] tnum"
          />
        </div>
      ) : (
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="icon"
            aria-label="Previous period"
            onClick={() => onChange(shiftPeriod(period, -1))}
          >
            <ChevronLeft className="size-4" />
          </Button>
          <div
            aria-live="polite"
            className="min-w-[9rem] rounded-md border bg-card px-3 py-1.5 text-center text-sm font-medium"
          >
            {periodName(period)}
          </div>
          <Button
            variant="outline"
            size="icon"
            aria-label="Next period"
            disabled={!forward}
            onClick={() => onChange(next)}
          >
            <ChevronRight className="size-4" />
          </Button>
        </div>
      )}
    </div>
  )
}
