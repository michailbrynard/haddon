import { cn } from "@/lib/utils"
import { amount } from "@/lib/format"

/** An amount in a column of figures.
 *
 * Tabular numerals and right alignment, so a column lines up on the decimal point without a monospace font.
 * Negatives aren't coloured: every payment out on a bank statement is negative, so colouring them would tint
 * half the table for no reason.  Colour is kept for the difference between bank and books.
 */
export function Amount({
  micros,
  precision = 2,
  commodity,
  className,
  emphasis = false,
}: {
  micros: string | null | undefined
  precision?: number
  commodity?: string
  className?: string
  emphasis?: boolean
}) {
  if (micros === null || micros === undefined || micros === "") {
    return <span className={cn("tnum text-muted-foreground", className)}>—</span>
  }
  return (
    <span className={cn("tnum whitespace-nowrap", emphasis && "font-medium", className)}>
      {amount(micros, precision)}
      {commodity ? <span className="ml-1 text-muted-foreground text-[0.9em]">{commodity}</span> : null}
    </span>
  )
}
