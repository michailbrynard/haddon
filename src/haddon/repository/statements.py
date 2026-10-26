"""The reconciliation extension's input half: a document, the statement printed on it, and that statement's lines.

There is no file reader here, by design.  Haddon is a general bookkeeping ledger, so it holds no bank-specific or
company-specific code: no CSV/XLSX/PDF parsing, no format detection, no per-institution column maps.  An agent
reads whatever the bank produced outside the ledger and submits normalised lines through one shape.  The ledger
owns the part a parser must not be trusted with: strict validation, refusal, and a provenance chain back to the
original document.

A submission that breaks any of these four rules is refused whole, and nothing partial is stored:

1. the lines sum to closing minus opening (``UNBALANCED``).  This arithmetic is what makes a statement a statement
   rather than a list of transactions.  It is the one check that catches a dropped, duplicated or misparsed line
   without knowing anything about the format it came from;
2. every line's date falls inside the declared period (``validation``).  The ``statement_lines_insert`` trigger
   enforces it underneath as well, and this is the clean, named refusal in front of it;
3. the currency is declared, on the statement and on every line, and they must agree.  Nothing is inferred.  A
   line with no currency is refused rather than silently taking the statement's;
4. the same document cannot be loaded twice (``DOCUMENT_ALREADY_LOADED``).  Identity is the document's sha256
   (``documents.sha256`` is UNIQUE), so a renamed re-download is still the same statement.  This means loaded once
   at a time, not loaded ever.  A document whose statement has been replaced is free again, and a replacement may
   restate the very document it corrects.  So "the parse was wrong, here is the same download read properly" is an
   ordinary command and does not need different bytes.

Balance and reconciliation are different questions (issue 0004, pitfall 2).  Rule 1 says the statement is
internally consistent.  It says nothing about whether the journal explains it.  That belongs to
``repository/lines.py`` and ``repository/reconcile.py``, and a statement that balances perfectly can still have
every one of its lines unexplained.

Provenance is the point (pitfall 1).  A machine did fuzzy work to produce these lines, so the audit trail has to
reach the actual file.  The ``documents`` row keeps the sha256, the media type, the size, and either the path to
the bytes or an external reference to them.  Every ``statement_lines`` row keeps its position in that document,
its stable source key, a content hash and the ``raw`` text it was read from.

That provenance is a claim, not a verification.  Nothing here opens the file.  The hash, the size and the location
are recorded exactly as submitted, and the record says "the agent told us these bytes hashed to this".  Overwrite
the file at ``blob_path`` afterwards and the ledger will not notice, because it never looks.  Reading the file
would put format handling back in the ledger, and could never cover ``external_ref`` at all.  This is recorded as
a known limitation, with what would force it to change, in ``docs/design/decisions.md`` ("Ingestion records what
it is told about the bytes, and does not read them").

Replacing a mistaken statement is an ordinary command, not a repair job.  ``replace_statement`` writes a new
statement carrying ``replaces_id``, and the predecessor stays exactly as it was and stays readable.  The database
guarantees there is at most one successor (``statements.replaces_id`` is UNIQUE) and that a replacement into a
locked period fails closed.  A predecessor whose lines are still booked is refused.  Undo those first, so a
replacement never silently orphans an entry it caused.

The opening balance is stored.  Statements chain, one period's opening being the previous period's closing, so the
declared opening is checked against the prior live statement when one ends the day before, and a disagreement is a
refusal.  When there is no such statement the opening cannot be verified from the record, and the statement says so
(``opening_verified: false`` plus an ``opening_basis`` naming which case it is).  It is never defaulted to zero.
An unverifiable opening that silently becomes 0 is a wrong balance sheet with no trace.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.canonical import canonical_json, sha256_hex
from haddon.domain.dates import parse_date
from haddon.domain.errors import (
    ACCOUNT_UNKNOWN,
    ALREADY_SUPERSEDED,
    DOCUMENT_ALREADY_LOADED,
    IMMUTABLE,
    INTERNAL_ERROR,
    PERIOD_LOCKED,
    PERIOD_OVERLAP,
    UNBALANCED,
    HaddonError,
    not_found,
    validation,
)
from haddon.domain.micros import Micros, sum_micros
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.entries import lock_through
from haddon.repository.provisioning import all_commodities, allowed_commodities, get_account

#: A submission larger than this is implausible as one statement and is refused before anything is written.
MAX_LINES = 10_000

#: The three ways an opening balance can stand.  Only the first is verified against the record.
OPENING_PRIOR = "prior_statement_closing"
OPENING_FIRST = "first_statement_for_this_account_and_commodity"
OPENING_GAP = "no_statement_ends_the_day_before_this_period"


def _prev_day(value: str) -> str:
    return (date.fromisoformat(value) - timedelta(days=1)).isoformat()


@dataclass(frozen=True)
class Document:
    """The original file: its hash, its name, its media type and where the bytes are.

    The ledger records this and does not fetch it.  ``sha256`` is the agent's claim about the bytes it read, and
    the uniqueness key of rule 4.  Exactly one of ``blob_path`` (bytes we hold) and ``external_ref`` (bytes
    somebody else holds, named by URI or document-management id) must be given, so "where is the original?"
    always has one answer.
    """

    sha256: str
    filename: str
    media_type: str
    size_bytes: int
    blob_path: str = ""
    external_ref: str | None = None

    @property
    def storage(self) -> str:
        """``stored`` when this service holds the bytes, ``external`` when somebody else does.

        This is what responses carry instead of ``blob_path`` (issue 0007).  A reviewer needs to know whether the
        document can be opened from here.  Where it sits on a server's disk is no use to them, and not something a
        public page should be told.
        """
        return "external" if self.external_ref else "stored"

    def as_json(self) -> dict[str, Any]:
        """The document as recorded, including where the bytes are kept.  For the audit trail only.

        The audit log records what a caller claimed, verbatim, so ``blob_path`` belongs in it.  It does not belong
        in a response, which is what ``as_view`` is for.  When the two were one method, the path went out on every
        read of a statement, a filing and a provenance trail.
        """
        return {"sha256": self.sha256, "filename": self.filename, "media_type": self.media_type,
                "size_bytes": self.size_bytes, "blob_path": self.blob_path, "external_ref": self.external_ref}

    def as_view(self) -> dict[str, Any]:
        """The document as a caller sees it: everything identifying, and no filesystem path."""
        return {"sha256": self.sha256, "filename": self.filename, "media_type": self.media_type,
                "size_bytes": self.size_bytes, "storage": self.storage, "external_ref": self.external_ref}


@dataclass(frozen=True)
class Line:
    """One normalised statement line as the agent read it off the document.

    ``currency`` is required (rule 3) and must equal the statement's commodity.  ``raw`` is the line as it appeared
    in the file, which is what a human compares against.  When the agent cannot supply it, it defaults to the
    canonical JSON of the normalised line.  ``source_key`` is the line's own stable identity in the document,
    usually the bank's reference.  It defaults to the reference, then to a digest of what the line says and where.
    """

    dt: str
    description: str
    amount_micros: str | Micros
    currency: str
    reference: str | None = None
    raw: str | None = None
    source_key: str | None = None


@dataclass(frozen=True)
class StoredLine:
    id: str
    statement_id: str
    line_no: int
    source_key: str
    content_hash: str
    dt: str
    amount_micros: int
    description: str
    reference: str | None
    currency: str
    raw: str

    def as_json(self) -> dict[str, Any]:
        return {"line_id": self.id, "statement_id": self.statement_id, "line_no": self.line_no,
                "source_key": self.source_key, "content_hash": self.content_hash, "dt": self.dt,
                "amount_micros": str(self.amount_micros), "description": self.description,
                "reference": self.reference, "currency": self.currency, "raw": self.raw}


@dataclass(frozen=True)
class Statement:
    """A loaded statement: its subject, its period, its balances, its document and its replacement chain."""

    id: str
    account_id: str
    commodity: str
    period_start: str
    period_end: str
    opening_micros: int
    closing_micros: int
    opening_verified: bool
    opening_basis: str
    prior_statement_id: str | None
    line_count: int
    lines_micros: int
    document_id: str
    document: Document
    page: int | None
    locator: str
    reason: str
    replaces_id: str | None
    replaced_by_id: str | None
    created_at: str
    created_by: str

    @property
    def live(self) -> bool:
        return self.replaced_by_id is None

    def as_json(self) -> dict[str, Any]:
        return {"statement_id": self.id, "account_id": self.account_id, "commodity": self.commodity,
                "period_start": self.period_start, "period_end": self.period_end,
                "opening_micros": str(self.opening_micros), "closing_micros": str(self.closing_micros),
                "opening_verified": self.opening_verified, "opening_basis": self.opening_basis,
                "prior_statement_id": self.prior_statement_id, "line_count": self.line_count,
                "lines_micros": str(self.lines_micros), "document_id": self.document_id,
                "document": self.document.as_view(), "page": self.page, "locator": self.locator,
                "reason": self.reason, "replaces_id": self.replaces_id, "replaced_by_id": self.replaced_by_id,
                "live": self.live, "created_at": self.created_at, "created_by": self.created_by}


def _text(value: object, path: str, *, max_len: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise validation(f"{path} is required", path=path, reason="required")
    if len(value) > max_len:
        raise validation(f"{path} is longer than {max_len} characters", path=path, reason="too_long",
                         length=len(value), maximum=max_len)
    return value


def _optional_text(value: object, path: str, *, max_len: int = 4096) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise validation(f"{path} must be a string", path=path, reason="string_required")
    if len(value) > max_len:
        raise validation(f"{path} is longer than {max_len} characters", path=path, reason="too_long",
                         length=len(value), maximum=max_len)
    return value


def check_page(page: object) -> int | None:
    if page is None:
        return None
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise validation("page must be a positive integer", path="page", reason="page_invalid")
    return page


_HEX = frozenset("0123456789abcdef")


def check_document(document: object) -> Document:
    """Validate a document reference: a lowercase 64-hex sha256, a name, a media type, a size and one location."""
    if not isinstance(document, Document):
        raise validation("document must be a Document", path="document", reason="document_shape")
    digest = _text(document.sha256, "document.sha256", max_len=64)
    if len(digest) != 64 or not set(digest) <= _HEX:
        raise validation("document.sha256 must be 64 lowercase hexadecimal characters", path="document.sha256",
                         reason="sha256_shape", value=digest)
    size = document.size_bytes
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise validation("document.size_bytes must be a non-negative integer", path="document.size_bytes",
                         reason="size_invalid", value=size if isinstance(size, (int, str)) else None)
    blob_path = _optional_text(document.blob_path, "document.blob_path", max_len=2048) or ""
    external_ref = _optional_text(document.external_ref, "document.external_ref", max_len=2048)
    if bool(blob_path.strip()) == bool(external_ref and external_ref.strip()):
        raise validation("a document has either stored bytes (blob_path) or an external reference "
                         "(external_ref), and exactly one of them", path="document.blob_path",
                         reason="one_location_required")
    return Document(sha256=digest, filename=_text(document.filename, "document.filename", max_len=512),
                    media_type=_text(document.media_type, "document.media_type", max_len=255), size_bytes=size,
                    blob_path=blob_path, external_ref=external_ref)


def _check_period(ctx: WriteCtx, period_start: str, period_end: str) -> None:
    parse_date(period_start, path="period_start")
    parse_date(period_end, path="period_end")
    if period_start > period_end:
        raise validation("period_start must not be after period_end", path="period_start", reason="period_inverted",
                         period_start=period_start, period_end=period_end)
    through = lock_through(ctx.conn)
    if through is not None and period_start <= through:
        raise HaddonError(PERIOD_LOCKED, f"{period_start} is on or before the period lock {through}",
                          details=[{"path": "period_start", "dt": period_start, "locked_through": through}])


def _check_subject(ctx: WriteCtx, account_id: object, commodity: object) -> tuple[str, str]:
    """Check that the account and the commodity exist and are bound to each other.

    The binding matters beyond referential integrity.  Close eligibility enumerates the (account, commodity) pairs
    of ``account_commodities``, so a statement loaded against an unbound pair would be invisible to the gate it is
    supposed to satisfy.
    """
    account_id = _text(account_id, "account_id", max_len=255)
    commodity = _text(commodity, "commodity", max_len=64)
    if get_account(ctx.conn, ctx.entity_id, account_id) is None:
        raise HaddonError(ACCOUNT_UNKNOWN, f"unknown account {account_id!r}",
                          details=[{"path": "account_id", "id": account_id}])
    if commodity not in all_commodities(ctx.conn, ctx.entity_id):
        raise validation(f"unknown commodity {commodity!r}", path="commodity", reason="commodity_unknown",
                         value=commodity)
    if (account_id, commodity) not in allowed_commodities(ctx.conn, ctx.entity_id):
        raise validation(f"{commodity} is not a commodity of account {account_id}", path="commodity",
                         reason="commodity_not_allowed", account_id=account_id, commodity=commodity)
    return account_id, commodity


def check_lines(ctx: WriteCtx, lines: object, *, commodity: str, period_start: str, period_end: str,
                document: Document) -> tuple[list[StoredLine], Micros]:
    """Apply rules 2 and 3 to every line, plus shape, precision and the derived line identity.  Returns the sum."""
    if not isinstance(lines, (list, tuple)):
        raise validation("lines must be a list", path="lines", reason="list_required")
    if len(lines) > MAX_LINES:
        raise validation(f"a statement carries at most {MAX_LINES} lines", path="lines", reason="too_many",
                         count=len(lines), maximum=MAX_LINES)
    com = all_commodities(ctx.conn, ctx.entity_id)[commodity]
    out: list[StoredLine] = []
    seen: dict[str, int] = {}
    for i, line in enumerate(lines):
        path = f"lines[{i}]"
        if not isinstance(line, Line):
            raise validation(f"{path} is not a Line", path=path, reason="line_shape")
        dt = parse_date(line.dt, path=f"{path}.dt")
        # Rule 2.  The declared period is the claim, so a line outside it means the period or the parse is wrong.
        if not period_start <= dt <= period_end:
            raise validation(f"{path}.dt {dt} falls outside the declared period {period_start}..{period_end}",
                             path=f"{path}.dt", reason="line_outside_period", dt=dt,
                             period_start=period_start, period_end=period_end)
        # Rule 3.  Declared, never inferred: a missing currency is a refusal, not a default.
        currency = _text(line.currency, f"{path}.currency", max_len=64)
        if currency != commodity:
            raise validation(f"{path}.currency {currency!r} is not the statement commodity {commodity!r}",
                             path=f"{path}.currency", reason="currency_mismatch", value=currency,
                             commodity=commodity)
        description = _text(line.description, f"{path}.description")
        reference = _optional_text(line.reference, f"{path}.reference", max_len=512)
        raw = _optional_text(line.raw, f"{path}.raw", max_len=8192)
        amount = line.amount_micros if isinstance(line.amount_micros, Micros) else \
            Micros.parse(line.amount_micros, path=f"{path}.amount_micros")
        amount.check_quantum(com, path=f"{path}.amount_micros")
        if amount.is_zero:
            raise validation(f"{path}.amount_micros is zero: a statement line moves money",
                             path=f"{path}.amount_micros", reason="amount_zero")
        line_no = i + 1
        fields = {"description": description, "reference": reference, "currency": currency}
        content_hash = sha256_hex(canonical_json({"document": document.sha256, "line_no": line_no, "dt": dt,
                                                  "amount_micros": amount.render(), **fields}))
        # The line's identity inside its document: what the caller says it is, else the bank's own reference,
        # else a digest of what the line says and where it says it.  The position matters.  Two identical coffees
        # on one day are two lines, not one line loaded twice, and a key from the content alone would refuse the
        # second.  Keys are scoped to the statement, so a replacement may restate every key.
        source_key = _optional_text(line.source_key, f"{path}.source_key", max_len=512) \
            or reference or sha256_hex(canonical_json({"line_no": line_no, "dt": dt,
                                                       "amount_micros": amount.render(),
                                                       "description": description}))
        if source_key in seen:
            raise validation(f"{path}.source_key {source_key!r} is already used by line {seen[source_key]}; a "
                             f"line's key identifies it within its statement",
                             path=f"{path}.source_key", reason="duplicate_source_key", value=source_key)
        seen[source_key] = line_no
        out.append(StoredLine(id="", statement_id="", line_no=line_no, source_key=source_key,
                              content_hash=content_hash, dt=dt, amount_micros=amount.value,
                              description=description, reference=reference, currency=currency,
                              raw=raw if raw is not None else canonical_json(
                                  {"dt": dt, "amount_micros": amount.render(), **fields})))
    return out, sum_micros([ln.amount_micros for ln in out], path="lines")


@dataclass(frozen=True)
class Opening:
    micros: Micros
    verified: bool
    basis: str
    prior_statement_id: str | None
    prior_end_dt: str | None

    def as_json(self) -> dict[str, Any]:
        return {"opening_micros": self.micros.render(), "opening_verified": self.verified,
                "opening_basis": self.basis, "prior_statement_id": self.prior_statement_id,
                "prior_end_dt": self.prior_end_dt}


def _live_clause(alias=None):
    """The SQL form of "live" (no statement replaces this one), defined once in ``tables``."""
    return t.live_statement_clause(alias)


def successor_of(conn: Connection, entity_id: str, statement_id: str) -> str | None:
    """Return the id of the statement that replaced this one, if any.

    ``live_statement_clause`` answers the same question as a filter over a set.  This answers it for one statement
    and returns the successor's id, because some callers need to name the replacement in a refusal rather than
    merely exclude it.  Both derive the same fact from the same column, and
    ``tests/test_one_definition_of_live.py`` fails if another spelling appears.
    """
    row = conn.execute(sa.select(t.statements.c.id)
                       .where(t.statements.c.entity_id == entity_id,
                              t.statements.c.replaces_id == statement_id)).first()
    return None if row is None else row.id


def is_live(conn: Connection, entity_id: str, statement_id: str) -> bool:
    """A statement nothing replaces."""
    return successor_of(conn, entity_id, statement_id) is None


def live_statements(conn: Connection, entity_id: str, account_id: str, commodity: str):
    return conn.execute(
        sa.select(t.statements.c.id, t.statements.c.period_start, t.statements.c.period_end,
                  t.statements.c.opening_micros, t.statements.c.closing_micros)
        .where(t.statements.c.entity_id == entity_id, t.statements.c.account_id == account_id,
               t.statements.c.commodity == commodity, _live_clause())
        .order_by(t.statements.c.period_end.desc(), t.statements.c.id)).all()


def resolve_opening(ctx: WriteCtx, *, account_id: str, commodity: str, period_start: str,
                    declared: Micros, ignoring: str | None = None) -> Opening:
    """Check the declared opening against the prior period's closing, and say plainly when it cannot be checked.

    There are three outcomes, all explicit and none of them zero:

    * a live statement ends on the day before ``period_start``.  The declared opening must equal its closing, or
      the statement is refused.  This is the chain, and it catches a statement loaded out of order or against the
      wrong account;
    * no statement exists at all for this account and commodity, so this is the first.  Accepted, recorded, and
      flagged ``opening_verified: false``;
    * statements exist but none ends the day before, so there is a gap.  Also accepted and flagged, with the end
      date of the nearest earlier one reported so the gap is visible rather than implied.

    ``ignoring`` is the statement being replaced.  It is still live at this point in the transaction, and a
    replacement must be checked against the chain around its predecessor, not against the predecessor.
    """
    rows = [r for r in live_statements(ctx.conn, ctx.entity_id, account_id, commodity) if r.id != ignoring]
    if not rows:
        return Opening(declared, False, OPENING_FIRST, None, None)
    wanted = _prev_day(period_start)
    adjacent = next((r for r in rows if r.period_end == wanted), None)
    if adjacent is None:
        earlier = next((r.period_end for r in rows if r.period_end < period_start), None)
        return Opening(declared, False, OPENING_GAP, None, earlier)
    if int(adjacent.closing_micros) != declared.value:
        raise validation(
            f"the declared opening {declared.render()} does not match the closing balance "
            f"{adjacent.closing_micros} already recorded for {account_id} {commodity} at {wanted}",
            path="opening_micros", reason="opening_does_not_match_prior_statement",
            declared_micros=declared.render(), prior_micros=str(int(adjacent.closing_micros)),
            prior_statement_id=adjacent.id, prior_end_dt=wanted)
    return Opening(declared, True, OPENING_PRIOR, adjacent.id, wanted)


def document_by_sha256(conn: Connection, entity_id: str, sha256: str):
    return conn.execute(sa.select(t.documents)
                        .where(t.documents.c.entity_id == entity_id, t.documents.c.sha256 == sha256)).first()


def document_row(conn: Connection, entity_id: str, document_id: str):
    return conn.execute(sa.select(t.documents)
                        .where(t.documents.c.entity_id == entity_id, t.documents.c.id == document_id)).first()


def document_of_row(row) -> Document:
    return Document(sha256=row.sha256, filename=row.filename, media_type=row.media_type,
                    size_bytes=int(row.size_bytes), blob_path=row.blob_path, external_ref=row.external_ref)


def store_document(ctx: WriteCtx, document: Document) -> str:
    """Insert the document, or return the id of the row that already holds these bytes.

    Storage is content-addressed.  The same file registered from two places is one row, and the stored row keeps
    the name it was first seen under.  The one contradiction refused is a size that disagrees with the stored one,
    since identical bytes cannot differ in length.
    """
    existing = document_by_sha256(ctx.conn, ctx.entity_id, document.sha256)
    if existing is not None:
        if int(existing.size_bytes) != document.size_bytes:
            raise HaddonError(IMMUTABLE,
                              f"a document with sha256 {document.sha256} is registered as "
                              f"{existing.size_bytes} bytes, not {document.size_bytes}; identical bytes cannot "
                              f"differ in length",
                              details=[{"path": "document.size_bytes", "document_id": existing.id,
                                        "sha256": document.sha256,
                                        "registered_size_bytes": int(existing.size_bytes),
                                        "submitted_size_bytes": document.size_bytes}])
        return existing.id
    return ctx.insert(t.documents, None, "doc", sha256=document.sha256, filename=document.filename,
                      media_type=document.media_type, size_bytes=document.size_bytes,
                      blob_path=document.blob_path, external_ref=document.external_ref)


def _refuse_reload(ctx: WriteCtx, document: Document, *, replacing: str | None = None) -> None:
    """Apply rule 4.  Identity is the bytes, so the refusal names the statement the document is already loaded as.

    Loaded once at a time, not loaded ever.  A document whose statement has been replaced is free again, and the
    statement being replaced right now does not count against its own replacement.  That last part matters most.
    Without it the commonest correction there is, "the parse was wrong, here is the same download read
    properly", would need different bytes or a faked hash.  ``statements`` carries a ``document_already_loaded``
    trigger underneath saying the same thing (decisions.md D6).
    """
    existing = document_by_sha256(ctx.conn, ctx.entity_id, document.sha256)
    if existing is None:
        return
    loaded = [r.id for r in ctx.conn.execute(
        sa.select(t.statements.c.id).where(t.statements.c.entity_id == ctx.entity_id,
                                           t.statements.c.document_id == existing.id,
                                           t.statements.c.id != (replacing or ""), _live_clause())
        .order_by(t.statements.c.id))]
    if not loaded:
        return  # registered as evidence, replaced, or being replaced here: none of those is a reload
    raise HaddonError(DOCUMENT_ALREADY_LOADED,
                      f"this document is already loaded (sha256 {document.sha256}); a statement is loaded once",
                      details=[{"path": "document.sha256", "sha256": document.sha256,
                                "document_id": existing.id, "filename": existing.filename,
                                "statement_ids": loaded}])


def _refuse_overlap(ctx: WriteCtx, *, account_id: str, commodity: str, period_start: str, period_end: str,
                    replacing: str | None) -> None:
    """One account and commodity cannot have two live statements covering the same day.

    The ``period_overlap`` trigger is the real guarantee.  This is the pre-validation in front of it (decisions.md
    D6), so the refusal names the statement that already covers the period instead of arriving as trigger text.
    """
    rows = [r for r in live_statements(ctx.conn, ctx.entity_id, account_id, commodity)
            if r.period_start <= period_end and r.period_end >= period_start and r.id != replacing]
    if rows:
        raise HaddonError(PERIOD_OVERLAP,
                          f"a statement is already loaded for {account_id} {commodity} over a day of "
                          f"{period_start}..{period_end}",
                          details=[{"path": "period_start", "statement_id": r.id, "period_start": r.period_start,
                                    "period_end": r.period_end} for r in rows])


def _refuse_locked_allocations(ctx: WriteCtx, replacing: str, through: str | None) -> None:
    """Refuse a replacement that would release allocations against postings in a closed period.

    ``live_explanations`` requires a live statement, so once a replacement lands the predecessor's allocations stop
    consuming their postings.  ``_check_replaced`` checks the predecessor's own period, which says nothing about
    the dates of the postings its lines are linked to.  A March line linked to a February posting would silently
    unexplain February.  This is the rule in ``lines._check_allocations_open``, from the other end (issue 0004,
    pitfall 3).
    """
    if through is None:
        return
    rows = ctx.conn.execute(
        sa.select(t.postings.c.id, t.entries.c.id.label("entry_id"), t.entries.c.dt,
                  t.statement_lines.c.id.label("line_id"))
        .select_from(t.allocations
                     .join(t.statement_lines, sa.and_(t.statement_lines.c.entity_id == t.allocations.c.entity_id,
                                                      t.statement_lines.c.id == t.allocations.c.line_id))
                     .join(t.explanations, sa.and_(t.explanations.c.entity_id == t.allocations.c.entity_id,
                                                   t.explanations.c.id == t.allocations.c.explanation_id))
                     .join(t.postings, sa.and_(t.postings.c.entity_id == t.allocations.c.entity_id,
                                               t.postings.c.id == t.allocations.c.posting_id))
                     .join(t.entries, sa.and_(t.entries.c.entity_id == t.postings.c.entity_id,
                                              t.entries.c.id == t.postings.c.entry_id)))
        .where(t.allocations.c.entity_id == ctx.entity_id, t.statement_lines.c.statement_id == replacing,
               t.entries.c.dt <= through, t.live_explanation_clause(ctx.entity_id))
        .order_by(t.entries.c.dt, t.postings.c.id)).all()
    if rows:
        raise HaddonError(PERIOD_LOCKED,
                          f"statement {replacing} explains {len(rows)} posting(s) dated on or before the period "
                          f"lock {through}; replacing it would release them and leave a closed month's postings "
                          f"unexplained, so reopen the period first",
                          details=[{"path": "statement_id", "statement_id": replacing, "line_id": r.line_id,
                                    "posting_id": r.id, "entry_id": r.entry_id, "dt": r.dt,
                                    "locked_through": through, "remedy": "reopen"} for r in rows])


def _check_replaced(ctx: WriteCtx, replacing: str) -> Any:
    """The statement being replaced: it must exist, still be live, and have nothing booked against its lines."""
    row = ctx.conn.execute(sa.select(t.statements)
                           .where(t.statements.c.entity_id == ctx.entity_id,
                                  t.statements.c.id == replacing)).first()
    if row is None:
        raise not_found("statement", replacing)
    successor = successor_of(ctx.conn, ctx.entity_id, replacing)
    if successor is not None:
        raise HaddonError(ALREADY_SUPERSEDED,
                          f"statement {replacing} has already been replaced by {successor}",
                          details=[{"path": "statement_id", "statement_id": replacing,
                                    "replaced_by_id": successor}])
    through = lock_through(ctx.conn)
    if through is not None and row.period_start <= through:
        raise HaddonError(PERIOD_LOCKED,
                          f"statement {replacing} starts {row.period_start}, on or before the period lock "
                          f"{through}; reopen the period to replace it",
                          details=[{"path": "statement_id", "statement_id": replacing,
                                    "period_start": row.period_start, "locked_through": through}])
    _refuse_locked_allocations(ctx, replacing, through)
    booked = ctx.conn.execute(
        sa.select(t.statement_lines.c.id, t.explanations.c.id.label("explanation_id"),
                  t.explanations.c.entry_id)
        .select_from(t.statement_lines.join(t.explanations,
                                            sa.and_(t.explanations.c.entity_id == t.statement_lines.c.entity_id,
                                                    t.explanations.c.line_id == t.statement_lines.c.id)))
        .where(t.statement_lines.c.entity_id == ctx.entity_id,
               t.statement_lines.c.statement_id == replacing,
               t.explanations.c.kind == "booked", t.live_explanation_clause(ctx.entity_id))
        .order_by(t.statement_lines.c.line_no)).all()
    if booked:
        raise HaddonError(IMMUTABLE,
                          f"{len(booked)} line(s) of statement {replacing} are still booked; undo those "
                          f"classifications before replacing the statement they rest on",
                          details=[{"path": "statement_id", "line_id": r.id, "entry_id": r.entry_id,
                                    "explanation_id": r.explanation_id} for r in booked])
    return row


def load_statement(ctx: WriteCtx, *, account_id: str, commodity: str, period_start: str, period_end: str,
                   opening_micros: str | Micros, closing_micros: str | Micros, lines: Sequence[Line],
                   document: Document, locator: str, reason: str, page: int | None = None,
                   replaces_id: str | None = None, id: str | None = None) -> Statement:
    """Load one statement in one transaction, or refuse it whole.

    The checks run in the order refusals should reach a caller: what it asked about (account, commodity, period),
    then whether this is a replacement and whether that is allowed, then whether the document is new, then the
    numbers, then the lines, then the chain.  Nothing is written until every check has passed, so a refusal leaves
    no trace and there is no half-loaded statement.
    """
    account_id, commodity = _check_subject(ctx, account_id, commodity)
    _check_period(ctx, period_start, period_end)
    replaced = None
    if replaces_id is not None:
        replaced = _check_replaced(ctx, replaces_id)
        if (replaced.account_id, replaced.commodity) != (account_id, commodity):
            raise validation(f"statement {replaces_id} is for {replaced.account_id} {replaced.commodity}; a "
                             f"replacement restates the same subject",
                             path="account_id", reason="replaces_subject",
                             statement_id=replaces_id, account_id=replaced.account_id,
                             commodity=replaced.commodity)
    _refuse_overlap(ctx, account_id=account_id, commodity=commodity, period_start=period_start,
                    period_end=period_end, replacing=replaces_id)
    doc = check_document(document)
    _refuse_reload(ctx, doc, replacing=replaces_id)
    locator = _text(locator, "locator", max_len=512)
    reason = _text(reason, "reason")
    page = check_page(page)
    com = all_commodities(ctx.conn, ctx.entity_id)[commodity]
    opening = opening_micros if isinstance(opening_micros, Micros) else \
        Micros.parse(opening_micros, path="opening_micros")
    closing = closing_micros if isinstance(closing_micros, Micros) else \
        Micros.parse(closing_micros, path="closing_micros")
    opening.check_quantum(com, path="opening_micros")
    closing.check_quantum(com, path="closing_micros")
    stored, total = check_lines(ctx, lines, commodity=commodity, period_start=period_start,
                                period_end=period_end, document=doc)
    # Rule 1, the arithmetic that makes this a statement.  Checked before the chain so a caller with a bad parse
    # hears about the bad parse, not about its consequence for the opening.
    movement = closing - opening
    if total != movement:
        raise HaddonError(UNBALANCED,
                          f"the {len(stored)} lines sum to {total.render()}, but closing minus opening is "
                          f"{movement.render()}",
                          details=[{"path": "lines", "reason": "statement_does_not_balance",
                                    "lines_micros": total.render(), "expected_micros": movement.render(),
                                    "difference_micros": (total - movement).render(),
                                    "opening_micros": opening.render(), "closing_micros": closing.render(),
                                    "line_count": len(stored)}])
    resolved = resolve_opening(ctx, account_id=account_id, commodity=commodity, period_start=period_start,
                               declared=opening, ignoring=replaces_id)

    document_id = store_document(ctx, doc)
    statement_id = ctx.insert(t.statements, id, "stmt", account_id=account_id, commodity=commodity,
                              period_start=period_start, period_end=period_end, opening_micros=opening.value,
                              closing_micros=closing.value, document_id=document_id, page=page,
                              locator=locator, reason=reason, replaces_id=replaces_id)
    saved: list[StoredLine] = []
    for ln in stored:
        line_id = ctx.insert(t.statement_lines, None, "sl", statement_id=statement_id, line_no=ln.line_no,
                             source_key=ln.source_key, content_hash=ln.content_hash, dt=ln.dt,
                             amount_micros=ln.amount_micros, description=ln.description,
                             reference=ln.reference,
                             fields=canonical_json({"description": ln.description, "reference": ln.reference,
                                                    "currency": ln.currency}), raw=ln.raw)
        saved.append(StoredLine(line_id, statement_id, ln.line_no, ln.source_key, ln.content_hash, ln.dt,
                                ln.amount_micros, ln.description, ln.reference, ln.currency, ln.raw))
    # The evidence link is what an auditor follows from the document to the balance printed on it, with the page
    # and the locator inside it.
    ctx.insert(t.evidence_links, None, "ev", document_id=document_id, entry_id=None,
               statement_id=statement_id, page=page, locator=locator,
               caption=f"statement {doc.filename} for {account_id} {commodity} {period_start}..{period_end}, "
                       f"closing {closing.render()}")
    return Statement(id=statement_id, account_id=account_id, commodity=commodity, period_start=period_start,
                     period_end=period_end, opening_micros=opening.value, closing_micros=closing.value,
                     opening_verified=resolved.verified, opening_basis=resolved.basis,
                     prior_statement_id=resolved.prior_statement_id, line_count=len(saved),
                     lines_micros=total.value, document_id=document_id, document=doc, page=page,
                     locator=locator, reason=reason, replaces_id=replaces_id, replaced_by_id=None,
                     created_at=ctx.now, created_by=ctx.actor)


def document_of(conn: Connection, entity_id: str, document_id: str, *, context: str) -> Document:
    """The document a statement points at.

    The whole audit trail hangs from this link, so it refuses rather than degrades.  A ``document_id`` with no row
    behind it is an ``internal_error``.  A ``null`` document would read like an absence of evidence rather than a
    broken chain.
    """
    row = document_row(conn, entity_id, document_id)
    if row is None:
        raise HaddonError(INTERNAL_ERROR,
                          f"{context} points at document {document_id}, which this entity does not hold",
                          details=[{"context": context, "document_id": document_id}])
    return document_of_row(row)


def _statement_row(conn: Connection, entity_id: str, statement_id: str):
    return conn.execute(sa.select(t.statements).where(t.statements.c.entity_id == entity_id,
                                                      t.statements.c.id == statement_id)).first()


def statement_of_row(conn: Connection, entity_id: str, row) -> Statement:
    successor = successor_of(conn, entity_id, row.id)
    counted = conn.execute(
        sa.select(sa.func.count(), sa.func.coalesce(sa.func.sum(t.statement_lines.c.amount_micros), 0))
        .where(t.statement_lines.c.entity_id == entity_id,
               t.statement_lines.c.statement_id == row.id)).first()
    opening = Micros(int(row.opening_micros))
    prior = conn.execute(
        sa.select(t.statements.c.id, t.statements.c.closing_micros)
        .where(t.statements.c.entity_id == entity_id, t.statements.c.account_id == row.account_id,
               t.statements.c.commodity == row.commodity,
               t.statements.c.period_end == _prev_day(row.period_start),
               t.statements.c.id != row.id, _live_clause())).first()
    if prior is None:
        anything = conn.execute(
            sa.select(t.statements.c.id)
            .where(t.statements.c.entity_id == entity_id, t.statements.c.account_id == row.account_id,
                   t.statements.c.commodity == row.commodity, t.statements.c.id != row.id,
                   t.statements.c.period_end < row.period_start, _live_clause())).first()
        basis, verified, prior_id = (OPENING_GAP if anything is not None else OPENING_FIRST), False, None
    else:
        verified = int(prior.closing_micros) == opening.value
        basis, prior_id = (OPENING_PRIOR if verified else OPENING_GAP), prior.id
    return Statement(id=row.id, account_id=row.account_id, commodity=row.commodity,
                     period_start=row.period_start, period_end=row.period_end,
                     opening_micros=opening.value, closing_micros=int(row.closing_micros),
                     opening_verified=verified, opening_basis=basis, prior_statement_id=prior_id,
                     line_count=int(counted[0]), lines_micros=int(counted[1]), document_id=row.document_id,
                     document=document_of(conn, entity_id, row.document_id, context=f"statement {row.id}"),
                     page=None if row.page is None else int(row.page), locator=row.locator, reason=row.reason,
                     replaces_id=row.replaces_id, replaced_by_id=successor,
                     created_at=row.created_at, created_by=row.created_by)


def get_statement(conn: Connection, entity_id: str, statement_id: str) -> Statement | None:
    row = _statement_row(conn, entity_id, statement_id)
    return None if row is None else statement_of_row(conn, entity_id, row)


def line_of_row(row) -> StoredLine:
    import json
    fields = json.loads(row.fields)
    return StoredLine(id=row.id, statement_id=row.statement_id, line_no=int(row.line_no),
                      source_key=row.source_key, content_hash=row.content_hash, dt=row.dt,
                      amount_micros=int(row.amount_micros), description=row.description,
                      reference=row.reference, currency=fields.get("currency", ""), raw=row.raw)


def statement_lines(conn: Connection, entity_id: str, statement_id: str) -> list[StoredLine]:
    rows = conn.execute(sa.select(t.statement_lines)
                        .where(t.statement_lines.c.entity_id == entity_id,
                               t.statement_lines.c.statement_id == statement_id)
                        .order_by(t.statement_lines.c.line_no))
    return [line_of_row(r) for r in rows]


def get_line(conn: Connection, entity_id: str, line_id: str):
    return conn.execute(sa.select(t.statement_lines)
                        .where(t.statement_lines.c.entity_id == entity_id,
                               t.statement_lines.c.id == line_id)).first()


@dataclass(frozen=True)
class LineProvenance:
    """One line, its statement, the document behind it, and what it currently explains."""

    line: StoredLine
    statement_id: str
    account_id: str
    commodity: str
    period_start: str
    period_end: str
    statement_live: bool
    #: The document's id, not only its content (issue 0007).  A reader that can see a document's name and hash
    #: but cannot address it cannot ask for the bytes, which is what a provenance trail is for.
    document_id: str
    document: Document
    allocations: tuple[dict[str, Any], ...]

    def as_json(self) -> dict[str, Any]:
        return {**self.line.as_json(), "statement_id": self.statement_id, "account_id": self.account_id,
                "commodity": self.commodity, "period_start": self.period_start, "period_end": self.period_end,
                "statement_live": self.statement_live, "document_id": self.document_id,
                "document": self.document.as_view(), "allocations": [dict(a) for a in self.allocations]}


@dataclass(frozen=True)
class Provenance:
    subject_kind: str
    subject_id: str
    lines: tuple[LineProvenance, ...]
    evidence: tuple[dict[str, Any], ...]

    def as_json(self) -> dict[str, Any]:
        return {"subject": {"kind": self.subject_kind, "id": self.subject_id},
                "line_count": len(self.lines), "lines": [ln.as_json() for ln in self.lines],
                "evidence": [dict(e) for e in self.evidence]}


def _line_provenance(conn: Connection, entity_id: str, row, *, with_allocations: bool) -> LineProvenance:
    from haddon.repository import lines as lines_repo

    statement = conn.execute(sa.select(t.statements)
                             .where(t.statements.c.entity_id == entity_id,
                                    t.statements.c.id == row.statement_id)).first()
    if statement is None:
        raise HaddonError(INTERNAL_ERROR, f"statement line {row.id} points at a statement this entity does not "
                                          f"hold", details=[{"line_id": row.id, "statement_id": row.statement_id}])
    successor = successor_of(conn, entity_id, statement.id)
    allocations: tuple[dict[str, Any], ...] = ()
    if with_allocations:
        live = lines_repo.live_explanation(conn, entity_id, row.id)
        if live is not None:
            allocations = tuple({"allocation_id": a.id, "explanation_id": live.id, "kind": live.kind,
                                 "posting_id": a.posting_id, "entry_id": a.entry_id,
                                 "amount_micros": str(a.amount_micros)} for a in live.allocations)
    return LineProvenance(line=line_of_row(row), statement_id=statement.id, account_id=statement.account_id,
                          commodity=statement.commodity, period_start=statement.period_start,
                          period_end=statement.period_end, statement_live=successor is None,
                          document_id=statement.document_id,
                          document=document_of(conn, entity_id, statement.document_id,
                                               context=f"statement {statement.id}"),
                          allocations=allocations)


def _evidence_for_entries(conn: Connection, entity_id: str, entry_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
    if not entry_ids:
        return ()
    rows = conn.execute(sa.select(t.evidence_links)
                        .where(t.evidence_links.c.entity_id == entity_id,
                               t.evidence_links.c.entry_id.in_(list(entry_ids)))
                        .order_by(t.evidence_links.c.created_at, t.evidence_links.c.id))
    return tuple({"evidence_id": r.id, "entry_id": r.entry_id, "statement_id": r.statement_id,
                  "page": None if r.page is None else int(r.page), "locator": r.locator, "caption": r.caption,
                  "document_id": r.document_id,
                  "document": document_of(conn, entity_id, r.document_id,
                                          context=f"evidence {r.id}").as_view()} for r in rows)


def read_provenance(conn: Connection, entity_id: str, *, line_id: str | None = None,
                    posting_id: str | None = None, entry_id: str | None = None) -> Provenance:
    """Answer "where did this come from?" for a line, a posting or an entry.

    For a line, the answer is the statement and the document it was read off.  For a posting or an entry, it is
    the lines whose live explanation points at it, and so their documents, plus the evidence filed directly
    against the entry.  A subject this entity does not hold is a 404.  An empty ``lines`` list means the subject
    exists and no statement line links it to a document yet.
    """
    given = [(k, v) for k, v in (("line", line_id), ("posting", posting_id), ("entry", entry_id))
             if v is not None]
    if len(given) != 1:
        raise validation("give exactly one of line_id, posting_id or entry_id", path="subject",
                         reason="one_subject_required")
    kind, subject_id = given[0]
    if kind == "line":
        row = get_line(conn, entity_id, subject_id)
        if row is None:
            raise not_found("statement line", subject_id)
        return Provenance(kind, subject_id, (_line_provenance(conn, entity_id, row, with_allocations=True),), ())
    if kind == "posting":
        posting = conn.execute(sa.select(t.postings.c.id, t.postings.c.entry_id)
                               .where(t.postings.c.entity_id == entity_id,
                                      t.postings.c.id == subject_id)).first()
        if posting is None:
            raise not_found("posting", subject_id)
        posting_ids, entries = [subject_id], [posting.entry_id]
    else:
        entry = conn.execute(sa.select(t.entries.c.id).where(t.entries.c.entity_id == entity_id,
                                                             t.entries.c.id == subject_id)).first()
        if entry is None:
            raise not_found("entry", subject_id)
        posting_ids = [r.id for r in conn.execute(
            sa.select(t.postings.c.id).where(t.postings.c.entity_id == entity_id,
                                             t.postings.c.entry_id == subject_id))]
        entries = [subject_id]
    rows = conn.execute(
        sa.select(t.statement_lines)
        .select_from(t.statement_lines
                     .join(t.explanations, sa.and_(t.explanations.c.entity_id == t.statement_lines.c.entity_id,
                                                   t.explanations.c.line_id == t.statement_lines.c.id))
                     .join(t.allocations, sa.and_(t.allocations.c.entity_id == t.explanations.c.entity_id,
                                                  t.allocations.c.explanation_id == t.explanations.c.id)))
        .where(t.statement_lines.c.entity_id == entity_id, t.allocations.c.posting_id.in_(posting_ids),
               t.live_explanation_clause(entity_id))
        .order_by(t.statement_lines.c.dt, t.statement_lines.c.id)).all()
    seen, unique = set(), []
    for r in rows:
        if r.id not in seen:
            seen.add(r.id)
            unique.append(r)
    return Provenance(kind, subject_id,
                      tuple(_line_provenance(conn, entity_id, r, with_allocations=True) for r in unique),
                      _evidence_for_entries(conn, entity_id, entries))
