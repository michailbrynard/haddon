import { today } from "@/lib/period"
import { Input } from "@/components/ui/input"

/** The as-at date for a point-in-time report (trial balance, balance sheet).  Capped at today. */
export function AsOfNav({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <Input
      type="date"
      aria-label="As at"
      value={value}
      max={today()}
      onChange={(e) => e.target.value && onChange(e.target.value)}
      className="w-[10.5rem] tnum"
    />
  )
}

/** A from/to date range for flow reports (register, statement).  The inputs' `min`/`max` keep the ends
 *  in order. */
export function RangeNav({
  from,
  to,
  onChange,
}: {
  from: string
  to: string
  onChange: (from: string, to: string) => void
}) {
  return (
    <div className="flex items-center gap-2">
      <Input
        type="date"
        aria-label="From"
        value={from}
        max={to}
        onChange={(e) => e.target.value && onChange(e.target.value, to)}
        className="w-[10.5rem] tnum"
      />
      <span className="text-sm text-muted-foreground">to</span>
      <Input
        type="date"
        aria-label="To"
        value={to}
        min={from}
        max={today()}
        onChange={(e) => e.target.value && onChange(from, e.target.value)}
        className="w-[10.5rem] tnum"
      />
    </div>
  )
}
