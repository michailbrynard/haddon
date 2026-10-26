/** API client.  The UI uses `/v1`, the same API agents use.
 *
 * The page holds no token.  The reverse proxy authenticates the person and passes on who they are, and the
 * service turns that into an actor string the same way it does for a bearer token.  So the browser sends plain
 * same-origin requests and the audit record names whoever was at the keyboard.
 *
 * Why the `/ui` prefix: it's the same `/v1` underneath (the proxy strips the prefix), but the two entrances need
 * opposite rules.  Agents reach `/v1` with a token, so the proxy must blank the user header there or anyone
 * could forge a caller.  Browsers reach `/ui/v1` with a password, so the proxy must pass the header through.
 * See `deploy/haddon.nginx.conf.example`.
 *
 * Every write needs:
 *
 * - an `Idempotency-Key`, so a retry after a dropped connection replays the original instead of writing twice.
 *   The service refuses a write without one.
 * - a reason.  The service refuses a blank one; the write functions here take it as a required argument so a
 *   caller can't forget it.
 */

import type {
  Account,
  AccountPage,
  Capabilities,
  DocumentResponse,
  EntryDetail,
  EntryEvents,
  EventPage,
  CloseChecks,
  CloseRecord,
  CloseOverride,
  ClosePeriodResult,
  EntitySummary,
  Entry,
  ExportRun,
  Line,
  LinePage,
  Provenance,
  ProvisionalPage,
  Reconciliation,
  Register,
  ReopenPeriodResult,
  Statement,
  StatementPage,
  StatementResponse,
  StatementsDocument,
  StatementWriteResponse,
  TrialBalance,
  UnrecoveredPage,
} from "./types"

const V1 = "/ui/v1"

export interface ErrorDetail {
  path?: string
  reason?: string
  message?: string
  [key: string]: unknown
}

/** A refusal from the service, with its own code and details rather than a flattened message. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: ErrorDetail[]
  readonly requestId?: string

  constructor(
    status: number,
    code: string,
    message: string,
    details: ErrorDetail[] = [],
    requestId?: string,
  ) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
  }

  /** The message, plus the first detail's message when it adds something. */
  get summary(): string {
    const first = this.details[0]
    if (first?.message && first.message !== this.message) return `${this.message} — ${first.message}`
    return this.message
  }
}

async function refuse(response: Response): Promise<never> {
  let body: { error?: { code?: string; message?: string; details?: ErrorDetail[]; request_id?: string } } = {}
  try {
    body = await response.json()
  } catch {
    /* not JSON; fall back to the status */
  }
  const error = body.error
  throw new ApiError(
    response.status,
    error?.code ?? `http_${response.status}`,
    error?.message ?? response.statusText ?? "the request failed",
    error?.details ?? [],
    error?.request_id,
  )
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  })
  if (!response.ok) await refuse(response)
  return (await response.json()) as T
}

function get<T>(path: string): Promise<T> {
  return request<T>(path)
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  })
}

function query(params: Record<string, string | number | string[] | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue
    if (Array.isArray(value)) value.forEach((v) => search.append(key, v))
    else search.append(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ""
}

const base = (entity: string) => `${V1}/entities/${encodeURIComponent(entity)}`

/**
 * A URL a browser can follow for a service link.
 *
 * The service returns links under `/v1`, the agent entrance where the proxy blanks the user header, so a plain
 * anchor to one would arrive unauthenticated.  Rewrite it to `/ui/v1`, like every fetch in this file.
 */
export const uiHref = (serviceLink: string) => (serviceLink.startsWith("/v1/") ? `/ui${serviceLink}` : serviceLink)

/**
 * A leg on the contra side of a booking.  The cash leg comes from the statement line and is never supplied.
 *
 * `account` is a path or a stable id, and `amount` is an exact decimal string in whole commodity units (the
 * command vocabulary from issue 0006).  Responses still carry `amount_micros`, so pages convert with
 * `decimalForWire` on the way out and read micros on the way in.
 */
export interface Leg {
  account: string
  commodity: string
  amount: string
  memo?: string | null
}

/**
 * One normalised statement line in a statement submission.
 *
 * `amount` is an exact decimal string in whole commodity units; read back, the line carries `amount_micros`.
 * `currency` is required and must equal the statement's commodity.  The ledger never infers currency, so a
 * line without one is refused rather than defaulting to the statement's.
 *
 * `raw` is the line as it appeared in the file and `source_key` its stable identity in the document.  Both are
 * optional and derived by the service when absent, but the replacement form sends them so a restatement keeps
 * the original's provenance.
 */
export interface StatementLineSubmission {
  dt: string
  description: string
  amount: string
  currency: string
  reference?: string | null
  raw?: string | null
  source_key?: string | null
}

/**
 * The document a statement was read from, and where the original is kept.
 *
 * The ledger records this but doesn't fetch it.  `sha256` describes the bytes somebody read and is the
 * document's identity: the same file registered twice is one row, and only one live statement is allowed per
 * document.
 *
 * Exactly one of `blob_path` and `external_ref` is required.  A browser can't put bytes on the service's disk,
 * so the UI only ever sends `external_ref`.
 */
export interface DocumentSubmission {
  sha256: string
  filename: string
  media_type: string
  size_bytes: number
  blob_path?: string
  external_ref?: string | null
}

export const api = {
  entities: () => get<{ items: EntitySummary[] }>(`${V1}/entities`),

  capabilities: (entity: string) => get<Capabilities>(`${base(entity)}/capabilities`),

  /** The whole chart, following the cursor to the end.  An account missing from the picker couldn't be
   *  booked to, so one page isn't enough. */
  accounts: async (entity: string): Promise<Account[]> => {
    const out: Account[] = []
    let cursor: string | null = null
    do {
      const page: AccountPage = await get<AccountPage>(`${base(entity)}/accounts${query({ limit: 200, cursor })}`)
      out.push(...page.items)
      cursor = page.next_cursor
    } while (cursor)
    return out
  },

  /** Add an account to the chart.
   *
   * `reason` is optional here, unlike other writes: adding an account is structure, not a booking, and the
   * audit action records that an account was added when no reason is given.  The UI doesn't send one.  Instead
   * adding an account is its own form step, never a side effect of saving a booking.
   */
  createAccount: (
    entity: string,
    body: {
      path: string
      type: string
      commodities: string[]
      is_cash?: boolean
      code?: string | null
      reason?: string
    },
  ) => post<{ account: Account }>(`${base(entity)}/accounts`, body),

  /** Every statement line over a date range, with what each one was booked to.
   *
   *  Must follow the cursor to the end: `state` filters each line's derived status after the page is selected
   *  (`filtered_after_page`), so a page can come back short or empty with `next_cursor` still set.  Stopping at
   *  the first short page would drop lines the reviewer filtered for. */
  lines: async (
    entity: string,
    params: { from?: string; to?: string; state?: string[]; statement_id?: string },
  ): Promise<Line[]> => {
    const out: Line[] = []
    let cursor: string | null = null
    do {
      const page: LinePage = await get<LinePage>(
        `${base(entity)}/lines${query({ ...params, limit: 200, cursor })}`,
      )
      out.push(...page.items)
      cursor = page.next_cursor
    } while (cursor)
    return out
  },

  line: (entity: string, lineId: string) =>
    get<{ line: Line }>(`${base(entity)}/lines/${encodeURIComponent(lineId)}`),

  /** Every loaded statement, replaced ones included, following the cursor to the end.
   *
   * A replacement chain can only be walked if both ends are in the list, and the predecessor is the older one,
   * so it's the one that falls off the first page.  Reading a single page once made a statement look like the
   * only one ever loaded for its account.
   *
   * `live` is not passed on purpose: the page needs superseded statements too, to show what replaced what.
   */
  statements: async (entity: string): Promise<{ items: Statement[] }> => {
    const out: Statement[] = []
    let cursor: string | null = null
    do {
      const page: StatementPage = await get<StatementPage>(
        `${base(entity)}/statements${query({ limit: 200, cursor })}`,
      )
      out.push(...page.items)
      cursor = page.next_cursor
    } while (cursor)
    return { items: out }
  },

  /** One statement by id: its period, balances, document and replacement chain.  Chains are walked one id
   *  at a time, hence a by-id fetch rather than a lookup in the listing. */
  statement: (entity: string, statementId: string) =>
    get<StatementResponse>(`${base(entity)}/statements/${encodeURIComponent(statementId)}`),

  /**
   * Replace a wrongly loaded statement with a restatement of the same subject that points back at it.
   *
   * `account` and `commodity` are required by the schema and copied from the predecessor.  The service refuses
   * a replacement whose subject differs ("a replacement restates the same subject"), so the form offers no
   * account picker.  A statement loaded against the wrong account can't be fixed by replacement; `Statements.tsx`
   * says so on the page.
   *
   * The predecessor is kept, stays readable and is named in the result.  Replacement is refused while the
   * predecessor's period is locked, while any of its lines is still booked, or if it was already replaced.
   */
  replaceStatement: (
    entity: string,
    statementId: string,
    body: {
      account: string
      commodity: string
      period_start: string
      period_end: string
      opening: string
      closing: string
      lines: StatementLineSubmission[]
      document: DocumentSubmission
      locator: string
      page?: number | null
      reason: string
    },
  ) =>
    post<StatementWriteResponse>(
      `${base(entity)}/statements/${encodeURIComponent(statementId)}/replace`,
      body,
    ),

  reconciliation: (entity: string, asOf: string, from?: string) =>
    get<Reconciliation>(`${base(entity)}/reconciliation${query({ as_of: asOf, from })}`),

  trialBalance: (entity: string, asOf?: string) =>
    get<TrialBalance>(`${base(entity)}/reports/trial-balance${query({ as_of: asOf })}`),

  /** A period's income statement and balance sheet, arranged and signed by the service.
   *
   * The canonical shaped view (issue 0008 section B).  The UI draws these sections as served instead of
   * ordering, signing and subtotalling lines itself, so the screen and the PDF agree. */
  statementsDocument: (entity: string, from: string, to: string) =>
    get<StatementsDocument>(`${base(entity)}/reports/statements${query({ from, to })}`),

  /** One account's movements over a range, following the cursor to the last posting. */
  register: async (
    entity: string,
    accountId: string,
    range: { from?: string; to?: string } = {},
  ): Promise<Register> => {
    const path = `${base(entity)}/accounts/${encodeURIComponent(accountId)}/register`
    let page: Register = await get<Register>(`${path}${query({ ...range, limit: 200 })}`)
    const items = [...page.items]
    while (page.next_cursor) {
      page = await get<Register>(`${path}${query({ ...range, limit: 200, cursor: page.next_cursor })}`)
      items.push(...page.items)
    }
    return { ...page, items, next_cursor: null }
  },

  /** Tax not being reclaimed, and why.  Totals are per commodity. */
  unrecoveredTax: (entity: string, params: { from?: string; to?: string } = {}) =>
    get<UnrecoveredPage>(`${base(entity)}/unrecovered-tax${query({ ...params, limit: 200 })}`),

  /** Provisional bookings: every booking still awaiting a document. */
  provisional: (entity: string, params: { from?: string; to?: string } = {}) =>
    get<ProvisionalPage>(`${base(entity)}/provisional${query({ ...params, limit: 200 })}`),

  provenance: (entity: string, lineId: string) =>
    get<Provenance>(`${base(entity)}/provenance${query({ line_id: lineId })}`),

  /** Post an entry for this line and link the line to it.  Posted directly, not drafted (issue 0003). */
  book: (entity: string, lineId: string, body: { legs: Leg[]; narration: string; reason: string }) =>
    post<{ line: Line }>(`${base(entity)}/lines/${encodeURIComponent(lineId)}/book`, body),

  /**
   * Replace this line's explanation.  The old explanation is kept and the new one references it; if the old
   * one booked an entry, that entry is fixed through the normal correction path.
   */
  reexplain: (
    entity: string,
    lineId: string,
    body: { kind: "booked" | "linked" | "ignored"; legs?: Leg[]; narration?: string; reason: string },
  ) => post<{ line: Line }>(`${base(entity)}/lines/${encodeURIComponent(lineId)}/reexplain`, body),

  /** Record that a line won't be booked, and why. */
  ignore: (entity: string, lineId: string, body: { reason: string }) =>
    post<{ line: Line }>(`${base(entity)}/lines/${encodeURIComponent(lineId)}/ignore`, body),

  /** Record that no document is coming after all.  The booking stands. */
  withdrawExpectation: (entity: string, entryId: string, body: { reason: string }) =>
    post<unknown>(`${base(entity)}/entries/${encodeURIComponent(entryId)}/awaiting-document/withdraw`, body),

  /** Reverse a posted entry by posting an exact mirror beside it.  Both stay in the books. */
  reverse: (entity: string, entryId: string, body: { reason: string }) =>
    post<{ entry: Entry }>(`${base(entity)}/entries/${encodeURIComponent(entryId)}/reverse`, body),

  /** One entry: its legs, current status, and every correction it is part of (issue 0007). */
  entry: (entity: string, entryId: string) =>
    get<EntryDetail>(`${base(entity)}/entries/${encodeURIComponent(entryId)}`),

  /**
   * The append-only log for one entry and for the corrections that reference it.
   *
   * A correction is its own subject in the log, so events for the entry's id alone would show a reversed
   * posting as if nothing had happened.  The service joins them; this asks for enough of them.
   */
  entryEvents: (entity: string, entryId: string, limit = 200) =>
    get<EntryEvents>(`${base(entity)}/entries/${encodeURIComponent(entryId)}/events${query({ limit })}`),

  /**
   * The last `count` events in the entity's log, newest first.
   *
   * The log is only served forward ("everything after N"), so this takes two calls: a one-item read to learn
   * `high_water`, then everything after `high_water - count`.  Reading from `since: 0` and keeping the tail
   * would fetch the entire log on real books.
   */
  recentEvents: async (entity: string, count = 12): Promise<EventPage> => {
    const head = await get<EventPage>(`${base(entity)}/events${query({ since: 0, limit: 1 })}`)
    const from = Math.max(0, head.high_water - count)
    const page = await get<EventPage>(`${base(entity)}/events${query({ since: from, limit: count })}`)
    return { ...page, items: [...page.items].reverse() }
  },

  /** The documents behind this entry: the statement lines behind its legs, and anything filed on it. */
  entryProvenance: (entity: string, entryId: string) =>
    get<Provenance>(`${base(entity)}/provenance${query({ entry_id: entryId })}`),

  /** One registered document and every record that cites it. */
  document: (entity: string, documentId: string) =>
    get<DocumentResponse>(`${base(entity)}/documents/${encodeURIComponent(documentId)}`),

  /**
   * The browser URL for a document's bytes.
   *
   * Built from the service's `links.content` rather than assembled here, so a document the service won't serve
   * (one held elsewhere) has no URL and the UI shows no button for it.  Goes through `uiHref` like every other
   * service link.
   *
   * `inline` is only a request: the service honours it for media types that can't execute and downloads
   * everything else.
   */
  documentContentHref: (link: string | undefined, { inline = false } = {}) =>
    link === undefined ? undefined : `${uiHref(link)}${inline ? "?disposition=inline" : ""}`,

  /**
   * Post a balanced journal entry with no statement line behind it: an accrual, reclassification or adjustment.
   * Legs are account paths and exact decimal amounts (the command vocabulary of issue 0006).
   */
  postEntry: (
    entity: string,
    body: { dt: string; narration: string; reason: string; legs: Leg[] },
  ) => post<{ entry: Entry }>(`${base(entity)}/entries`, body),

  /**
   * Reverse a posted entry and post its replacement in one transaction.
   *
   * `reverse` withdraws a booking; this restates it.  Both halves land together, so there is never a moment
   * when the original is reversed and nothing stands in its place.
   */
  correctEntry: (
    entity: string,
    entryId: string,
    body: {
      reason: string
      replacement: { dt: string; narration: string; legs: Leg[] }
    },
  ) => post<{ correction_id: string; mirror_id: string; replacement_id: string; entry: Entry }>(
    `${base(entity)}/entries/${encodeURIComponent(entryId)}/correct`,
    body,
  ),

  /**
   * Export the books as hledger journal files over the given range.
   *
   * The service reads the store, renders deterministically, checks the output with the pinned `hledger` binary
   * and publishes only if that passes.  It answers with the immutable record and one `href` per file.  No
   * `reason`: an export changes nothing.
   *
   * Exporting the same range again is allowed.  An unchanged book produces byte-identical files and reuses the
   * snapshot the first export recorded, so replacing what is there is the sensible default.
   */
  runExport: (entity: string, body: { from?: string; to?: string; year?: number; overwrite?: boolean }) =>
    post<ExportRun>(`${base(entity)}/exports`, body),

  /** Whether this period would close, and if not, what's outstanding.  Read-only. */
  closeChecks: (entity: string, through: string) =>
    get<CloseChecks>(`${base(entity)}/close-checks${query({ through })}`),

  /**
   * One close or reopen, with a link to the snapshot it was recorded against.
   *
   * Lets a reviewer get from the lock to the immutable evidence behind it without walking the event log.
   * `EntitySummary.lock_close_id` is the id to pass.
   */
  closeRecord: (entity: string, closeId: string) =>
    get<CloseRecord>(`${base(entity)}/closes/${encodeURIComponent(closeId)}`),

  /**
   * Close the books through a date.
   *
   * `expected_version` is the lock version the page was showing.  If somebody else closed in the meantime the
   * service refuses with `stale_version` rather than closing over a state this reviewer never saw.  That's why
   * the client sends it instead of the server reading it.
   */
  closePeriod: (
    entity: string,
    body: { through: string; reason: string; expected_version: number; overrides: CloseOverride[] },
  ) => post<ClosePeriodResult>(`${base(entity)}/close`, body),

  /** Move the lock back to reopen a period, with a reason.  Not gated on the close checks. */
  reopenPeriod: (entity: string, body: { through: string; reason: string; expected_version: number }) =>
    post<ReopenPeriodResult>(`${base(entity)}/reopen`, body),
}
