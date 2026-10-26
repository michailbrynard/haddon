"""The published schemas: what ``/openapi.json`` documents is what the routes actually return.

Every response model here is declared on its route, and each body is validated against it on the way out, so a
shape that drifts from its documentation fails loudly instead of shipping an undocumented field.

Amounts are strings everywhere, in both directions.  Public journal commands accept decimal strings in whole
commodity units (``"-12.00"``); stored facts and query results expose ``amount_micros`` as a base-10 integer
string.  No amount is ever a JSON number: a float would lose exactness and a JSON integer would invite one, so the
request path refuses numbers outright (``haddon.api.wire.reject_wire_numbers``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MICROS_PATTERN = r"^-?(0|[1-9][0-9]*)$"
MICROS = Field(description="Signed integer micro-units (millionths) as a base-10 string; never a JSON number.",
               pattern=MICROS_PATTERN, examples=["-12000000"])

#: The pattern a decimal amount must match before the commodity's declared precision is applied.  Only the shape
#: is checked here.  "12.345678" is well-shaped and is still refused for a two-decimal currency, by
#: ``Micros.from_decimal``, the one place that knows what the currency is.
DECIMAL_PATTERN = r"^-?[0-9]+(\.[0-9]+)?$"
DECIMAL = Field(description="Signed decimal amount in whole commodity units, as a string; never a JSON number. "
                            "Converted by the commodity's declared precision, so more decimal places than the "
                            "currency has is refused rather than rounded.",
                pattern=DECIMAL_PATTERN, examples=["-12.00"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Errors
class ErrorBody(Strict):
    code: str
    message: str
    retryable: bool
    details: list[dict[str, Any]]
    request_id: str | None = None
    next_action: dict[str, Any] | None = None


class ErrorEnvelope(Strict):
    error: ErrorBody


ERRORS = {
    400: {"model": ErrorEnvelope}, 401: {"model": ErrorEnvelope}, 403: {"model": ErrorEnvelope},
    404: {"model": ErrorEnvelope}, 409: {"model": ErrorEnvelope}, 412: {"model": ErrorEnvelope},
    422: {"model": ErrorEnvelope}, 500: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope},
}


# Service
class Health(Strict):
    status: str
    service: str
    api_version: str


class Check(Strict):
    name: str
    ok: bool
    detail: str


class Readiness(Strict):
    status: str
    checks: list[Check]


class EntitySummary(Strict):
    entity_id: str
    legal_name: str
    legal_form: str
    country: str
    base_currency: str
    commodities: list[str]
    lock_through: str | None
    lock_version: int
    lock_close_id: str | None = Field(
        description="the close record that set the current lock; the way from 'this period is closed' to the "
                    "snapshot it was closed against, without walking /events")
    live_close_eligible: bool
    links: dict[str, str]


class CommodityDeclaration(Strict):
    code: str = Field(description="the commodity's code, e.g. USD")
    precision: int = Field(ge=0, le=6, description="decimal places this commodity is held to; 2 for USD, 0 for JPY")


class EntityCreationSubmission(Strict):
    """Create one entity: its database file, its schema, its commodities and its opening chart.

    Everything here is supplied by the caller, because every part of it is permanent or nearly so.  The id is
    the entity's address in every URL and the name of the file it lives in, so it is a plain slug with no second
    spelling.

    ``base_precision`` is explicit, as ``initialize_entity`` requires: defaulting to two decimals would silently
    hold a zero-decimal currency to hundredths, and every later amount would be wrong by a factor of a hundred
    without anything reporting it.

    ``starter_chart`` defaults to false.  A book about to be imported or authored wants its own chart, and
    accounts added by a default would have to be noticed and archived.  Either way, the equity account an
    opening balance lands in is created by ``initialize_entity``; without it an entity cannot take an opening
    balance at all.
    """

    entity_id: str = Field(description="the entity's permanent address: 1-63 characters of lower-case letters, "
                                       "digits and hyphens", examples=["acme-holdings"])
    legal_name: str = Field(description="the registered name, as it appears on the statements")
    legal_form: str = Field(description="e.g. Ltd, Inc, B.V., S.L.")
    country: str = Field(description="ISO country code")
    base_currency: str = Field(description="the currency this entity's statements are presented in")
    base_precision: int = Field(ge=0, le=6, description="decimal places for the base currency; 2 for USD, 0 for JPY")
    commodities: list[CommodityDeclaration] = Field(default_factory=list,
                                                    description="further commodities beyond the base currency")
    starter_chart: bool = Field(default=False,
                                description="add the generic starter chart (cash, receivables, payables, general "
                                            "expenses, sales) as well as the retained-earnings equity account")
    tax_regime: dict[str, Any] | None = Field(default=None, description="opaque, recorded on the entity row")
    reason: str | None = Field(default=None, description="why this book exists; kept on the audit action")


class EntityList(Strict):
    items: list[EntitySummary]
    next_cursor: str | None = None
    snapshot_seq: int | None = None


class Limits(Strict):
    default_limit: int
    max_limit: int
    event_default_limit: int
    event_max_limit: int
    max_entry_legs: int
    max_batch_entries: int


class Capabilities(Strict):
    entity_id: str
    legal_name: str
    base_currency: str
    snapshot_seq: int
    actor_recorded_as: str
    entities_in_scope: list[str]
    reads: list[str]
    writes: list[str]
    commands: list[dict[str, str]] = Field(
        description="the named write commands this installation serves: the command's name, its verb and its "
                    "path. The name is the same string as the OpenAPI operationId and the application handler")
    service_reads: list[str] = Field(
        description="the /v1 reads that are not relative to an entity, as absolute paths")
    service_commands: list[dict[str, str]] = Field(
        description="the named write commands that are not relative to an entity, as absolute paths. "
                    "`initialize_entity` is here: a book has to exist before there is an entity to address")
    limits: Limits
    amount_contract: dict[str, str]
    commodities: list[dict[str, Any]]
    account_commodities: list[dict[str, str]]
    idempotency: dict[str, Any]
    pagination: dict[str, Any]
    dates: dict[str, str]


# Entity resources
class EntityScoped(Strict):
    """Fields every entity resource response carries: which entity it belongs to and the state it was read at."""

    entity_id: str
    legal_name: str
    base_currency: str
    snapshot_seq: int


class Account(Strict):
    id: str
    path: str
    type: str
    code: str | None
    tax_code: str | None
    is_cash: bool
    archived: bool
    version: int
    commodities: list[str]
    links: dict[str, str]


class AccountPage(EntityScoped):
    items: list[Account]
    next_cursor: str | None


class AccountResponse(EntityScoped):
    account: Account
    version: int


class AccountCreationSubmission(Strict):
    """Add one account to the chart.

    The chart is authored, not derived: an account written here is permanent, every future report carries it,
    and the only way out is to archive it.  So this asks for everything an account needs and infers nothing.

    ``type`` cannot be read off the path.  ``assets:...`` usually is an asset, but a chart may put a
    contra-asset under ``assets`` or a recovery under ``expenses``, and a guess that lands an account on the
    wrong side of the balance sheet is wrong in every statement afterwards.

    ``is_cash`` defaults to false.  A cash account is one a bank statement is reconciled against, which is a
    fact about the real world rather than the books, and should not be acquired by leaving a field out.

    ``reason`` is optional.  The ``account.created`` audit action keeps whatever is given (issue 0006).  Adding
    an account is structure rather than an assertion about what happened, so "an account was added" is the
    whole content of the act, and is what the action records when no reason is given.  Retiring an account is a
    decision about the books, so ``POST /accounts/{id}/archive`` does require a reason.
    """

    path: str = Field(min_length=1, max_length=255,
                      description="colon-separated, e.g. 'expenses:professional-services:audit'")
    type: str = Field(description="asset | liability | equity | income | expense")
    commodities: list[str] = Field(min_length=1,
                                   description="the currencies this account may hold; every account declares at least one")
    is_cash: bool = Field(default=False, description="true only for an account a bank statement is reconciled against")
    code: str | None = Field(default=None, max_length=255, description="the chart code this account is known by, if any")
    tax_code: str | None = Field(default=None, max_length=255)
    id: str | None = Field(default=None, description="caller-supplied account id; generated when omitted")
    reason: str | None = Field(default=None, min_length=1,
                               description="why this account is being added; kept verbatim on the audit action. "
                                           "Omit it and the action records that an account was added to the chart")


class AccountArchiveSubmission(Strict):
    """Retire an account: nothing may be posted to it again, and everything already posted to it stays.

    ``reason`` is required here, though optional on creation, because this is a decision about the books rather
    than structure.  An archived account stays visible in every later report, and "why did this stop being
    used?" is what a reviewer will ask.

    ``expected_version`` is the account version the caller was looking at, the same concurrency check a close
    uses.  If two callers retire an account from two readings of the chart, the second fails with
    ``stale_version`` rather than acting on a chart it never saw.
    """

    reason: str = Field(min_length=1, description="why nothing may be posted to this account again")
    expected_version: int = Field(ge=1, description="the account version this caller read")


class AccountRestoreSubmission(Strict):
    """Put a retired account back in use: the inverse of archiving, with the reason it had to come back.

    This takes back an archive the way ``reopen_period`` takes back ``close_period``.  History does not move
    either way.  What comes back is the ability to post to the account, so a balance can be corrected, a
    statement line booked and the period closed after an account was retired too early.
    """

    reason: str = Field(min_length=1, description="why this account is in use again")
    expected_version: int = Field(ge=1, description="the account version this caller read")


class ChartChangeResponse(EntityScoped):
    """The reply to an account command: the account as it now stands, and its audit action."""

    account: Account
    version: int
    audit_seq: int = Field(description="the account.created / account.archived action's event sequence")
    links: dict[str, str]


class Posting(Strict):
    id: str
    seq: int
    account_id: str
    account_path: str
    commodity: str
    amount_micros: str = MICROS
    memo: str | None
    tax_code: str | None


class Entry(Strict):
    id: str
    dt: str
    narration: str
    status: str
    origin: str
    version: int
    code: str  # hledger tcode; '' when there is none
    comment: str  # hledger tcomment, verbatim
    tags: list[list[str]]  # [name, value] pairs, in the order they appear in the comment
    created_at: str
    created_by: str
    postings: list[Posting]
    links: dict[str, str]


class EntryPage(EntityScoped):
    items: list[Entry]
    next_cursor: str | None


class CorrectionLink(Strict):
    """One link in an entry's correction chain, and which end of it this entry is.

    ``role`` saves a reader re-deriving the relationship from three ids: ``original`` means this entry was
    corrected, ``mirror`` means it is the reversal, and ``replacement`` means it is what was posted in the
    original's place.  A reversal with no restatement has no ``replacement_id``: "the books stopped saying this"
    rather than "the books say it differently now".
    """

    id: str
    role: str = Field(description="original | mirror | replacement — which end of this correction the entry is")
    original_id: str
    mirror_id: str
    replacement_id: str | None
    reason: str
    created_at: str
    created_by: str
    links: dict[str, str]


class EntryResponse(EntityScoped):
    entry: Entry
    version: int
    reversed_by: str | None = Field(
        description="the mirror that reverses this entry, when one has been posted; a posted entry that "
                    "already has one cannot be reversed again")
    corrections: list[CorrectionLink] = Field(
        description="every correction this entry is part of, whichever end of it it is; issue 0007")
    state: str = Field(description="posted | corrected | reversed | draft | void — what this entry is now")


class RegisterLine(Strict):
    entry_id: str
    dt: str
    seq: int
    narration: str
    origin: str
    account_id: str
    account_path: str
    commodity: str
    amount_micros: str = MICROS
    running_micros: str = MICROS
    memo: str | None
    tax_code: str | None


class Register(EntityScoped):
    account_id: str
    account_path: str
    start: str | None
    end: str | None
    opening_micros: dict[str, str]
    items: list[RegisterLine]
    next_cursor: str | None
    links: dict[str, str]


class ReportLine(Strict):
    account_id: str
    account_path: str
    type: str
    commodity: str
    amount_micros: str = MICROS


class TrialBalance(EntityScoped):
    report: str
    through: str | None
    lines: list[ReportLine]
    totals: dict[str, str]
    links: dict[str, str]


class BalanceSheet(EntityScoped):
    report: str
    through: str | None
    sections: dict[str, list[ReportLine]]
    #: Per-commodity net of the three sections.  Not zero on its own: see ``result_micros``.
    totals: dict[str, str]
    #: The accumulated income and expense through ``through``, which equity carries and nothing posts to it.
    #: ``totals[c] + result_micros[c] == 0`` on a balanced journal, at any date.
    result_micros: dict[str, str]
    links: dict[str, str]


class IncomeStatement(EntityScoped):
    report: str
    start: str | None
    end: str | None
    lines: list[ReportLine]
    net_micros: dict[str, str]
    links: dict[str, str]


class Composition(EntityScoped):
    report: str
    start: str | None
    end: str | None
    origins: dict[str, int]
    journals: dict[str, int]
    entry_count: int
    links: dict[str, str]


# The shaped statements (issue 0008 section B).
# The arrangement of a statement (sections, groups, signs, subtotals, the result row and whether the two sides
# of the sheet meet) as the service decides it.  ``/reports/statements`` and the PDF are both built from
# ``haddon.statements.shape``; these models are that arrangement's wire form, and the interface reads it rather
# than rebuilding it.  An absent comparative is ``null``; ``_micros`` is always a string.
class ShapedLine(Strict):
    key: str
    account_id: str | None = Field(description="the account a line links to; null for a synthetic row")
    label: str
    amount_micros: str = MICROS
    comparative_micros: str | None = Field(default=None, pattern=MICROS_PATTERN)


class ShapedGroup(Strict):
    key: str
    heading: str | None = Field(description="null when the group holds a single account and needs no heading")
    subtotal_micros: str | None = Field(default=None, pattern=MICROS_PATTERN)
    comparative_subtotal_micros: str | None = Field(default=None, pattern=MICROS_PATTERN)
    lines: list[ShapedLine]


class ShapedSection(Strict):
    key: str
    title: str
    total_label: str
    total_micros: str = MICROS
    comparative_total_micros: str | None = Field(default=None, pattern=MICROS_PATTERN)
    groups: list[ShapedGroup]


class IncomeStatementView(Strict):
    commodity: str
    start: str | None
    end: str | None
    comparative_start: str | None
    comparative_end: str | None
    result_label: str = Field(description="this commodity's own bottom line, said rather than left to the sign; "
                                          "a document of several commodities has one label per commodity")
    sections: list[ShapedSection]
    result_micros: str = MICROS
    comparative_result_micros: str | None = Field(default=None, pattern=MICROS_PATTERN)


class BalanceCheck(Strict):
    date: str
    difference_micros: str = MICROS


class BalanceSheetView(Strict):
    commodity: str
    as_of: str
    comparative_as_of: str | None
    sections: list[ShapedSection]
    total_assets_micros: str = MICROS
    total_funding_micros: str = MICROS
    difference_micros: str = MICROS
    comparative_total_assets_micros: str | None = Field(default=None, pattern=MICROS_PATTERN)
    comparative_total_funding_micros: str | None = Field(default=None, pattern=MICROS_PATTERN)
    comparative_difference_micros: str | None = Field(default=None, pattern=MICROS_PATTERN)
    checks: list[BalanceCheck] = Field(description="one entry per column shown: assets less the two sides funding "
                                                  "them, which is zero on a balanced journal at every date")


class PeriodLabel(Strict):
    start: str
    end: str
    name: str
    heading: str


class ComparativeLabel(Strict):
    start: str
    end: str
    name: str


class CommodityStatements(Strict):
    commodity: str
    income_statement: IncomeStatementView
    balance_sheet: BalanceSheetView


class StatementsDocument(EntityScoped):
    report: str
    commodity: str = Field(description="the commodity the document leads with: the one requested, or the base one")
    period: PeriodLabel
    comparative: ComparativeLabel | None
    as_of: str
    footnote: str = Field(description="the sign convention and the commodities this document does not show")
    statements: list[CommodityStatements] = Field(description="one shaped income statement and balance sheet per "
                                                              "commodity these books hold in the period")
    other_commodities: list[str]
    links: dict[str, str]


class Event(Strict):
    seq: int
    kind: str
    subject_kind: str
    subject_id: str
    before: Any
    after: Any
    reason: str | None
    request_id: str | None
    created_at: str
    created_by: str


class EventPage(EntityScoped):
    items: list[Event]
    since: int
    next_since: int
    high_water: int


class EntryEvents(EntityScoped):
    entry_id: str
    items: list[Event]
    since: int
    next_since: int
    high_water: int


class Snapshot(EntityScoped):
    id: str
    snapshot_seq_of_snapshot: int
    state_hash: str
    through: str
    manifest: Any
    report: Any
    checks: Any
    created_at: str
    created_by: str
    links: dict[str, str]


class Export(EntityScoped):
    id: str
    snapshot_id: str
    operation_id: str
    manifest_sha256: str
    artifacts: list[dict[str, Any]]
    verifier: dict[str, Any]
    created_at: str
    created_by: str
    links: dict[str, str]


class ExportSubmission(Strict):
    """Which range of this book to render as hledger journal files.

    The default is the whole journal, which is the exporter's own default: a journal is continuous (issue 0002),
    so there is no natural default year.  ``year`` is only a calendar-year shorthand, exactly ``from`` the first
    of January ``to`` the thirty-first of December, and may not be combined with either bound.  Either bound may
    be left open on its own, in which case it is filled from what is actually posted.

    There is no ``reason``.  Every write that decides something takes one; an export decides nothing, it reads
    the book and renders it (``docs/design/decisions.md``, "Chunk C2a").  What is recorded is the range, the
    actor and the state that was rendered.
    """

    year: int | None = Field(default=None, ge=1, le=9999,
                             description="calendar-year shorthand for from/to; not combined with either")
    from_: str | None = Field(default=None, alias="from",
                              description="inclusive first day of the range (YYYY-MM-DD); omit for the first "
                                          "posting")
    to: str | None = Field(default=None,
                           description="inclusive last day of the range (YYYY-MM-DD); omit for the last posting")
    overwrite: bool = Field(default=True,
                            description="replace an export already published for this range. True by default: "
                                        "the directory holds one export per range and is exclusively managed by "
                                        "this service, so there is nothing of anybody else's to overwrite -- an "
                                        "unmanaged file in it refuses the whole export either way")


class ExportArtifact(Strict):
    """One published file: its name in the ledger directory, its digest and its length.

    The same three fields the ``exports`` row stores, plus where to fetch it.  The digest is the integrity
    claim that these are the bytes ``hledger check --strict`` passed under the pinned binary, and the download
    re-hashes the file against it rather than trusting the directory.
    """

    path: str = Field(description="the file name, relative to this export's ledger directory")
    sha256: str
    bytes: int
    href: str = Field(description="where these bytes are served from")


class ExportRun(EntityScoped):
    """What one export wrote: the immutable row, the state it was rendered from, and the files themselves."""

    export_id: str
    snapshot_id: str
    operation_id: str
    state_hash: str = Field(description="the digest of everything rendered; one state is one snapshot")
    manifest_sha256: str
    range_start: str = Field(description="the inclusive first day actually covered (YYYY-MM-DD)")
    range_end: str = Field(description="the inclusive last day actually covered (YYYY-MM-DD)")
    ledger_dir: str = Field(description="the directory the files were published into, relative to this "
                                        "entity's export root")
    artifacts: list[ExportArtifact]
    counts: dict[str, Any] = Field(description="files, entries, postings, accounts, commodities and the source "
                                               "groups the range covers")
    verifier: dict[str, Any] = Field(description="the pinned hledger build that checked the output, and its "
                                                 "verdict")
    event_seq: int = Field(description="the export.completed event's sequence")
    links: dict[str, str]


# Close eligibility
class CloseOverride(Strict):
    """Close over one named blocking check: the check, and why.  There is no id to pre-create and no wildcard.

    An override is matched against the checks as the server has just evaluated them, so it can only name a
    failure that is real at this moment.  A blanket bypass cannot be expressed.  Each override is stored on the
    close record with the check's status and outstanding count as they stood, the acting principal and this
    reason.
    """

    check: str = Field(min_length=1, description="the check key this override answers, e.g. 'statement_coverage'")
    reason: str = Field(min_length=1, description="why this period is being closed with that check unsatisfied")


class CloseOutstanding(Strict):
    """One specific thing that would have to change for a check to pass.

    Amounts inside ``detail`` are integer micro strings, as everywhere else."""

    kind: str
    message: str
    detail: dict[str, Any]


class CloseWaiver(Strict):
    """An override, matched to the check it let through and to what that check found at that moment."""

    check: str
    through: str
    reason: str
    actor: str | None = Field(description="the principal that ran the close; null on a read-only preview")
    status_at_close: str
    reason_at_close: str
    outstanding_at_close: int


class CloseCheck(Strict):
    check: str
    title: str
    requirement: str
    status: str = Field(description="pass | fail | unevaluable; only 'pass' is a positive answer")
    reason: str
    blocking: bool
    outstanding: list[CloseOutstanding]
    outstanding_truncated: int
    waiver: CloseWaiver | None


class PeriodLock(Strict):
    through: str | None = Field(description="the last date the books are shut through; null when nothing is closed")
    version: int = Field(description="pass this back as expected_version on the next close or reopen")
    close_id: str | None


class CloseChecks(EntityScoped):
    through: str
    window_start: str
    prior_through: str | None
    months: list[dict[str, str]]
    eligible: bool = Field(description="every check positively passed; a recorded override does NOT make this true")
    may_close: bool = Field(description="nothing blocks: every check passed or a recorded override waives it")
    checks: list[CloseCheck]
    blocking_checks: list[str]
    unmatched_overrides: list[CloseOverride] = Field(
        description="overrides that name no currently-blocking check; a close would refuse them")
    lock: PeriodLock = Field(
        description="the period lock as this evaluation read it. Its `version` is the `expected_version` a close "
                    "or reopen sends back, so the answer to 'would this close?' carries what performing it needs")
    links: dict[str, str]


class CloseSnapshot(Strict):
    """The verification snapshot the close was recorded against.  Computed by the server, never supplied."""

    id: str
    snapshot_seq: int
    state_hash: str
    through: str


class ClosePeriodSubmission(Strict):
    """Shut the books through a date.

    A date, a reason, the lock version you believe is current and, only where a check is blocking, an override
    naming that check and saying why.  Nothing else: the verification snapshot is computed by the server,
    because a caller that could supply one could supply a hash of a state that never existed.

    ``expected_version`` guards against two agents closing the same books from different readings of them; the
    second fails with ``stale_version`` and writes nothing.  Retrying the same intent is a different matter,
    handled by ``Idempotency-Key``.
    """

    through: str = Field(description="close through this date, inclusive (YYYY-MM-DD)")
    reason: str = Field(min_length=1, description="why the period is being closed; kept verbatim in the record")
    expected_version: int = Field(ge=1, description="the period lock version this close expects to advance")
    overrides: list[CloseOverride] = Field(default_factory=list,
                                           description="one per blocking check being closed over")


class ClosePeriodResponse(EntityScoped):
    close_id: str
    kind: str
    lock: PeriodLock
    snapshot: CloseSnapshot
    checks: dict[str, Any] = Field(description="the checks this close ran, exactly as GET /close-checks reports them")
    audit_seq: int
    links: dict[str, str]


class ReopenPeriodSubmission(Strict):
    """Move the lock deliberately backwards, and say why.

    Give exactly one of ``through`` or ``close_id``.  ``through`` is a boundary ("open March again");
    ``close_id`` is an action ("undo that close") and resolves to the boundary that close advanced from.

    A reopen is not gated on the close checks, because the month that most needs reopening is the one that
    cannot pass them.  Nothing is undone: the close record and its snapshot stay where they are.
    """

    through: str | None = Field(default=None, description="reopen back to this date, inclusive (YYYY-MM-DD)")
    close_id: str | None = Field(default=None, description="the close to undo; resolves to its prior boundary")
    reason: str = Field(min_length=1, description="why a closed period is being opened again")
    expected_version: int = Field(ge=1, description="the period lock version this reopen expects to move")


class ReopenPeriodResponse(EntityScoped):
    close_id: str
    kind: str
    reopened_close_id: str | None
    lock: PeriodLock
    snapshot: CloseSnapshot
    audit_seq: int
    links: dict[str, str]


class CloseRecord(EntityScoped):
    """One close or reopen, read back.  ``links.snapshot`` is the immutable state it was recorded against."""

    id: str
    kind: str
    through: str
    prior_through: str | None
    snapshot_id: str
    reason: str
    overrides: list[dict[str, Any]] = Field(
        description="the waivers this close was let through on, each with the check, the actor and the reason; "
                    "empty on a reopen, which waives nothing")
    created_at: str
    created_by: str
    links: dict[str, str]


# Statements and their lines (issue 0004)
class StatementDocument(Strict):
    """The original document, by reference.  The ledger records this and does not fetch it.

    ``sha256`` is the hash of the bytes the agent read, and the key that makes a second load of the same
    statement a refusal.  Give exactly one of ``blob_path`` (bytes this service holds) and ``external_ref``
    (bytes held elsewhere)."""

    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$",
                        description="SHA-256 of the original document, 64 lowercase hex characters")
    filename: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0, description="size of the original document in bytes")
    blob_path: str = Field(default="", max_length=2048, description="where the original document is kept")
    external_ref: str | None = Field(default=None, max_length=2048,
                                     description="a reference to bytes held elsewhere, when we hold none")


class DocumentView(Strict):
    """The original document as a reader sees it: what identifies it, and whether it can be opened here.

    There is deliberately no ``blob_path`` (removed in issue 0007): a server-local path is no use to a reviewer
    and discloses where the service keeps files.  ``storage`` says instead whether the bytes are available:
    ``stored`` means ``GET /documents/{id}/content`` will hand them over, ``external`` means only
    ``external_ref`` exists and the document is held elsewhere.
    """

    sha256: str = Field(description="SHA-256 of the registered bytes, and the document's identity")
    filename: str
    media_type: str
    size_bytes: int
    storage: str = Field(description="stored | external")
    external_ref: str | None = Field(description="where the bytes are, when this service does not hold them")


class StatementLineSubmission(Strict):
    """One normalised statement line.

    ``currency`` is required and must equal the statement's commodity: the ledger declares currency, it never
    infers it."""

    dt: str = Field(description="the line's value date (YYYY-MM-DD); must fall inside the declared period")
    description: str = Field(min_length=1, max_length=4096)
    amount: str = DECIMAL
    currency: str = Field(min_length=1, max_length=64)
    reference: str | None = Field(default=None, max_length=512,
                                  description="the document's own reference for this line, when it has one")
    raw: str | None = Field(default=None, max_length=8192,
                            description="the line exactly as it appeared in the document, when that can be given")
    source_key: str | None = Field(default=None, max_length=512,
                                   description="the line's stable identity in the document; derived when omitted")


class StatementSubmission(Strict):
    """A statement: one account, one period, an opening and a closing balance, a list of lines, and the document.

    There is no file format here.  Haddon parses nothing: whatever the bank produced (CSV, XLSX, PDF, a pasted
    table) is read outside the ledger and submitted as normalised lines.  The ledger enforces what a parser must
    not be trusted with, and refuses the whole submission if any of it fails:

    1. the lines must sum to ``closing`` minus ``opening``;
    2. every line's ``dt`` must fall inside ``period_start``..``period_end``;
    3. the currency must be declared, on the statement and on every line, and they must agree;
    4. the same document cannot be loaded twice (by ``document.sha256``).

    ``opening`` is checked against the closing balance already recorded for the period ending the day before,
    and a disagreement is refused.  When there is no such record the statement is still accepted, and the
    response says ``opening_verified: false`` with an ``opening_basis`` naming why.  It is never assumed to be 0.
    """

    account: str = Field(min_length=1, max_length=255,
                         description="account path (preferred) or stable account id")
    commodity: str = Field(min_length=1, max_length=64)
    period_start: str = Field(description="first day of the statement period (YYYY-MM-DD)")
    period_end: str = Field(description="last day of the statement period; inclusive, and free to cross a year")
    opening: str = DECIMAL
    closing: str = DECIMAL
    lines: list[StatementLineSubmission] = Field(default_factory=list)
    document: StatementDocument
    locator: str = Field(min_length=1, max_length=512,
                         description="where in the document the closing balance is printed")
    page: int | None = Field(default=None, ge=1, description="page of the document the closing balance is on")
    reason: str = Field(min_length=1, description="why this statement is being loaded; kept in the record")
    id: str | None = Field(default=None, description="caller-supplied statement id; generated when omitted")


class StatementReplacementSubmission(StatementSubmission):
    """Replace a statement loaded wrongly: the same subject, restated, pointing back at its predecessor.

    The predecessor is kept and stays readable, because what was believed and when is part of the record.  The
    replacement is refused if the predecessor has already been replaced, if its period is locked, or if any of
    its lines is still booked.  Undo those bookings first: a replacement must not silently orphan an entry it
    caused.
    """


class Statement(Strict):
    statement_id: str
    account_id: str
    account_path: str
    commodity: str
    period_start: str
    period_end: str
    opening_micros: str = MICROS
    closing_micros: str = MICROS
    opening_verified: bool = Field(description="true only when the opening equals a recorded prior closing")
    opening_basis: str = Field(description="which of the three opening cases this is")
    prior_statement_id: str | None
    line_count: int
    lines_micros: str = MICROS
    document_id: str
    document: DocumentView
    page: int | None
    locator: str
    reason: str
    replaces_id: str | None = Field(description="the statement this one replaces, when it is a replacement")
    replaced_by_id: str | None = Field(description="the statement that replaced this one, when one has")
    live: bool = Field(description="nothing replaces this statement")
    created_at: str
    created_by: str
    links: dict[str, str]


class StatementPage(EntityScoped):
    items: list[Statement]
    next_cursor: str | None


class StatementResponse(EntityScoped):
    statement: Statement


class StatementWriteResponse(StatementResponse):
    """The reply to loading or replacing a statement: the statement, and the action that recorded it.

    The read routes use ``StatementResponse``, which has no ``audit_seq`` because a read has no action behind
    it.  Hence a separate model rather than an optional field.
    """

    audit_seq: int = Field(description="the statement.loaded / statement.replaced action's event sequence")


class StatementLine(Strict):
    line_id: str
    statement_id: str
    line_no: int
    source_key: str
    content_hash: str
    dt: str
    amount_micros: str = MICROS
    description: str
    reference: str | None
    currency: str
    raw: str
    links: dict[str, str]


class StatementLinePage(EntityScoped):
    statement_id: str
    items: list[StatementLine]
    next_cursor: str | None
    links: dict[str, str]


class ProvenanceAllocation(Strict):
    allocation_id: str
    explanation_id: str
    kind: str
    posting_id: str
    entry_id: str
    amount_micros: str = MICROS


class ProvenanceLine(Strict):
    """One statement line traced back to the document it was read off."""

    line_id: str
    line_no: int
    source_key: str
    content_hash: str
    dt: str
    amount_micros: str = MICROS
    description: str
    reference: str | None
    currency: str
    raw: str
    statement_id: str
    account_id: str
    commodity: str
    period_start: str
    period_end: str
    statement_live: bool
    document_id: str
    document: DocumentView
    allocations: list[ProvenanceAllocation]
    links: dict[str, str]


class ProvenanceEvidence(Strict):
    evidence_id: str
    entry_id: str | None
    statement_id: str | None
    page: int | None
    locator: str
    caption: str
    document_id: str
    document: DocumentView


class ProvenanceSubject(Strict):
    kind: str
    id: str


class Provenance(EntityScoped):
    """The answer to "where did this come from?": the actual document, or nothing.

    For a statement line, the answer is the document it was read off.  For a posting or an entry, it is the
    lines behind it, and so their documents.  A subject this entity does not hold is a 404; an empty ``lines``
    list means the subject exists but nothing links it to a document yet.
    """

    subject: ProvenanceSubject
    lines: list[ProvenanceLine]
    evidence: list[ProvenanceEvidence]
    line_count: int
    links: dict[str, str]


# Explaining a line (book/link/ignore/reexplain)
class LineLeg(Strict):
    """One contra leg of a booking: what the line was for.

    The cash leg is not supplied and cannot be: it is the statement line itself, on the line's own account for
    the line's own amount.  These legs are the other side, and must sum to the negative of the line.
    """

    account: str = Field(min_length=1, max_length=255,
                         description="account path (preferred) or stable account id")
    commodity: str = Field(min_length=1, max_length=64)
    amount: str = DECIMAL
    memo: str | None = Field(default=None, max_length=4096)
    tax_code: str | None = Field(default=None, max_length=255)


class LineBookSubmission(Strict):
    """Post the entry this line is, and hold the line against the cash posting it creates, in one call.

    The entry goes through the ordinary posting path with all its checks, and is posted, not drafted: since
    issue 0003 there is no two-step approval, and a booked line is in the books immediately.  To change it
    later, use ``reexplain``, which reverses or corrects it.
    """

    legs: list[LineLeg] = Field(min_length=1, description="the contra side; sums to minus the line's amount")
    narration: str = Field(min_length=1, max_length=4096)
    reason: str = Field(min_length=1, description="why this is what the line is; kept in the record")
    dt: str | None = Field(default=None, description="entry date; defaults to the line's own date")
    origin: str | None = Field(default=None, description="entries.origin; defaults to 'source'")
    memo: str | None = Field(default=None, max_length=4096, description="memo on the cash leg")
    tax_code: str | None = Field(default=None, max_length=255, description="tax code on the cash leg")
    code: str = Field(default="", max_length=255, description="hledger transaction code")
    comment: str = Field(default="", max_length=4096)
    id: str | None = Field(default=None, description="caller-supplied entry id; generated when omitted")


class LinePartSubmission(Strict):
    """One part of a split: an existing posting, and how much of the line it is.

    A posting is named by id: it has no path, and naming its account would not say which leg.  The amount is in
    the line's own commodity, the only commodity a part can be in.
    """

    posting_id: str = Field(min_length=1, max_length=255)
    amount: str = DECIMAL


class LineLinkSubmission(Strict):
    """Say which existing posting(s) this line is, and for how much.  This posts nothing.

    Several parts make a split: one line explained by two or more postings.  A sum short of the line's amount is
    a partial allocation, a real state that the line reports and the close refuses until it is completed.
    """

    parts: list[LinePartSubmission] = Field(min_length=1)
    reason: str = Field(min_length=1)
    id: str | None = Field(default=None, description="caller-supplied explanation id; generated when omitted")


class LineIgnoreSubmission(Strict):
    """Record that this line will not be booked, and why.

    An ignored line counts as explained, and the close checks take it as answered.  That is why the reason is
    required: it is the whole content of the decision a reviewer reads later.
    """

    reason: str = Field(min_length=1)
    id: str | None = Field(default=None, description="caller-supplied explanation id; generated when omitted")


class LineReexplainSubmission(Strict):
    """Supersede what was said about this line before.

    The previous explanation is kept and the new one names it.  If the previous one booked an entry, the books
    are put right through the ordinary correction path in the same transaction: ``kind: booked`` with new legs
    corrects it, and ``kind: linked``/``ignored`` reverses it.  A closed period refuses.
    """

    kind: str = Field(description="booked | linked | ignored")
    reason: str = Field(min_length=1, description="why the previous reading was wrong; recorded verbatim")
    legs: list[LineLeg] | None = Field(default=None, min_length=1, description="required for kind=booked")
    parts: list[LinePartSubmission] | None = Field(default=None, min_length=1,
                                                   description="required for kind=linked")
    narration: str | None = Field(default=None, min_length=1, max_length=4096)
    dt: str | None = Field(default=None)
    memo: str | None = Field(default=None, max_length=4096)
    tax_code: str | None = Field(default=None, max_length=255)
    code: str = Field(default="", max_length=255)
    comment: str = Field(default="", max_length=4096)
    id: str | None = Field(default=None, description="caller-supplied explanation id; generated when omitted")


class LineAllocation(Strict):
    allocation_id: str
    posting_id: str
    entry_id: str
    amount_micros: str = MICROS


class LineExplanation(Strict):
    """One thing somebody said about a line.  The chain of these is the line's history."""

    explanation_id: str
    line_id: str
    kind: str = Field(description="booked | linked | ignored")
    reason: str
    entry_id: str | None
    correction_id: str | None
    supersedes_id: str | None
    allocations: list[LineAllocation]
    allocated_micros: str = MICROS
    created_at: str
    created_by: str


class Line(Strict):
    """What has become of one statement line.

    ``status`` is derived from the live explanation, never stored, so there is exactly one answer.
    ``unexplained_micros`` is the remainder, reported in every state including a fully explained line, where it
    is ``"0"``.  ``history`` is every explanation ever written for this line, oldest first.
    """

    line_id: str
    statement_id: str
    statement_live: bool
    line_no: int
    source_key: str
    dt: str
    amount_micros: str = MICROS
    commodity: str
    account_id: str
    description: str
    reference: str | None
    raw: str
    status: str = Field(description="unexplained | partly_explained | booked | linked | ignored | superseded_input")
    allocated_micros: str = MICROS
    unexplained_micros: str = MICROS
    explanation: LineExplanation | None
    history: list[LineExplanation]
    entries: list["LineEntry"] = Field(default_factory=list,
                                       description="the entries this line is booked or linked to, oldest first")
    links: dict[str, str]


class LineEntry(Entry):
    """An entry a statement line is explained by, with the mirror that already reverses it, if any."""

    reversed_by: str | None = Field(default=None, description="the mirror entry id, when this one was reversed")


Line.model_rebuild()


class LinePage(EntityScoped):
    """Statement lines across every statement, by date: what each one is, and what it was booked to."""

    items: list[Line]
    next_cursor: str | None
    links: dict[str, str]
    filtered_after_page: list[str] = Field(
        default_factory=list,
        description="Filters that were applied to each row's derived state *after* this page was selected. "
                    "While this is non-empty a page may hold fewer items than `limit` -- even none -- with "
                    "`next_cursor` still set, and that is not the end of the results: read until `next_cursor` "
                    "is null. It is empty when every filter in play was applied by the query itself, and a "
                    "short page then does mean the last one.")


class LineResponse(EntityScoped):
    line: Line
    result: dict[str, Any] | None = Field(default=None,
                                          description="what the write did; absent when nothing was written")


# Reconciliation (bank against books)
class ReconciliationGap(Strict):
    start: str
    end: str


class ReconciliationAccount(Strict):
    """One cash account and commodity at one date, from both sides.

    ``statement_micros`` is the loaded statements' own arithmetic: the earliest declared opening plus every line
    up to ``as_of``.  ``asserted_micros`` is a closing balance the bank actually printed for that exact date,
    when there is one, reported beside the derived figure rather than instead of it.  ``null`` anywhere means
    "not knowable here", never zero.

    ``agrees`` and ``explained`` answer different questions.  A statement can balance to the penny with every
    line unclassified, so the count of unexplained lines is reported beside the difference, not folded into it.
    """

    account_id: str
    account_path: str
    commodity: str
    as_of: str
    statement_micros: str | None
    ledger_micros: str | None
    asserted_micros: str | None
    difference_micros: str | None = Field(description="ledger minus statement; positive means the books hold more")
    agrees: bool
    statement_ids: list[str]
    line_count: int
    unexplained_lines: int
    unexplained_micros: str | None
    explained: bool = Field(description="every loaded line through as_of has a live explanation")
    uncovered: list[ReconciliationGap] = Field(description="days of the period no loaded statement covers")


class Reconciliation(EntityScoped):
    as_of: str
    start: str
    agrees: bool
    explained: bool
    accounts: list[ReconciliationAccount]
    links: dict[str, str]


class EntryReversalSubmission(Strict):
    """Disagree with a posted entry: an exact mirror is posted beside it, and both stay standing.

    ``reason`` is required and is recorded verbatim on the append-only audit action.  A year later the numbers
    speak for themselves, but why somebody disagreed is not recoverable from anywhere else.
    """

    reason: str = Field(min_length=1, description="why this booking is wrong; recorded verbatim")
    dt: str | None = Field(default=None, description="the mirror's date; defaults to the original's")
    narration: str | None = Field(default=None, description="the mirror's narration; defaults to naming the original")


class EntryReversal(EntityScoped):
    correction_id: str
    original_id: str
    mirror_id: str
    replacement_id: str | None
    audit_seq: int = Field(description="the entry.reversed audit action's event sequence")
    entry: Entry = Field(description="the mirror that was posted")
    links: dict[str, str]


# Manual journal entries
class EntryLeg(Strict):
    """One leg of a manual journal entry: an account, a currency and a signed amount.

    Unlike a line booking's legs, these are the whole entry.  No cash leg is supplied for you, because there is
    no statement line behind this entry.  Two legs or more, netting to zero per commodity.
    """

    account: str = Field(min_length=1, max_length=255,
                         description="account path (preferred) or stable account id")
    commodity: str = Field(min_length=1, max_length=64)
    amount: str = Field(description="signed decimal amount in whole commodity units; never a JSON number",
                        examples=["-12.00"])
    memo: str | None = Field(default=None, max_length=4096)
    tax_code: str | None = Field(default=None, max_length=255)


class JournalEntrySubmission(Strict):
    """Write one balanced entry directly into the books: an accrual, a reclassification, an adjustment.

    This is how the books record what a bank statement does not: an invoice raised, a payroll accrued, a
    quarter's withholding moved from one liability to the next.  Without it a ledger can only record cash, and
    a period's profit is not a cash-flow figure.

    The entry is posted when it is written; there is no approval step.  A posted entry is never edited.  It is
    corrected with ``POST /entries/{id}/correct`` (a mirror that cancels it plus a replacement, in one
    transaction) or withdrawn with ``POST /entries/{id}/reverse``.

    ``reason`` is required and is kept verbatim on the append-only audit action.  ``narration`` is what the entry
    reads as in every report and export; ``reason`` is why it was written.
    """

    legs: list[EntryLeg] = Field(min_length=2, description="every leg of the entry; they net to zero per commodity")
    dt: str = Field(description="the entry date, YYYY-MM-DD; a date in a closed month is refused")
    narration: str = Field(min_length=1, max_length=4096, description="what this entry reads as in the books")
    reason: str = Field(min_length=1, description="why it is being written; kept in the record")
    origin: str | None = Field(default=None,
                               description="entries.origin; defaults to 'manual'. 'invoice', 'payroll' and "
                                           "'adjustment' are the other ordinary ones here")
    code: str | None = Field(default=None, max_length=255, description="hledger tcode, e.g. a document number")
    comment: str | None = Field(default=None, max_length=4096, description="hledger tcomment, kept verbatim")
    tags: list[list[str]] | None = Field(default=None, description="[name, value] pairs, in comment order")
    id: str | None = Field(default=None, description="caller-supplied entry id; generated when omitted")


class JournalEntryResponse(EntityScoped):
    entry: Entry
    version: int
    audit_seq: int = Field(description="the entry.posted audit action's event sequence; see /entries/{id}/events")
    links: dict[str, str]


# Batches: N entries applied whole or refused whole
class BatchEntry(Strict):
    """One entry of a batch, in the same shape ``POST /entries`` takes.

    The only difference is that ``reason`` is optional, because the batch has one and repeating it on every entry
    of a converted year would be a thousand copies of one sentence.  An entry with something more specific to
    say carries its own, recorded verbatim on that entry's audit action.
    """

    legs: list[EntryLeg] = Field(min_length=2, description="every leg of the entry; they net to zero per commodity")
    dt: str = Field(description="the entry date, YYYY-MM-DD")
    narration: str = Field(min_length=1, max_length=4096, description="what this entry reads as in the books")
    reason: str | None = Field(default=None,
                               description="why this entry in particular was written; defaults to the batch's")
    origin: str | None = Field(default=None, description="entries.origin; defaults to 'manual'")
    code: str = Field(default="", max_length=255, description="hledger tcode, e.g. a document number")
    comment: str = Field(default="", max_length=4096, description="hledger tcomment, kept verbatim")
    tags: list[list[str]] | None = Field(default=None, description="[name, value] pairs, in comment order")
    id: str | None = Field(default=None, description="caller-supplied entry id; generated when omitted")


class BatchTotal(Strict):
    """What the caller says one commodity comes to across the whole batch, as two magnitudes in micros.

    Both are non-negative: ``debits_micros`` is the sum of every positive posting of this commodity and
    ``credits_micros`` of every negative one.  Each entry balances, so the two must be equal once the batch is
    built, and that is why both are asked for.  The caller computes them from its own source, so a figure
    transposed on one side of the conversion disagrees with the other and the batch is refused.
    """

    commodity: str = Field(min_length=1, max_length=64)
    debits_micros: str = Field(description="sum of the positive postings of this commodity, in micros",
                               pattern=MICROS_PATTERN, examples=["1200000"])
    credits_micros: str = Field(description="sum of the negative postings of this commodity, as a magnitude",
                                pattern=MICROS_PATTERN, examples=["1200000"])


class BatchAssertion(Strict):
    """What the caller says this batch contains: the caller's claim, not the ledger's derivation.

    The ledger never sees the journal, spreadsheet or export the entries were converted from (``decisions.md``,
    "Chunk F1 -- the generic statement input"), so it cannot know how many entries there should be.  The caller
    counted them at the source and states it here, and the ledger refuses a batch that does not match.
    """

    count: int = Field(ge=1, description="how many entries this batch contains")
    totals: list[BatchTotal] = Field(min_length=1,
                                     description="every commodity the batch touches, each named exactly once")


class BatchSubmission(Strict):
    """N entries, posted as one unit that is applied whole or refused whole.

    This is the write for a conversion: a year of entries an agent has converted from a journal, a spreadsheet or
    another system's export.  A loop of single ``POST /entries`` calls posts the same rows but proves nothing
    about the set.  A dropped entry, a retry that posted twice, or a connection lost two thirds of the way
    through each leave books that are internally consistent and simply wrong.  A batch carries the caller's own
    count and per-commodity totals, and either matches them or leaves nothing behind.
    """

    reason: str = Field(min_length=1, description="why this batch is being loaded; kept in the record")
    entries: list[BatchEntry] = Field(min_length=1, description="the entries, in the order they are to be posted")
    asserted: BatchAssertion


class BatchResponse(EntityScoped):
    operation_id: str = Field(description="the one operations row this batch was recorded as")
    count: int
    entry_ids: list[str] = Field(description="the entries that were posted, in submission order")
    totals: list[BatchTotal] = Field(description="what was actually posted; equal to the assertion, or this "
                                                 "would have been refused")
    audit_seq: int = Field(description="the batch.posted audit action's event sequence")
    links: dict[str, str]

class EntryReplacement(Strict):
    """What the corrected entry should have said: the same shape as writing one, minus the reason.

    There is one reason for the whole correction, and it belongs to the correction, not the replacement.  A
    separate reason on the replacement would invite two different sentences about one act.
    """

    legs: list[EntryLeg] = Field(min_length=2, description="every leg of the replacement; nets to zero per commodity")
    dt: str = Field(description="the replacement's date, YYYY-MM-DD")
    narration: str = Field(min_length=1, max_length=4096)
    origin: str | None = Field(default=None, description="entries.origin; defaults to 'manual'")
    code: str = Field(default="", max_length=255, description="hledger tcode")
    comment: str = Field(default="", max_length=4096)
    tags: list[list[str]] | None = Field(default=None, description="[name, value] pairs, in comment order")


class EntryCorrectionSubmission(Strict):
    """Reverse a posted entry and post what it should have said, in one transaction.

    A reversal says only "this was wrong".  A correction says what was meant, which is the ordinary case: an
    entry booked to the wrong account, for the wrong amount or on the wrong date is restated, not just
    withdrawn.  Doing both in one command means the books never pass through a state, visible to a concurrent
    report, where the original is reversed and nothing stands in its place.

    Nothing is edited.  The original stays posted and in the sums, an exact mirror is posted beside it, and the
    replacement is posted as its own entry.  All three carry the actor that wrote them, and one
    ``entry.corrected`` audit action records why.
    """

    reason: str = Field(min_length=1, description="why the original was wrong; recorded verbatim")
    replacement: EntryReplacement
    dt: str | None = Field(default=None, description="the mirror's date; defaults to the original's")
    narration: str | None = Field(default=None,
                                  description="the mirror's narration; defaults to naming the original")


class EntryCorrection(EntityScoped):
    correction_id: str
    original_id: str
    mirror_id: str
    replacement_id: str | None
    audit_seq: int = Field(description="the entry.corrected audit action's event sequence")
    entry: Entry = Field(description="the mirror that was posted")
    replacement: Entry = Field(description="the entry that now stands in the original's place")
    links: dict[str, str]


# Documents that are not bank statements (decisions.md)
class DocumentSubmission(Strict):
    """A document to register: the bytes' hash, what the file is called, and where it is kept.

    The ledger records this and does not fetch it, the same contract as ``POST /statements``.  Identity is
    ``sha256`` alone, so registering the same file twice returns the existing row rather than creating a second
    one or refusing.
    """

    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$",
                        description="SHA-256 of the document, 64 lowercase hex characters; its identity here")
    filename: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0, description="size of the document in bytes")
    blob_path: str = Field(default="", max_length=2048,
                           description="where the bytes are kept, when this service keeps them")
    external_ref: str | None = Field(default=None, max_length=2048,
                                     description="a reference to bytes held elsewhere, when we hold none")
    reason: str = Field(min_length=1, description="why it is being registered; kept in the record")


class DocumentFiling(Strict):
    """One place a document is cited: which record, where in the file, and what it says."""

    evidence_id: str
    subject_kind: str = Field(description="entry | statement")
    subject_id: str | None
    page: int | None
    locator: str
    caption: str
    created_at: str
    created_by: str


class Document(DocumentView):
    """A registered document, read back.

    ``links.content`` is present only when the bytes are here to be served.  An ``external`` document has
    nothing at that address, and the link would only give the interface a button that always fails.
    """

    id: str
    filing_count: int = Field(description="how many records cite this document")
    created_at: str
    created_by: str
    links: dict[str, str]


class DocumentResponse(EntityScoped):
    document: Document
    created: bool = Field(description="false when this call found the document already registered")
    filings: list[DocumentFiling]


class DocumentPage(EntityScoped):
    items: list[Document]
    next_cursor: str | None


class DocumentFilingSubmission(Strict):
    """File an already-registered document against an entry.

    ``locator`` is where in the document the relevant part is; ``caption`` is what it is.  A closed period does
    not refuse this: an evidence link has no amount and no date of its own, so filing a late invoice against a
    closed month's entry changes nothing that month reports.
    """

    document_id: str = Field(min_length=1, max_length=255, description="a document from POST /documents")
    locator: str = Field(min_length=1, max_length=512,
                         description="where in the document the relevant part is")
    caption: str = Field(min_length=1, max_length=1024, description="what this document is, in a sentence")
    page: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, description="why it belongs on this entry; kept in the record")


class EntryFiling(Strict):
    id: str
    entry_id: str
    document_id: str
    page: int | None
    locator: str
    caption: str
    document: dict[str, Any]
    created_at: str
    created_by: str
    met_expectation_id: str | None = Field(
        default=None, description="the provisional booking this filing settled, when the entry was awaiting one")


class EntryFilingResponse(EntityScoped):
    filing: EntryFiling
    links: dict[str, str]


# Booked, awaiting a document (decisions.md)
class ExpectationSubmission(Strict):
    """Record that a booking is provisional: it stands, and a named document is expected that may change it.

    ``awaiting`` is required because what is expected is the whole content of the state: "this is provisional"
    without saying what would settle it helps nobody.  Submitting again for an entry already awaiting something
    restates the expectation rather than opening a second one.
    """

    awaiting: str = Field(min_length=1, max_length=1024,
                          description="the document that would settle this, in a phrase")
    reason: str = Field(min_length=1, description="why it was booked this way meanwhile; kept in the record")


class WithdrawalSubmission(Strict):
    """No document is coming.  The booking stands as it is, and the reason says why it is no longer provisional.

    This is deliberately a different act from the document arriving.  A receipt that will never be reissued is a
    different fact about the books from an invoice that turned up, and the record keeps them apart.
    """

    reason: str = Field(min_length=1, description="why no document is expected any more")


class Expectation(Strict):
    id: str
    entry_id: str
    state: str = Field(description="awaiting | met | withdrawn")
    awaiting: str
    reason: str
    evidence_id: str | None = Field(default=None, description="the filed document that met it, when it was met")
    supersedes_id: str | None
    created_at: str
    created_by: str


class ExpectationResponse(EntityScoped):
    expectation: Expectation
    history: list[Expectation] = Field(description="every record about this entry, oldest first")
    links: dict[str, str]


class ProvisionalItem(Strict):
    """One provisional booking: the entry as it stands, and what it is waiting for."""

    expectation: Expectation
    entry: Entry
    links: dict[str, str]


class ProvisionalPage(EntityScoped):
    items: list[ProvisionalItem]
    next_cursor: str | None


# Recoverable tax that was not recovered (decisions.md)
class UnrecoveredSubmission(Strict):
    """An amount of recoverable tax that was not recovered on this entry, and why.

    Nothing about a country or a tax is modelled.  ``recoverable_until`` is a date the caller supplies; the
    ledger applies no regime rule.  It is what makes "what can we still reclaim, and by when" answerable.
    ``tax_code`` is the entity's own code, the same column name the chart and the postings use.
    """

    commodity: str = Field(min_length=1, max_length=64)
    amount_micros: str = MICROS
    reason: str = Field(min_length=1, description="why it was not recovered; the whole point of the record")
    posting_id: str | None = Field(default=None, max_length=255,
                                   description="the leg the amount is sitting inside, when it is known")
    tax_code: str | None = Field(default=None, max_length=255, description="which tax, in the entity's own code")
    recoverable_until: str | None = Field(
        default=None, description="the last date it could still be recovered (YYYY-MM-DD), when a rule sets one")


class UnrecoveredResolution(Strict):
    """Close a record.

    ``recovered`` names the entry that recovered it.  ``forgone`` names nothing, so its reason has to explain it
    (the window closed, or the claim was not worth making)."""

    outcome: str = Field(description="recovered | forgone")
    reason: str = Field(min_length=1)
    recovered_entry_id: str | None = Field(
        default=None, max_length=255,
        description="required when recovered; a different entry, because a recovery is its own event")


class UnrecoveredRecord(Strict):
    id: str
    entry_id: str
    posting_id: str | None
    commodity: str
    amount_micros: str = MICROS
    state: str = Field(description="unrecovered | recovered | forgone")
    tax_code: str | None
    reason: str
    recoverable_until: str | None
    recovered_entry_id: str | None
    supersedes_id: str | None
    created_at: str
    created_by: str


class UnrecoveredResponse(EntityScoped):
    record: UnrecoveredRecord
    history: list[UnrecoveredRecord] = Field(description="every record about this entry, oldest first")
    links: dict[str, str]


class UnrecoveredItem(Strict):
    record: UnrecoveredRecord
    entry: Entry
    links: dict[str, str]


class UnrecoveredPage(EntityScoped):
    items: list[UnrecoveredItem]
    next_cursor: str | None
    state: str
    totals: dict[str, str] = Field(description="per commodity, never summed across them")
