"""Booked, awaiting a document: the state a provisional booking is in, and how it stops being in it.

A provisional booking is one where the cost is real, the amount is the best available, and a document is expected
that may change it.  A typical one is a cost booked gross that splits into a smaller net cost plus recoverable tax
once its invoice arrives.  Without a record of this, the list of open items lives outside the books in somebody's
notes, and the books are only right if somebody remembers.

The state is not a column on the entry, because a posted entry is immutable.  What is wanted is the history of a
judgement: provisional from this date for this stated reason, then settled by this document.  So this is an
append-only side table with a ``supersedes_id`` chain, the shape ``explanations`` uses for statement lines.  The
live record is the one nothing supersedes.

It clears by doing the real thing.  Filing the awaited document against the entry (``documents.file_against_entry``)
writes the ``met`` row itself, naming the ``evidence_links`` row that met it.  There is no "mark as done".  A tick
box would be a second thing to remember, and so a second thing to forget.

Withdrawing is the other ending.  Sometimes no better document exists, for example when a simplified receipt is all
the supplier will ever issue.  That is not a document arriving and must not be recorded as one, so it is its own
state with its own required reason, and the booking stands as it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.errors import IMMUTABLE, HaddonError, not_found, validation
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx


AWAITING, MET, WITHDRAWN = "awaiting", "met", "withdrawn"


@dataclass(frozen=True)
class Expectation:
    """One record in the chain: what was expected of this entry, and what became of the expectation."""

    id: str
    entry_id: str
    state: str
    awaiting: str
    reason: str
    evidence_id: str | None
    supersedes_id: str | None
    created_at: str
    created_by: str

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "entry_id": self.entry_id, "state": self.state, "awaiting": self.awaiting,
                "reason": self.reason, "evidence_id": self.evidence_id,
                "supersedes_id": self.supersedes_id, "created_at": self.created_at,
                "created_by": self.created_by}


def of_row(r) -> Expectation:
    """One ``document_expectations`` row as an :class:`Expectation`."""
    return Expectation(id=r.id, entry_id=r.entry_id, state=r.state, awaiting=r.awaiting, reason=r.reason,
                       evidence_id=r.evidence_id, supersedes_id=r.supersedes_id,
                       created_at=r.created_at, created_by=r.created_by)


def live_clause():
    """The rows nothing supersedes: the current state of every expectation ever opened."""
    successor = t.document_expectations.alias("successor")
    return ~sa.exists().where(sa.and_(successor.c.entity_id == t.document_expectations.c.entity_id,
                                      successor.c.supersedes_id == t.document_expectations.c.id))


def awaiting_clause():
    """What "provisional" means as a query: a live record still in the ``awaiting`` state."""
    return sa.and_(t.document_expectations.c.state == AWAITING, live_clause())


def awaiting_for_entry(conn: Connection, entity_id: str, entry_id: str) -> Expectation | None:
    """The one live ``awaiting`` record for this entry, if it has one.

    There is at most one, because ``expectations_insert`` refuses a second.  An entry may also have settled chains
    beside it (a document was expected, arrived, and later another was expected), so "live" alone is not unique and
    the state is part of the query.
    """
    row = conn.execute(sa.select(t.document_expectations)
                       .where(t.document_expectations.c.entity_id == entity_id,
                              t.document_expectations.c.entry_id == entry_id, awaiting_clause())).first()
    return None if row is None else of_row(row)


def history_of_entry(conn: Connection, entity_id: str, entry_id: str) -> list[Expectation]:
    """Every record about this entry, in the order the chain was written.

    The order follows the chain, not ``created_at``.  Timestamps are second-resolution, so three records written
    within one second would sort by their random ids, and a reader could be shown "withdrawn, awaiting, awaiting",
    the opposite of what happened.  Each record names the one it supersedes, so within a chain the order is in the
    data.
    """
    raw = list(conn.execute(
        sa.select(t.document_expectations, sa.literal_column("rowid").label("_rowid"))
        .where(t.document_expectations.c.entity_id == entity_id,
               t.document_expectations.c.entry_id == entry_id)))
    rows = [of_row(r) for r in raw]
    written = {r.id: r._rowid for r in raw}
    successor = {r.supersedes_id: r for r in rows if r.supersedes_id is not None}
    # Separate chains (an entry can await a second document after the first arrived) have no link between them,
    # so they go in the order they were written.  Inside a chain the links decide.
    roots = sorted((r for r in rows if r.supersedes_id is None), key=lambda r: written[r.id])
    ordered: list[Expectation] = []
    for root in roots:
        current: Expectation | None = root
        while current is not None:
            ordered.append(current)
            current = successor.get(current.id)
    return ordered


def get_expectation(conn: Connection, entity_id: str, expectation_id: str) -> Expectation | None:
    row = conn.execute(sa.select(t.document_expectations)
                       .where(t.document_expectations.c.entity_id == entity_id,
                              t.document_expectations.c.id == expectation_id)).first()
    return None if row is None else of_row(row)


def expect_document(ctx: WriteCtx, *, entry_id: str, awaiting: str, reason: str) -> Expectation:
    """Record that this booking is provisional, naming the document that would settle it.

    Calling it again for an entry that is already awaiting a document restates the expectation rather than
    refusing.  An expectation that becomes more specific ("the supplier's invoice" becomes "a full invoice carrying
    our tax number") is a revision, and the chain keeps both.  It never opens a second expectation beside the first.
    One live record per entry is what makes "what is provisional?" a query rather than a fold.
    """
    awaiting = _text(awaiting, "awaiting", max_len=1024)
    reason = _text(reason, "reason", max_len=4096)
    entry = _entry(ctx, entry_id)
    # A settled chain does not block a new one: a booking can await a second document after the first arrived.
    previous = awaiting_for_entry(ctx.conn, ctx.entity_id, entry_id)
    return _append(ctx, entry_id=entry_id, state=AWAITING, awaiting=awaiting, reason=reason, evidence_id=None,
                   supersedes_id=None if previous is None else previous.id, action="entry.awaiting_document",
                   input={"entry_id": entry_id, "entry_dt": entry.dt, "awaiting": awaiting, "reason": reason,
                          "restates_id": None if previous is None else previous.id})


def withdraw_expectation(ctx: WriteCtx, *, entry_id: str, reason: str) -> Expectation:
    """No document is coming.  The booking stands as it is, and the reason says why it is not provisional.

    This is not the same as the document arriving and must not be recorded as if it were.  A simplified receipt
    that will never be reissued is a different fact about the books from an invoice that turned up.
    """
    reason = _text(reason, "reason", max_len=4096)
    _entry(ctx, entry_id)
    live = awaiting_for_entry(ctx.conn, ctx.entity_id, entry_id)
    if live is None:
        raise HaddonError(IMMUTABLE, f"entry {entry_id} is not awaiting a document",
                          details=[{"path": "entry_id", "entry_id": entry_id, "reason": "nothing_awaited"}])
    return _append(ctx, entry_id=entry_id, state=WITHDRAWN, awaiting=live.awaiting, reason=reason,
                   evidence_id=None, supersedes_id=live.id, action="entry.document_not_expected",
                   input={"entry_id": entry_id, "awaiting": live.awaiting, "withdraws_id": live.id,
                          "reason": reason})


def meet_with_evidence(ctx: WriteCtx, *, entry_id: str, evidence_id: str, reason: str) -> Expectation | None:
    """Settle the live expectation now that the document has arrived, naming the evidence link.

    Only ``documents.file_against_entry`` calls this.  Filing the document is the resolution, and there is no
    separate act that could be done instead or forgotten after.  Returns ``None`` when the entry was not awaiting
    anything, which is the usual case.  Most documents are filed against bookings nobody called provisional.
    """
    live = awaiting_for_entry(ctx.conn, ctx.entity_id, entry_id)
    if live is None:
        return None
    return _append(ctx, entry_id=entry_id, state=MET, awaiting=live.awaiting, reason=reason,
                   evidence_id=evidence_id, supersedes_id=live.id, action="entry.document_arrived",
                   input={"entry_id": entry_id, "evidence_id": evidence_id, "awaiting": live.awaiting,
                          "meets_id": live.id, "reason": reason})


def _append(ctx: WriteCtx, *, entry_id: str, state: str, awaiting: str, reason: str, evidence_id: str | None,
            supersedes_id: str | None, action: str, input: dict, id: str | None = None) -> Expectation:
    from haddon.repository.audit import record_action

    row_id = ctx.insert(t.document_expectations, id, "dx", entry_id=entry_id, state=state, awaiting=awaiting,
                        reason=reason, evidence_id=evidence_id, supersedes_id=supersedes_id)
    record = Expectation(id=row_id, entry_id=entry_id, state=state, awaiting=awaiting, reason=reason,
                         evidence_id=evidence_id, supersedes_id=supersedes_id,
                         created_at=ctx.now, created_by=ctx.actor)
    record_action(ctx, action=action, subject_kind="entry", subject_id=entry_id, reason=reason,
                  result=record.as_json(), input=input)
    return record


def _entry(ctx: WriteCtx, entry_id: str):
    row = ctx.conn.execute(sa.select(t.entries.c.id, t.entries.c.dt, t.entries.c.narration)
                           .where(t.entries.c.entity_id == ctx.entity_id,
                                  t.entries.c.id == entry_id)).first()
    if row is None:
        raise not_found("entry", entry_id)
    return row


def _text(value: object, path: str, *, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise validation(f"{path} is required", path=path, reason="required")
    if len(value) > max_len:
        raise validation(f"{path} is longer than {max_len} characters", path=path, reason="too_long",
                         max_length=max_len, length=len(value))
    return value
