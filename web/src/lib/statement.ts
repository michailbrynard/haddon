/** The shape `Statement.tsx` renders, and the adapter from the served report.
 *
 * Sections, order, signs, subtotals, the result row and whether the balance sheet balances are all decided by
 * the service and served by `/reports/statements` (issue 0008 section B).  This file only renames fields.
 * Keep accounting rules out of it so there is one place that decides what a statement says.
 */

import type { ShapedSection } from "./types"

export interface StatementLine {
  key: string
  accountId: string | null
  label: string
  micros: string
  comparative: string | null
}

export interface StatementGroup {
  key: string
  heading: string | null
  lines: StatementLine[]
  subtotal: string | null
  comparativeSubtotal: string | null
}

export interface StatementSection {
  key: string
  title: string
  totalLabel: string
  groups: StatementGroup[]
  total: string
  comparativeTotal: string | null
}

/** Rename a served section's fields for the component. */
export function toSection(section: ShapedSection): StatementSection {
  return {
    key: section.key,
    title: section.title,
    totalLabel: section.total_label,
    total: section.total_micros,
    comparativeTotal: section.comparative_total_micros,
    groups: section.groups.map((group) => ({
      key: group.key,
      heading: group.heading,
      subtotal: group.subtotal_micros,
      comparativeSubtotal: group.comparative_subtotal_micros,
      lines: group.lines.map((line) => ({
        key: line.key,
        accountId: line.account_id,
        label: line.label,
        micros: line.amount_micros,
        comparative: line.comparative_micros,
      })),
    })),
  }
}
