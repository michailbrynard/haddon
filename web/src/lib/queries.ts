/** Query hooks for every read the UI does, all keyed under `["entity", <id>]`.
 *
 * Writes invalidate `["entity", <id>]` wholesale rather than patching caches.  A booking touches the line, the
 * entries behind it, the trial balance, the account register and the reconciliation; keeping those in step by
 * hand would eventually show a wrong balance.  Refetching is cheap.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api } from "./api"
import { monthBounds } from "./format"

export const keys = {
  entities: ["entities"] as const,
  entity: (id: string) => ["entity", id] as const,
  capabilities: (id: string) => ["entity", id, "capabilities"] as const,
  accounts: (id: string) => ["entity", id, "accounts"] as const,
  lines: (id: string, from: string, to: string) => ["entity", id, "lines", from, to] as const,
  line: (id: string, lineId: string) => ["entity", id, "line", lineId] as const,
  statements: (id: string) => ["entity", id, "statements"] as const,
  statement: (id: string, statementId: string) => ["entity", id, "statement", statementId] as const,
  statementLines: (id: string, statementId: string) =>
    ["entity", id, "statement", statementId, "lines"] as const,
  reconciliation: (id: string, asOf: string, from: string) =>
    ["entity", id, "reconciliation", asOf, from] as const,
  trialBalance: (id: string, asOf: string) => ["entity", id, "trial-balance", asOf] as const,
  statementsDocument: (id: string, from: string, to: string) =>
    ["entity", id, "statements-document", from, to] as const,
  register: (id: string, accountId: string, from: string, to: string) =>
    ["entity", id, "register", accountId, from, to] as const,
  provenance: (id: string, lineId: string) => ["entity", id, "provenance", lineId] as const,
  provisional: (id: string) => ["entity", id, "provisional"] as const,
  unrecoveredTax: (id: string, from: string, to: string) =>
    ["entity", id, "unrecovered-tax", from, to] as const,
  closeChecks: (id: string, through: string) => ["entity", id, "close-checks", through] as const,
  closeRecord: (id: string, closeId: string) => ["entity", id, "close", closeId] as const,
  entry: (id: string, entryId: string) => ["entity", id, "entry", entryId] as const,
  entryEvents: (id: string, entryId: string) => ["entity", id, "entry", entryId, "events"] as const,
  entryProvenance: (id: string, entryId: string) => ["entity", id, "entry", entryId, "provenance"] as const,
  document: (id: string, documentId: string) => ["entity", id, "document", documentId] as const,
  openLines: (id: string) => ["entity", id, "lines", "open"] as const,
  recentEvents: (id: string, count: number) => ["entity", id, "events", "recent", count] as const,
}

export function useEntities() {
  return useQuery({ queryKey: keys.entities, queryFn: () => api.entities().then((r) => r.items) })
}

export function useEntity(entity: string) {
  const all = useEntities()
  return { ...all, data: all.data?.find((e) => e.entity_id === entity) }
}

export function useCapabilities(entity: string) {
  return useQuery({
    queryKey: keys.capabilities(entity),
    queryFn: () => api.capabilities(entity),
    staleTime: 5 * 60 * 1000,
    enabled: Boolean(entity),
  })
}

/** Whether the reconciliation extension is running on this installation.
 *
 * `undefined` while capabilities load, so callers can tell "not yet known" from "switched off" and fire no
 * request that would 404.  With `HADDON_RECONCILIATION=off` the statement and line routes aren't mounted, and
 * requesting them anyway used to put an error on the month page (issue 0004, exit gate 6).
 *
 * Matched on the end of the path: `/capabilities` publishes reads absolutely
 * (`/v1/entities/<id>/reconciliation`), and an earlier exact match made every installation look as if
 * reconciliation were off.
 */
export function useReconciliationEnabled(entity: string): boolean | undefined {
  const { data } = useCapabilities(entity)
  return data ? data.reads.some((read) => read.endsWith("/reconciliation")) : undefined
}

/** Each commodity's precision, so no page has to assume two decimals. */
export function usePrecision(entity: string): (commodity: string) => number {
  const { data } = useCapabilities(entity)
  return (commodity: string) => data?.commodities.find((c) => c.code === commodity)?.precision ?? 2
}

export function useAccounts(entity: string) {
  return useQuery({
    queryKey: keys.accounts(entity),
    queryFn: () => api.accounts(entity),
    staleTime: 5 * 60 * 1000,
    enabled: Boolean(entity),
  })
}

export function useMonthLines(entity: string, month: string) {
  const { start, end } = monthBounds(month)
  const reconciles = useReconciliationEnabled(entity)
  return useQuery({
    queryKey: keys.lines(entity, start, end),
    queryFn: () => api.lines(entity, { from: start, to: end }),
    enabled: Boolean(entity && month) && reconciles === true,
  })
}

export function useLine(entity: string, lineId: string) {
  return useQuery({
    queryKey: keys.line(entity, lineId),
    queryFn: () => api.line(entity, lineId).then((r) => r.line),
    enabled: Boolean(entity && lineId),
  })
}

export function useStatements(entity: string) {
  const reconciles = useReconciliationEnabled(entity)
  return useQuery({
    queryKey: keys.statements(entity),
    queryFn: () => api.statements(entity).then((r) => r.items),
    enabled: Boolean(entity) && reconciles === true,
  })
}

/** One statement, by id.
 *
 * Fetched separately rather than picked out of `useStatements` because replacement chains are walked by id,
 * and a predecessor two links back may not be in the cached listing.
 */
export function useStatement(entity: string, statementId: string | null | undefined) {
  const reconciles = useReconciliationEnabled(entity)
  return useQuery({
    queryKey: keys.statement(entity, statementId ?? ""),
    queryFn: () => api.statement(entity, statementId as string).then((r) => r.statement),
    enabled: Boolean(entity && statementId) && reconciles === true,
    retry: false,
  })
}

/** The lines of one statement, with each line's status.
 *
 * `GET /statements/{id}/lines` returns the lines in printed order with nothing about bookings.  This uses
 * `GET /lines?statement_id=` instead, which adds each line's derived status, explanation and entries.  The
 * replacement screen needs that: a statement with any line still booked can't be replaced, and the screen
 * should know before it tries.
 */
export function useStatementLines(entity: string, statementId: string | null | undefined) {
  const reconciles = useReconciliationEnabled(entity)
  return useQuery({
    queryKey: keys.statementLines(entity, statementId ?? ""),
    queryFn: () => api.lines(entity, { statement_id: statementId as string }),
    enabled: Boolean(entity && statementId) && reconciles === true,
  })
}

export function useReconciliation(entity: string, month: string) {
  const { start, end } = monthBounds(month)
  const reconciles = useReconciliationEnabled(entity)
  return useQuery({
    queryKey: keys.reconciliation(entity, end, start),
    queryFn: () => api.reconciliation(entity, end, start),
    enabled: Boolean(entity && month) && reconciles === true,
  })
}

export function useTrialBalance(entity: string, asOf: string) {
  return useQuery({
    queryKey: keys.trialBalance(entity, asOf),
    queryFn: () => api.trialBalance(entity, asOf),
    enabled: Boolean(entity && asOf),
  })
}

export function useStatementsDocument(entity: string, from: string | null, to: string | null) {
  return useQuery({
    queryKey: keys.statementsDocument(entity, from ?? "", to ?? ""),
    queryFn: () => api.statementsDocument(entity, from!, to!),
    enabled: Boolean(entity && from && to),
    retry: false,
  })
}

export function useRegister(entity: string, accountId: string, from: string, to: string) {
  return useQuery({
    queryKey: keys.register(entity, accountId, from, to),
    queryFn: () => api.register(entity, accountId, { from, to }),
    enabled: Boolean(entity && accountId && from && to),
  })
}

/** Recoverable tax not recovered over a period, with totals. */
export function useUnrecoveredTax(entity: string, from: string | null, to: string | null) {
  return useQuery({
    queryKey: keys.unrecoveredTax(entity, from ?? "", to ?? ""),
    queryFn: () => api.unrecoveredTax(entity, { from: from ?? undefined, to: to ?? undefined }),
    enabled: Boolean(entity && from && to),
  })
}

/** Every booking still awaiting a document, across all months.
 *
 * Not filtered by month on the server because the month review also counts the ones older than the month
 * being viewed.
 */
export function useProvisional(entity: string) {
  return useQuery({
    queryKey: keys.provisional(entity),
    queryFn: () => api.provisional(entity).then((r) => r.items),
    enabled: Boolean(entity),
  })
}

export function useProvenance(entity: string, lineId: string) {
  return useQuery({
    queryKey: keys.provenance(entity, lineId),
    queryFn: () => api.provenance(entity, lineId),
    enabled: Boolean(entity && lineId),
  })
}

/** Whether this period would close, and if not, what's outstanding.
 *
 * Keyed on the date: a close covers the window from the current lock to that date, so the same books give a
 * different answer for March than for April.
 */
export function useCloseChecks(entity: string, through: string) {
  return useQuery({
    queryKey: keys.closeChecks(entity, through),
    queryFn: () => api.closeChecks(entity, through),
    enabled: Boolean(entity && through),
  })
}

/** The close record behind a lock, which links to the snapshot the close was taken against.
 *
 * Close records are immutable, so this is cached indefinitely; the id only changes when the lock moves.
 */
export function useCloseRecord(entity: string, closeId: string | null | undefined) {
  return useQuery({
    queryKey: keys.closeRecord(entity, closeId ?? ""),
    queryFn: () => api.closeRecord(entity, closeId as string),
    enabled: Boolean(entity && closeId),
    staleTime: Infinity,
  })
}

/** Whether this installation serves the named command.
 *
 * Read from the published capabilities, not a role: Haddon records authority but doesn't enforce it
 * (`docs/design/decisions.md`, "Authorisation").  The question is whether the service mounted the route, which
 * it doesn't with reconciliation off or for a command not yet built.
 *
 * Since issue 0006 a command's published name is one string: the `operationId` in the OpenAPI document, the key
 * in `/capabilities.commands`, and the handler in `haddon.commands`.  Matching the name is exact, whereas
 * `endsWith("/close")` would also match a hypothetical `/entries/{id}/close`.
 *
 * `undefined` while capabilities load, so callers don't render a button that's about to disappear.
 */
export function useCommand(entity: string, name: string): boolean | undefined {
  const { data } = useCapabilities(entity)
  return data ? data.commands.some((c) => c.name === name) : undefined
}

/** Whether this installation serves the named read.  Reads have no command name, so they are matched by path
 * suffix; `/capabilities` publishes them as absolute paths including the entity id. */
export function useRead(entity: string, pathSuffix: string): boolean | undefined {
  const { data } = useCapabilities(entity)
  return data ? data.reads.some((r) => r.endsWith(pathSuffix)) : undefined
}

export function useCanClose(entity: string): boolean | undefined {
  return useCommand(entity, "close_period")
}

/** Whether this installation serves the export command.
 *
 * It can't tell whether `export_root` is configured.  An installation without it refuses the run with its own
 * message, which the page shows, rather than the UI guessing and hiding the button. */
export function useCanExport(entity: string): boolean | undefined {
  return useCommand(entity, "run_export")
}

/** One entry: its legs, current status, and every correction it is part of. */
export function useEntry(entity: string, entryId: string) {
  return useQuery({
    queryKey: keys.entry(entity, entryId),
    queryFn: () => api.entry(entity, entryId),
    enabled: Boolean(entity && entryId),
  })
}

/** The audit trail of one entry, corrections included.
 *
 * `retry: false`: the expected failure is a 404 for an entry that isn't here, and retrying only delays saying so. */
export function useEntryEvents(entity: string, entryId: string) {
  return useQuery({
    queryKey: keys.entryEvents(entity, entryId),
    queryFn: () => api.entryEvents(entity, entryId),
    enabled: Boolean(entity && entryId),
    retry: false,
  })
}

/** The documents behind an entry.  Only enabled when the provenance read is served: it belongs to the
 * reconciliation extension, and without it every entry page would 404. */
export function useEntryProvenance(entity: string, entryId: string) {
  const served = useRead(entity, "/provenance")
  return useQuery({
    queryKey: keys.entryProvenance(entity, entryId),
    queryFn: () => api.entryProvenance(entity, entryId),
    enabled: Boolean(entity && entryId) && served === true,
    retry: false,
  })
}

/** One registered document and everything that cites it. */
export function useDocument(entity: string, documentId: string | null | undefined) {
  return useQuery({
    queryKey: keys.document(entity, documentId ?? ""),
    queryFn: () => api.document(entity, documentId as string),
    enabled: Boolean(entity && documentId),
    staleTime: Infinity,
  })
}


/* Review inbox reads.
 *
 * These are existing reads without a month bound.  The inbox asks what in the books is unfinished, and most of
 * that isn't tied to a month: a line left unexplained in January is still unexplained in March.
 *
 * All the keys sit under `["entity", id]`, so `useEntityMutation` refetches every one on any write.  The inbox
 * reads five endpoints, and without that its counts and lists would drift apart one write at a time.
 */

/** Whether an entity-scoped read is served, matched including the entity id.
 *
 * Some paths are also the tails of other routes: `/entries/{entry_id}/events` ends with `/events` and
 * `/statements/{statement_id}/lines` ends with `/lines`.  A bare `useRead(entity, "/events")` would say yes
 * for an installation serving only the per-entry log.  Prefixing the entity id pins it to the entity-scoped
 * route.
 */
export function useEntityRead(entity: string, path: string): boolean | undefined {
  return useRead(entity, `/${entity}${path}`)
}

/** Every statement line not yet accounted for, across all statements and months.
 *
 * No date bound.  `api.lines` follows its cursor to the end, so the count covers the whole book rather than
 * one page.
 */
export function useOpenLines(entity: string) {
  const served = useEntityRead(entity, "/lines")
  return useQuery({
    queryKey: keys.openLines(entity),
    queryFn: () => api.lines(entity, { state: ["unexplained", "partly_explained"] }),
    enabled: Boolean(entity) && served === true,
  })
}

/** Recoverable tax not recovered, over the whole journal.
 *
 * `useUnrecoveredTax` above is the per-period version the reports use.  This one is unbounded and cached
 * separately so a report's figure can't depend on whether the inbox was opened.
 */
export function useOpenUnrecoveredTax(entity: string) {
  const served = useEntityRead(entity, "/unrecovered-tax")
  return useQuery({
    queryKey: keys.unrecoveredTax(entity, "", ""),
    queryFn: () => api.unrecoveredTax(entity),
    enabled: Boolean(entity) && served === true,
  })
}

/** The most recent events in the books, newest first.
 *
 * Lets a reviewer notice changes such as a correction somebody else posted, which no other screen shows.
 */
export function useRecentEvents(entity: string, count = 12) {
  const served = useEntityRead(entity, "/events")
  return useQuery({
    queryKey: keys.recentEvents(entity, count),
    queryFn: () => api.recentEvents(entity, count),
    enabled: Boolean(entity) && served === true,
  })
}

/** Any write against one entity.  Everything read about that entity is refetched once it succeeds.
 *
 * The entity listing is invalidated too.  It holds `lock_through`, `lock_version` and `lock_close_id` and is
 * cached under `["entities"]`, so without this a close would leave the page holding the old lock version and
 * its next write would be refused as stale.
 */
export function useEntityMutation<TArgs, TResult>(
  entity: string,
  run: (args: TArgs) => Promise<TResult>,
) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: run,
    onSuccess: () => {
      client.invalidateQueries({ queryKey: keys.entity(entity) })
      client.invalidateQueries({ queryKey: keys.entities })
    },
  })
}
