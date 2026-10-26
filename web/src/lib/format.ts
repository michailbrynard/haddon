/** Formatting helpers: service values to display text.  No logic here decides anything.
 *
 * Amounts never pass through a JavaScript number.  They arrive as integer strings of millionths (micros) and
 * are formatted by string manipulation so nothing is rounded on the way to the screen.  When an amount can't
 * be written exactly at its commodity's precision, the raw micro string is shown instead, the same rule the
 * service's own formatter follows.
 */

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
]

const SHORT_MONTHS = MONTHS.map((m) => m.slice(0, 3))

/** `"2026-04"` → `"April 2026"`. */
export function monthTitle(month: string): string {
  const [year, mm] = month.split("-")
  return `${MONTHS[Number(mm) - 1]} ${year}`
}

/** `"2026-04-28"` → `"28 Apr"`; with `withYear`, `"28 Apr 2026"`. */
export function shortDate(date: string, withYear = false): string {
  const [year, mm, dd] = date.split("-")
  const base = `${Number(dd)} ${SHORT_MONTHS[Number(mm) - 1]}`
  return withYear ? `${base} ${year}` : base
}

/** `"2026-04-28"` → `"28 April 2026"`. */
export function longDate(date: string): string {
  const [year, mm, dd] = date.split("-")
  return `${Number(dd)} ${MONTHS[Number(mm) - 1]} ${year}`
}

export function monthBounds(month: string): { start: string; end: string } {
  const [year, mm] = month.split("-").map(Number)
  const last = new Date(Date.UTC(year, mm, 0)).getUTCDate()
  return { start: `${month}-01`, end: `${month}-${String(last).padStart(2, "0")}` }
}

export function shiftMonth(month: string, by: number): string {
  const [year, mm] = month.split("-").map(Number)
  const d = new Date(Date.UTC(year, mm - 1 + by, 1))
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`
}

function group(whole: string): string {
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")
}

/** The digits of an integer micro string at `precision`, or `null` when it cannot be written exactly. */
function atPrecision(micros: string, precision: number): { negative: boolean; text: string } | null {
  if (!/^-?\d+$/.test(micros)) return null
  const negative = micros.startsWith("-")
  const digits = (negative ? micros.slice(1) : micros).padStart(7, "0")
  const whole = digits.slice(0, -6)
  const fraction = digits.slice(-6)
  if (fraction.slice(precision).replace(/0/g, "") !== "") return null
  const kept = fraction.slice(0, precision)
  return { negative, text: kept ? `${group(whole)}.${kept}` : group(whole) }
}

/** `"-2642650000"` at precision 2 → `"-2,642.65"`. */
export function amount(micros: string | null | undefined, precision = 2): string {
  if (micros === null || micros === undefined || micros === "") return ""
  const parts = atPrecision(micros, precision)
  if (parts === null) return micros
  return `${parts.negative ? "-" : ""}${parts.text}`
}

/** The same, with the commodity after it: `"-2,642.65 EUR"`. */
export function money(micros: string | null | undefined, commodity: string, precision = 2): string {
  if (micros === null || micros === undefined || micros === "") return "—"
  return `${amount(micros, precision)} ${commodity}`
}

/** The magnitude without its sign, for prose that supplies the direction in words. */
export function magnitude(micros: string, precision = 2): string {
  return amount(micros.startsWith("-") ? micros.slice(1) : micros, precision)
}

export function addMicros(values: string[]): string {
  return values.reduce((total, value) => (BigInt(total) + BigInt(value)).toString(), "0")
}

export function negate(micros: string): string {
  return (-BigInt(micros)).toString()
}

/**
 * An integer micro string as the exact decimal a write sends: `"-1450000000"` at precision 2 → `"-1450.00"`.
 *
 * Since issue 0006 the command surface takes decimal strings in whole commodity units and answers in micros, so
 * this is the conversion used on the way out.  Don't use `amount()` for this: it groups thousands
 * (`"-1,450.00"`), which the service won't accept.  Trailing zeros are kept to the commodity's precision,
 * because the service checks the value against it.
 */
export function decimalForWire(micros: string, precision: number): string {
  if (!/^-?\d+$/.test(micros)) throw new Error(`not an integer micro string: ${micros}`)
  const negative = micros.startsWith("-")
  const digits = (negative ? micros.slice(1) : micros).padStart(7, "0")
  const whole = digits.slice(0, -6)
  const fraction = digits.slice(-6)
  if (fraction.slice(precision).replace(/0/g, "") !== "") {
    throw new Error(`${micros} has more precision than ${precision} decimal places can hold`)
  }
  const kept = fraction.slice(0, precision)
  return `${negative ? "-" : ""}${whole}${kept ? `.${kept}` : ""}`
}

/**
 * The reverse of `decimalForWire`: `"-1450.00"` → `"-1450000000"`, or `null` when the text isn't a decimal yet.
 *
 * Used by forms that total amounts while they are still being typed, e.g. statement lines, which must sum to
 * closing minus opening before the service accepts them.  Returning `null` instead of throwing means a
 * half-typed `"-14."` just doesn't count yet.
 *
 * No floats here either: `parseFloat("0.07") * 1e6` is `70000.00000000001`.  More than six decimal places are
 * refused, even trailing zeros, rather than silently dropped.
 */
export function microsFromDecimal(value: string): string | null {
  const match = /^\s*(-?)(\d+)(?:\.(\d{1,6}))?\s*$/.exec(value)
  if (match === null) return null
  const [, sign, whole, fraction = ""] = match
  return BigInt(`${sign}${whole}${fraction.padEnd(6, "0")}`).toString()
}

export function isZero(micros: string | null | undefined): boolean {
  return micros !== null && micros !== undefined && micros !== "" && BigInt(micros) === 0n
}

/** An account path with spaced separators: `"assets:cash:first-bank"` → `"assets · cash · first-bank"`.
 *
 * Only the separator changes.  An earlier version sentence-cased each segment and turned `irs-withholding` into
 * "Irs withholding".  Account names belong to whoever keeps the books, and someone looking for
 * `liabilities:taxes:irs-withholding` should be able to find it as written.
 */
export function prettyPath(path: string): string {
  return path.split(":").join(" · ")
}

/** The same path split at its first segment, for a list already grouped by that segment. */
export function splitPath(path: string): { head: string; rest: string } {
  const [head, ...rest] = path.split(":")
  return { head, rest: rest.join(" · ") }
}

export function pluralise(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`
}

/** `"2026-09-12T08:03:57Z"` → `"12 Sep 2026, 08:03"`. */
export function timestamp(value: string): string {
  const [date, rest] = value.split("T")
  if (!rest) return value
  return `${shortDate(date, true)}, ${rest.slice(0, 5)}`
}
