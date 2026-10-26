"""Documents: registering a file the books rest on, and filing it against an entry.

Without this module the only way into the ledger for a document is ``POST /statements``, which leaves invoices,
receipts and contracts outside the books as narration text.  That is the wrong way round for a ledger whose rule is
that treatment follows the document.  A cost with a full invoice is booked net of recoverable tax and the same cost
without one is booked gross, so the document is the reason the entry has the shape it has.

The module adds no tables.  It is two writes over tables that already do the job:

* ``documents`` is content-addressed (``sha256`` is UNIQUE).  The same bytes submitted twice are the same row,
  whatever the file was called the second time.
* ``evidence_links`` binds a document, a page, a locator and a caption to an ``entry_id`` or to a statement, and
  ``read_provenance`` reads that binding back.  So ``GET /provenance?entry_id=...`` answers "what does this entry
  rest on?" with no new read.

The one change underneath was a trigger (migration 0004).  Filing a document against an entry in a closed period is
allowed, because an evidence link has no amount, account or date and changes no figure a closed month reports.

The file itself is not uploaded.  ``blob_path`` says where the bytes are kept, and the ledger records it without
fetching it, the same contract ``POST /statements`` has.  The ledger keeps the part that must not be lost: the
hash, the provenance and the reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.errors import IMMUTABLE, HaddonError, not_found, validation
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.statements import (
    Document,
    check_document,
    document_by_sha256,
    document_of_row,
    document_row,
    store_document,
)

#: The audit action a registration is recorded as.
REGISTER_ACTION = "document.registered"

#: The audit action a filing is recorded as.
ATTACH_ACTION = "document.filed"


@dataclass(frozen=True)
class Registration:
    """A registered document, and whether this call is what created it."""

    document: Document
    document_id: str
    created: bool
    created_at: str
    created_by: str

    def as_json(self) -> dict[str, Any]:
        return {"document_id": self.document_id, "created": self.created, **self.document.as_json(),
                "created_at": self.created_at, "created_by": self.created_by}


@dataclass(frozen=True)
class Filing:
    """One document filed against one entry: the ``evidence_links`` row and what it points at."""

    id: str
    entry_id: str
    document_id: str
    page: int | None
    locator: str
    caption: str
    created_at: str
    created_by: str
    #: The ``document_expectations`` row this filing settled, when the entry was awaiting a document.
    met_expectation_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "entry_id": self.entry_id, "document_id": self.document_id,
                "page": self.page, "locator": self.locator, "caption": self.caption,
                "created_at": self.created_at, "created_by": self.created_by,
                "met_expectation_id": self.met_expectation_id}


def filings_of_document(conn: Connection, entity_id: str, document_id: str) -> list[dict[str, Any]]:
    """List every place this document is cited, whether against an entry or a statement.

    The read includes the links the statement loader and the lines inbox write as well as the ones filed here.
    Otherwise a loaded bank statement would look unattached.
    """
    rows = conn.execute(sa.select(t.evidence_links)
                        .where(t.evidence_links.c.entity_id == entity_id,
                               t.evidence_links.c.document_id == document_id)
                        .order_by(t.evidence_links.c.created_at, t.evidence_links.c.id))
    out = []
    for r in rows:
        subject_kind, subject_id = next(
            ((k, v) for k, v in (("entry", r.entry_id), ("statement", r.statement_id)) if v is not None),
            ("unknown", None))
        out.append({"evidence_id": r.id, "subject_kind": subject_kind, "subject_id": subject_id,
                    "page": None if r.page is None else int(r.page), "locator": r.locator, "caption": r.caption,
                    "created_at": r.created_at, "created_by": r.created_by})
    return out


def register_document(ctx: WriteCtx, *, document: Document, reason: str) -> Registration:
    """Register a document, or return the one already registered for the same bytes.

    Identity is the sha256, so a repeat is not an error.  Registering the same file twice is what happens when two
    entries rest on one invoice, or when an agent re-reads a directory.  The second call returns the first call's
    row and writes nothing.

    The one refusal is a submission that contradicts the stored row: identical bytes cannot have a different
    length.  Names and media types may differ.  The same PDF saved under two names is still one document, and the
    stored row keeps the name it was first registered under.

    This differs on purpose from ``load_statement``, which refuses a re-load because loading a statement twice
    would double a period's lines.  Registering a document twice changes nothing, so refusing it would only push
    callers into checking first.
    """
    doc = check_document(document)
    reason = _reason(reason)
    existing = document_by_sha256(ctx.conn, ctx.entity_id, doc.sha256)
    if existing is not None:
        store_document(ctx, doc)  # raises on a size that contradicts the stored row; a matching one is a no-op
        return Registration(document=document_of_row(existing), document_id=existing.id, created=False,
                            created_at=existing.created_at, created_by=existing.created_by)

    from haddon.repository.audit import record_action

    file_id = store_document(ctx, doc)
    registration = Registration(document=doc, document_id=file_id, created=True, created_at=ctx.now,
                                created_by=ctx.actor)
    record_action(ctx, action=REGISTER_ACTION, subject_kind="document", subject_id=file_id, reason=reason,
                  result=registration.as_json(), input={"document": doc.as_json(), "reason": reason},
                  evidence_refs=[file_id])
    return registration


def file_against_entry(ctx: WriteCtx, *, entry_id: str, document_id: str, locator: str, caption: str,
                       reason: str, page: int | None = None) -> Filing:
    """File a registered document against an entry.

    ``locator`` says where in the document the relevant part is ("line 3 of the table on page 2", "the total
    box"), ``caption`` says what it is ("supplier invoice 24267 for the May desk rental").  Both are required
    and both are free text, because what a document is depends on the document.

    A closed period does not refuse a filing (migration 0004).  An invoice arriving in March for a January cost is
    an ordinary event, and filing it changes no figure January reports.

    Filing the same document against the same entry at the same page and locator is refused as a duplicate.  A
    different page or locator is allowed, because one invoice can evidence two different things about one entry.
    """
    from haddon.repository import expectations as ex
    from haddon.repository.audit import record_action

    locator = _text(locator, "locator", max_len=512)
    caption = _text(caption, "caption", max_len=1024)
    reason = _reason(reason)
    if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
        raise validation("page must be a positive integer", path="page", reason="page_invalid")

    entry = ctx.conn.execute(sa.select(t.entries.c.id, t.entries.c.dt, t.entries.c.narration)
                             .where(t.entries.c.entity_id == ctx.entity_id,
                                    t.entries.c.id == entry_id)).first()
    if entry is None:
        raise not_found("entry", entry_id)
    file_row = document_row(ctx.conn, ctx.entity_id, document_id)
    if file_row is None:
        raise not_found("document", document_id)

    duplicate = ctx.conn.execute(
        sa.select(t.evidence_links.c.id)
        .where(t.evidence_links.c.entity_id == ctx.entity_id, t.evidence_links.c.entry_id == entry_id,
               t.evidence_links.c.document_id == document_id,
               t.evidence_links.c.locator == locator,
               t.evidence_links.c.page.is_(None) if page is None else t.evidence_links.c.page == page)).first()
    if duplicate is not None:
        raise HaddonError(IMMUTABLE,
                          f"{file_row.filename} is already filed against entry {entry_id} at this place",
                          details=[{"path": "document_id", "evidence_id": duplicate.id, "entry_id": entry_id,
                                    "document_id": document_id, "page": page, "locator": locator}])

    doc = document_of_row(file_row)
    link_id = ctx.insert(t.evidence_links, None, "ev", document_id=document_id, entry_id=entry_id,
                         statement_id=None, page=page, locator=locator, caption=caption)
    # If this entry was booked awaiting a document, the document arriving is the resolution
    # (docs/design/decisions.md, "Booked, awaiting a document").  There is no separate "mark it done" step,
    # because the failure this answers is that somebody has to remember one.
    met = ex.meet_with_evidence(ctx, entry_id=entry_id, evidence_id=link_id, reason=reason)
    filing = Filing(id=link_id, entry_id=entry_id, document_id=document_id, page=page, locator=locator,
                    caption=caption, created_at=ctx.now, created_by=ctx.actor,
                    met_expectation_id=None if met is None else met.id)
    record_action(ctx, action=ATTACH_ACTION, subject_kind="entry", subject_id=entry_id, reason=reason,
                  result=filing.as_json(),
                  input={"entry_id": entry_id, "entry_dt": entry.dt, "document": doc.as_json(), "page": page,
                         "locator": locator, "caption": caption, "reason": reason},
                  evidence_refs=[link_id])
    return filing


def _text(value: object, path: str, *, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise validation(f"{path} is required", path=path, reason="required")
    if len(value) > max_len:
        raise validation(f"{path} is longer than {max_len} characters", path=path, reason="too_long",
                         max_length=max_len, length=len(value))
    return value


def _reason(value: object) -> str:
    return _text(value, "reason", max_len=4096)
