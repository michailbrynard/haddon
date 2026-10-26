/** The response shapes from `/v1`, limited to the fields the UI reads.
 *
 * Hand-kept mirrors of the schemas in `src/haddon/api/schemas.py`, not generated: the UI needs a small, stable
 * subset, and a generated client would pull in every write body and report.  Every field here exists in the
 * OpenAPI document (`tests/test_ui_types_are_the_contract.py` checks some of them against live payloads).
 */

export type Micros = string

export interface EntityScoped {
  entity_id: string
  legal_name: string
  base_currency: string
  snapshot_seq: number
}

export interface EntitySummary {
  entity_id: string
  legal_name: string
  legal_form: string
  country: string
  base_currency: string
  commodities: string[]
  lock_through: string | null
  /** Sent back as `expected_version` on close or reopen, so a lock moved by somebody else is refused. */
  lock_version: number
  /** The close record that set the current lock, which links to the snapshot behind it. */
  lock_close_id: string | null
}

export interface Commodity {
  code: string
  precision: number
  quantum: number
}

export interface Command {
  /** The command's published name, the same string as the OpenAPI `operationId` and the server handler. */
  name: string
  method: string
  path: string
}

export interface Capabilities extends EntityScoped {
  actor_recorded_as: string
  commodities: Commodity[]
  /** What this installation serves.  A switched-off extension is absent from both lists. */
  reads: string[]
  writes: string[]
  /** The same writes by name (issue 0006).  UI actions are gated on these; see `useCommand`. */
  commands: Command[]
}

export interface Account {
  id: string
  path: string
  type: string
  code: string | null
  tax_code: string | null
  is_cash: boolean
  archived: boolean
  commodities: string[]
}

export interface AccountPage extends EntityScoped {
  items: Account[]
  next_cursor: string | null
}

export interface Posting {
  id: string
  seq: number
  account_id: string
  account_path: string
  commodity: string
  amount_micros: Micros
  memo: string | null
  tax_code: string | null
}

export interface Entry {
  id: string
  dt: string
  narration: string
  status: "draft" | "posted" | "void"
  origin: string
  created_at: string
  created_by: string
  postings: Posting[]
  reversed_by?: string | null
}

/** An entry's current status, derived by the service from the corrections that reference it (issue 0007).
 *
 * `corrected` and `reversed` differ: both leave the original in the books with an exact mirror beside it, but
 * only `corrected` also posts a replacement. */
export type EntryState = "posted" | "corrected" | "reversed" | "draft" | "void"

/** One link in an entry's correction chain, and this entry's place in it.
 *
 * `role` saves a page re-deriving the relationship from three ids: `original` means this entry was corrected,
 * `mirror` means it is the reversal, `replacement` means it was posted instead. */
export interface CorrectionLink {
  id: string
  role: "original" | "mirror" | "replacement"
  original_id: string
  mirror_id: string
  replacement_id: string | null
  reason: string
  created_at: string
  created_by: string
  links: Record<string, string>
}

/** `GET /entries/{id}`: the entry, its current status, and every correction it is part of. */
export interface EntryDetail extends EntityScoped {
  entry: Entry
  version: number
  reversed_by: string | null
  corrections: CorrectionLink[]
  state: EntryState
}

/** One row of the append-only log: a business action, its actor, its time and its reason.
 *
 * `before`/`after` are typed as unknown on purpose.  They're the stored record, and a page that branched on
 * their shape would be depending on the storage format. */
export interface AuditEvent {
  seq: number
  kind: string
  subject_kind: string
  subject_id: string
  before: unknown
  after: unknown
  reason: string | null
  request_id: string | null
  created_at: string
  created_by: string
}

export interface EntryEvents extends EntityScoped {
  entry_id: string
  items: AuditEvent[]
  since: number
  next_since: number
  high_water: number
}

/** A page of the entity's log, read forward from a sequence.  `high_water` is the latest sequence number. */
export interface EventPage extends EntityScoped {
  items: AuditEvent[]
  since: number
  next_since: number
  high_water: number
}

/** A posted booking still waiting on a document that may change it. */
export interface Expectation {
  id: string
  entry_id: string
  state: "awaiting" | "met" | "withdrawn"
  awaiting: string
  reason: string
  evidence_id: string | null
  supersedes_id: string | null
  created_at: string
  created_by: string
}

export interface ProvisionalItem {
  expectation: Expectation
  entry: Entry
  links: Record<string, string>
}

export interface ProvisionalPage extends EntityScoped {
  items: ProvisionalItem[]
  next_cursor: string | null
}

/** Recoverable tax that was not recovered on an entry, and why. */
export interface UnrecoveredRecord {
  id: string
  entry_id: string
  posting_id: string | null
  commodity: string
  amount_micros: Micros
  state: "unrecovered" | "recovered" | "forgone"
  tax_code: string | null
  reason: string
  recoverable_until: string | null
  recovered_entry_id: string | null
  supersedes_id: string | null
  created_at: string
  created_by: string
}

export interface UnrecoveredItem {
  record: UnrecoveredRecord
  entry: Entry
  links: Record<string, string>
}

export interface UnrecoveredPage extends EntityScoped {
  items: UnrecoveredItem[]
  next_cursor: string | null
  state: string
  totals: Record<string, Micros>
}

/** Derived from the line's current (unsuperseded) explanation, so each line has exactly one (issue 0004). */
export type LineStatus =
  | "unexplained"
  | "partly_explained"
  | "booked"
  | "linked"
  | "ignored"
  | "superseded_input"

export interface LineAllocation {
  allocation_id: string
  posting_id: string
  entry_id: string
  amount_micros: Micros
}

/** One explanation of a line.  The chain of explanations is the line's history. */
export interface LineExplanation {
  explanation_id: string
  line_id: string
  kind: "booked" | "linked" | "ignored"
  reason: string
  entry_id: string | null
  correction_id: string | null
  supersedes_id: string | null
  allocations: LineAllocation[]
  allocated_micros: Micros
  created_at: string
  created_by: string
}

export interface Line {
  line_id: string
  statement_id: string
  statement_live: boolean
  line_no: number
  source_key: string
  dt: string
  amount_micros: Micros
  commodity: string
  account_id: string
  description: string
  reference: string | null
  raw: string
  status: LineStatus
  allocated_micros: Micros
  unexplained_micros: Micros
  explanation: LineExplanation | null
  history: LineExplanation[]
  entries: Entry[]
}

export interface LinePage extends EntityScoped {
  items: Line[]
  next_cursor: string | null
  /** Filters applied after selecting the page: `["state"]`, or `[]` when the query did all the filtering.
   *  While non-empty, a page can hold fewer items than the limit, or none, with `next_cursor` still set.
   *  That's why `api.lines` follows the cursor. */
  filtered_after_page: string[]
}

/** The original file behind a booking.
 *
 * `blob_path` was removed in issue 0007: a path on the service's disk isn't something a reviewer can open or a
 * page should publish.  `storage` says whether the bytes can be fetched from here; `external` means only
 * `external_ref` exists and the document is held elsewhere.
 */
export interface StatementDocument {
  sha256: string
  filename: string
  media_type: string
  size_bytes: number
  storage: "stored" | "external"
  external_ref: string | null
}

/** A registered document, from `/documents/{id}`.
 *
 * `links.content` is present only when the bytes are held here.  The UI checks for the link rather than
 * building the URL itself. */
export interface DocumentRecord extends StatementDocument {
  id: string
  filing_count: number
  created_at: string
  created_by: string
  links: { self: string; documents: string; content?: string }
}

export interface DocumentFiling {
  evidence_id: string
  subject_kind: string
  subject_id: string | null
  page: number | null
  locator: string
  caption: string
  created_at: string
  created_by: string
}

export interface DocumentResponse extends EntityScoped {
  document: DocumentRecord
  created: boolean
  filings: DocumentFiling[]
}

/** One loaded statement: its subject, period, balances, document and replacement chain.
 *
 * The identifier is `statement_id`, not `id`.  It was once declared here as `id` and nothing failed, because
 * the only reader was a React `key`; `tsc` can't catch a field the server doesn't send.
 *
 * Use the service's `live` rather than re-deriving it from `replaced_by_id`.  The definition lives in one place
 * (`tables.live_statement_clause`).
 */
export interface Statement {
  statement_id: string
  account_id: string
  account_path: string
  commodity: string
  period_start: string
  period_end: string
  opening_micros: Micros
  closing_micros: Micros
  opening_verified: boolean
  /** Which opening case applies: verified against a prior closing, first statement, or a gap. */
  opening_basis: string
  prior_statement_id: string | null
  line_count: number
  /** The sum of the lines.  The statement balances when this equals closing minus opening. */
  lines_micros: Micros
  document_id: string
  document: StatementDocument
  page: number | null
  locator: string
  reason: string
  /** The statement this one replaces, when it is a replacement. */
  replaces_id: string | null
  /** The statement that replaced this one, if any.  A UNIQUE index guarantees at most one. */
  replaced_by_id: string | null
  live: boolean
  created_at: string
  created_by: string
  links: {
    self: string
    lines: string
    account: string
    document: string
    close_checks: string
    replaces?: string
    replaced_by?: string
  }
}

export interface StatementPage extends EntityScoped {
  items: Statement[]
  next_cursor: string | null
}

export interface StatementResponse extends EntityScoped {
  statement: Statement
}

/** The response to `load_statement` and `replace_statement`: the statement as it now stands, and the
 *  sequence number of the audit row the write recorded. */
export interface StatementWriteResponse extends EntityScoped {
  statement: Statement
  audit_seq: number
}

export interface ReconciliationAccount {
  account_id: string
  account_path: string
  commodity: string
  as_of: string
  statement_micros: Micros | null
  ledger_micros: Micros | null
  asserted_micros: Micros | null
  difference_micros: Micros | null
  agrees: boolean
  statement_ids: string[]
  uncovered: { start: string; end: string }[]
}

export interface Reconciliation extends EntityScoped {
  as_of: string
  start: string
  agrees: boolean
  accounts: ReconciliationAccount[]
}

export interface ReportLine {
  account_id: string
  account_path: string
  type: string
  commodity: string
  amount_micros: Micros
}

export interface TrialBalance extends EntityScoped {
  through: string
  lines: ReportLine[]
  totals: Record<string, Micros>
}

/* Shaped statements (issue 0008 section B).
 *
 * Sections, groups, signs, subtotals, the result row and whether the balance sheet balances are decided by the
 * service (`haddon.statements.shape`) and published by `/reports/statements`.  The UI renders them as served
 * without ordering, signing, subtotalling or checking anything, so the screen and the PDF can't disagree.
 */
export interface ShapedLine {
  key: string
  /** the account a line links to; null for a synthetic row such as the result carried inside equity */
  account_id: string | null
  label: string
  amount_micros: Micros
  /** null when no comparative column is shown */
  comparative_micros: Micros | null
}

export interface ShapedGroup {
  key: string
  /** null when the group holds a single account and needs no heading of its own */
  heading: string | null
  subtotal_micros: Micros | null
  comparative_subtotal_micros: Micros | null
  lines: ShapedLine[]
}

export interface ShapedSection {
  key: string
  title: string
  total_label: string
  total_micros: Micros
  comparative_total_micros: Micros | null
  groups: ShapedGroup[]
}

export interface IncomeStatementView {
  commodity: string
  start: string | null
  end: string | null
  comparative_start: string | null
  comparative_end: string | null
  /** the label for this commodity's bottom line, set by the service; each commodity has its own */
  result_label: string
  sections: ShapedSection[]
  /** positive is a profit (the service's net, with the statement's sign) */
  result_micros: Micros
  comparative_result_micros: Micros | null
}

export interface BalanceCheck {
  date: string
  /** assets minus liabilities and equity; zero at every date on a balanced journal */
  difference_micros: Micros
}

export interface BalanceSheetView {
  commodity: string
  as_of: string
  comparative_as_of: string | null
  sections: ShapedSection[]
  total_assets_micros: Micros
  total_funding_micros: Micros
  difference_micros: Micros
  comparative_total_assets_micros: Micros | null
  comparative_total_funding_micros: Micros | null
  comparative_difference_micros: Micros | null
  checks: BalanceCheck[]
}

export interface PeriodLabel {
  start: string
  end: string
  name: string
  heading: string
}

export interface ComparativeLabel {
  start: string
  end: string
  name: string
}

export interface CommodityStatements {
  commodity: string
  income_statement: IncomeStatementView
  balance_sheet: BalanceSheetView
}

export interface StatementsDocument extends EntityScoped {
  commodity: string
  period: PeriodLabel
  comparative: ComparativeLabel | null
  as_of: string
  footnote: string
  statements: CommodityStatements[]
  other_commodities: string[]
}

export interface RegisterLine {
  entry_id: string
  dt: string
  seq: number
  narration: string
  origin: string
  account_id: string
  account_path: string
  commodity: string
  amount_micros: Micros
  running_micros: Micros
  memo: string | null
}

export interface Register extends EntityScoped {
  account_id: string
  account_path: string
  start: string
  end: string
  opening_micros: Record<string, Micros>
  items: RegisterLine[]
  next_cursor: string | null
}

export interface ProvenanceLine {
  source_row_id: string
  row_no: number
  dt: string
  description: string
  amount_micros: Micros
  currency: string
  raw: string
  statement_id: string
  document_id: string
  document: StatementDocument
}

/** A document filed directly against an entry: what it is, where in it, and which document. */
export interface ProvenanceEvidence {
  evidence_id: string
  entry_id: string | null
  statement_id: string | null
  page: number | null
  locator: string
  caption: string
  document_id: string
  document: StatementDocument
}

export interface Provenance extends EntityScoped {
  subject: Record<string, unknown>
  line_count: number
  lines: ProvenanceLine[]
  evidence: ProvenanceEvidence[]
}

// Close workflow (issue 0005)
export interface CloseOutstanding {
  kind: string
  message: string
  detail: Record<string, unknown>
}

export interface CloseWaiver {
  check: string
  through: string
  reason: string
  actor: string | null
  status_at_close: string
  reason_at_close: string
  outstanding_at_close: number
}

/** One close check as evaluated by the service.  `status` is `pass | fail | unevaluable`; only `pass` passes. */
export interface CloseCheck {
  check: string
  title: string
  requirement: string
  status: string
  reason: string
  blocking: boolean
  outstanding: CloseOutstanding[]
  outstanding_truncated: number
  waiver: CloseWaiver | null
}

/**
 * `eligible` and `may_close` are different questions.  `eligible` is whether the books are complete;
 * `may_close` is whether a close would be allowed now, which an override can make true while `eligible` stays
 * false.
 */
export interface CloseChecks extends EntityScoped {
  through: string
  window_start: string
  prior_through: string | null
  months: { month: string; start: string; end: string }[]
  eligible: boolean
  may_close: boolean
  checks: CloseCheck[]
  blocking_checks: string[]
  unmatched_overrides: CloseOverride[]
}

/** An override of one named blocking check, with a reason.  No wildcards, and nothing to create beforehand. */
export interface CloseOverride {
  check: string
  reason: string
}

export interface PeriodLock {
  through: string | null
  version: number
  close_id: string | null
}

export interface CloseSnapshot {
  id: string
  snapshot_seq: number
  state_hash: string
  through: string
}

export interface ClosePeriodResult extends EntityScoped {
  close_id: string
  kind: string
  lock: PeriodLock
  snapshot: CloseSnapshot
  checks: CloseChecks
  audit_seq: number
}

/** One close or reopen.  `links.snapshot` is the immutable state it was recorded against. */
export interface CloseRecord extends EntityScoped {
  id: string
  kind: string
  through: string
  prior_through: string | null
  snapshot_id: string
  reason: string
  overrides: CloseWaiver[]
  created_at: string
  created_by: string
  links: Record<string, string>
}

export interface ReopenPeriodResult extends EntityScoped {
  close_id: string
  kind: string
  reopened_close_id: string | null
  lock: PeriodLock
  snapshot: CloseSnapshot
  audit_seq: number
}

/** One file an export published: its name in the ledger directory, digest, length, and download link.
 *
 * `sha256` covers the bytes that passed `hledger check --strict` under the pinned binary.  The download
 * re-hashes the file against it before serving it.
 */
export interface ExportArtifact {
  path: string
  sha256: string
  bytes: number
  href: string
}

/** What one export wrote: the immutable record, the state it was rendered from, and the files. */
export interface ExportRun extends EntityScoped {
  export_id: string
  snapshot_id: string
  operation_id: string
  state_hash: string
  manifest_sha256: string
  range_start: string
  range_end: string
  ledger_dir: string
  artifacts: ExportArtifact[]
  counts: Record<string, unknown>
  verifier: Record<string, unknown>
  event_seq: number
  links: Record<string, string>
}
