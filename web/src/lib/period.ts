/** The period a report covers, and date arithmetic for stepping between periods.
 *
 * The journal is continuous, so any valid date range can be reported on; there is no book year to stay inside.
 * An income statement covers a period while a balance sheet is at a date, so one selection drives both: flows
 * between `start` and `end`, and the position at `end`.
 *
 * Year, quarter and month are calendar shortcuts, not boundaries the service imposes.  A custom range is just
 * as valid.
 */

export type Granularity = "year" | "quarter" | "month" | "custom"

/** Today's date in UTC, the clock the service stamps writes with. */
export function today(): string {
  return new Date().toISOString().slice(0, 10)
}

export interface Period {
  granularity: Granularity
  start: string
  end: string
  /** The period was still running when requested, so `end` is today rather than its last day. */
  toDate?: boolean
}

/** Whether the period has started.  A future period would only report empty figures, which would mislead. */
export function hasBegun(period: Period, now = today()): boolean {
  return period.start <= now
}

/** The period cut off at today.
 *
 * A period wholly in the past is returned unchanged.  One containing today ends at today and is marked
 * `toDate`, so headings can say so; a running quarter headed just `Q3 2026` would imply figures that don't
 * exist yet.
 */
export function throughToday(period: Period, now = today()): Period {
  return period.end <= now ? period : { ...period, end: now, toDate: true }
}

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
]

const SHORT_MONTHS = MONTHS.map((m) => m.slice(0, 3))

function iso(d: Date): string {
  return d.toISOString().slice(0, 10)
}

function utc(year: number, month: number, day: number): Date {
  return new Date(Date.UTC(year, month, day))
}

function parts(date: string): { year: number; month: number; day: number } {
  const [year, month, day] = date.split("-").map(Number)
  return { year, month: month - 1, day }
}

export function yearOf(date: string): number {
  return Number(date.slice(0, 4))
}

export function quarterOf(date: string): number {
  return Math.floor(parts(date).month / 3) + 1
}

/** The period of the given kind that contains `date`. */
export function periodContaining(granularity: Granularity, date: string): Period {
  const { year, month } = parts(date)
  switch (granularity) {
    case "year":
      return { granularity, start: iso(utc(year, 0, 1)), end: iso(utc(year, 11, 31)) }
    case "quarter": {
      const first = Math.floor(month / 3) * 3
      return { granularity, start: iso(utc(year, first, 1)), end: iso(utc(year, first + 3, 0)) }
    }
    case "month":
      return { granularity, start: iso(utc(year, month, 1)), end: iso(utc(year, month + 1, 0)) }
    case "custom":
      return { granularity, start: date, end: date }
  }
}

/** The equal period before or after this one. A custom range steps by its own length in days. */
export function shiftPeriod(period: Period, by: number): Period {
  const { year, month, day } = parts(period.start)
  switch (period.granularity) {
    case "year":
      return periodContaining("year", iso(utc(year + by, month, 1)))
    case "quarter":
      return periodContaining("quarter", iso(utc(year, month + by * 3, 1)))
    case "month":
      return periodContaining("month", iso(utc(year, month + by, 1)))
    case "custom": {
      const span = days(period)
      const start = utc(year, month, day + by * span)
      return { granularity: "custom", start: iso(start), end: iso(utc(...offsetParts(start, span - 1))) }
    }
  }
}

function offsetParts(from: Date, by: number): [number, number, number] {
  return [from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate() + by]
}

function days(period: Period): number {
  const a = Date.parse(`${period.start}T00:00:00Z`)
  const b = Date.parse(`${period.end}T00:00:00Z`)
  return Math.round((b - a) / 86_400_000) + 1
}

/** The same period one calendar year earlier, for the prior-year comparison. */
export function samePeriodLastYear(period: Period): Period {
  const a = parts(period.start)
  const b = parts(period.end)
  if (period.toDate) return sameSpanLastYear(period)
  if (period.granularity === "custom") {
    return { granularity: "custom", start: iso(utc(a.year - 1, a.month, a.day)), end: iso(utc(b.year - 1, b.month, b.day)) }
  }
  return periodContaining(period.granularity, iso(utc(a.year - 1, a.month, a.day)))
}

/** The same dates one year back, as a custom range (it's a span, not a named period). */
export function sameSpanLastYear(period: Period): Period {
  const back = (date: string) => {
    const { y, m, d } = { y: Number(date.slice(0, 4)), m: Number(date.slice(5, 7)), d: Number(date.slice(8)) }
    // 29 February becomes 28 February in a non-leap year
    const last = new Date(Date.UTC(y - 1, m, 0)).getUTCDate()
    return `${y - 1}-${String(m).padStart(2, "0")}-${String(Math.min(d, last)).padStart(2, "0")}`
  }
  return { granularity: "custom", start: back(period.start), end: back(period.end) }
}

/** The day before `date`; a comparative balance sheet is drawn at the day before the period starts. */
export function dayBefore(date: string): string {
  const { year, month, day } = parts(date)
  return iso(utc(year, month, day - 1))
}

/** The picker label: `2026`, `Q2 2026`, `June 2026`, or the range itself. */
export function periodName(period: Period): string {
  const { year, month } = parts(period.start)
  switch (period.granularity) {
    case "year":
      return String(year)
    case "quarter":
      return `Q${Math.floor(month / 3) + 1} ${year}`
    case "month":
      return `${MONTHS[month]} ${year}`
    case "custom":
      return rangeWords(period.start, period.end)
  }
}

/** A date range in words, giving the year once when both ends share it: `1 April – 30 June 2026`. */
export function rangeWords(start: string, end: string): string {
  const a = parts(start)
  const b = parts(end)
  const left = a.year === b.year ? `${a.day} ${MONTHS[a.month]}` : `${a.day} ${MONTHS[a.month]} ${a.year}`
  return `${left} – ${b.day} ${MONTHS[b.month]} ${b.year}`
}

/** A single date in words: `30 June 2026`. */
export function dateWords(date: string): string {
  const { year, month, day } = parts(date)
  return `${day} ${MONTHS[month]} ${year}`
}

/** Short form for column heads: `30 Jun 2026`. */
export function shortDateWords(date: string): string {
  const { year, month, day } = parts(date)
  return `${day} ${SHORT_MONTHS[month]} ${year}`
}

/** The period as URL search params, so a report can be linked to and survives a reload. */
export function periodParams(period: Period): Record<string, string> {
  return { period: period.granularity, from: period.start, to: period.end }
}

/** Read a period back from the URL, falling back to the calendar year `fallbackYear`. */
export function periodFromParams(params: URLSearchParams, fallbackYear: number): Period {
  const granularity = params.get("period")
  const from = params.get("from")
  const to = params.get("to")
  const valid = (d: string | null): d is string => Boolean(d && /^\d{4}-\d{2}-\d{2}$/.test(d))
  if (granularity === "custom" && valid(from) && valid(to) && from <= to) {
    return { granularity, start: from, end: to }
  }
  if ((granularity === "year" || granularity === "quarter" || granularity === "month") && valid(from)) {
    return periodContaining(granularity, from)
  }
  return periodContaining("year", `${fallbackYear}-01-01`)
}
