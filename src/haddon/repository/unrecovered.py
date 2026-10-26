"""Recoverable tax that was not recovered, and why.

A recoverable tax is sometimes deliberately not claimed, for a documented reason: the document on hand does not
support a claim, or no document exists yet.  Without this record only the effect is stored (a larger number in an
expense or asset account) and the decision is not.  The books then cannot say what tax is not being reclaimed and
why, and an amount sitting inside a fixed asset has nothing in the store explaining it.

That is real money, and it is often recoverable long afterwards, since many regimes allow a claim for years after
the document's date.  An invisible balance is a balance nobody reclaims.

Nothing here is specific to a country or a tax.  There is no VAT field, return form or deadline rule, and the
ledger holds no regime logic.  What it holds is:

* the amount and its commodity.  The amount is positive because it is an amount not recovered.  A negative one
  would be a claim, and a claim is a posting;
* the reason it was not recovered.  This is required, because the reason is the point of the record;
* optionally ``tax_code``, the entity's own code for which tax this is.  The chart and the postings use the same
  column name, so nothing new has to be agreed to say "input VAT" or "GST credit";
* optionally ``posting_id``, the leg the amount is hiding inside.  312.40 in a fixed asset is invisible in a way
  that 312.40 in an expense is not, and pointing at the leg makes it findable;
* optionally ``recoverable_until``, the last date on which it could still be recovered.  The caller supplies it,
  because only the caller knows the rule.  It turns the record from a note into an answer to "what can we still
  reclaim, and by when".

Records are append-only.  A posted entry cannot carry a status, and what is wanted is the history of a decision
anyway: not recovered for this reason, then either recovered by that entry or deliberately forgone.  It uses the
same ``supersedes_id`` chain as ``explanations`` and ``document_expectations``.

``forgone`` exists so the list does not only grow.  A claim window closes, or a small amount is not worth the
paperwork, and without it the record would sit there forever saying money is recoverable when it is not.  That is
the failure ``document_expectations`` was written to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.dates import parse_date
from haddon.domain.errors import IMMUTABLE, HaddonError, not_found, validation
from haddon.domain.micros import Micros
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.provisioning import all_commodities


UNRECOVERED, RECOVERED, FORGONE = "unrecovered", "recovered", "forgone"
OUTCOMES = (RECOVERED, FORGONE)


@dataclass(frozen=True)
class Unrecovered:
    """One record in the chain: an amount not recovered, and what became of it."""

    id: str
    entry_id: str
    posting_id: str | None
    commodity: str
    amount_micros: int
    state: str
    tax_code: str | None
    reason: str
    recoverable_until: str | None
    recovered_entry_id: str | None
    supersedes_id: str | None
    created_at: str
    created_by: str

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "entry_id": self.entry_id, "posting_id": self.posting_id,
                "commodity": self.commodity, "amount_micros": str(self.amount_micros), "state": self.state,
                "tax_code": self.tax_code, "reason": self.reason,
                "recoverable_until": self.recoverable_until, "recovered_entry_id": self.recovered_entry_id,
                "supersedes_id": self.supersedes_id,
                "created_at": self.created_at, "created_by": self.created_by}


def of_row(r) -> Unrecovered:
    """One ``unrecovered_tax`` row as an :class:`Unrecovered`."""
    return Unrecovered(id=r.id, entry_id=r.entry_id, posting_id=r.posting_id, commodity=r.commodity,
                       amount_micros=int(r.amount_micros), state=r.state, tax_code=r.tax_code, reason=r.reason,
                       recoverable_until=r.recoverable_until, recovered_entry_id=r.recovered_entry_id,
                       supersedes_id=r.supersedes_id, created_at=r.created_at,
                       created_by=r.created_by)


def live_clause():
    """The rows nothing supersedes: the current state of every amount ever recorded."""
    successor = t.unrecovered_tax.alias("successor")
    return ~sa.exists().where(sa.and_(successor.c.entity_id == t.unrecovered_tax.c.entity_id,
                                      successor.c.supersedes_id == t.unrecovered_tax.c.id))


def state_clause(state: str = UNRECOVERED):
    return sa.and_(t.unrecovered_tax.c.state == state, live_clause())


def live_for(conn: Connection, entity_id: str, entry_id: str, commodity: str) -> Unrecovered | None:
    """The one live ``unrecovered`` record for this entry and commodity, if there is one."""
    row = conn.execute(sa.select(t.unrecovered_tax)
                       .where(t.unrecovered_tax.c.entity_id == entity_id,
                              t.unrecovered_tax.c.entry_id == entry_id,
                              t.unrecovered_tax.c.commodity == commodity, state_clause())).first()
    return None if row is None else of_row(row)


def get_record(conn: Connection, entity_id: str, record_id: str) -> Unrecovered | None:
    row = conn.execute(sa.select(t.unrecovered_tax)
                       .where(t.unrecovered_tax.c.entity_id == entity_id,
                              t.unrecovered_tax.c.id == record_id)).first()
    return None if row is None else of_row(row)


def history_of(conn: Connection, entity_id: str, entry_id: str) -> list[Unrecovered]:
    """Every record about this entry, chain by chain, in the order each chain was written.

    Ordered the way ``expectations.history_of_entry`` is, for the same reason.  ``created_at`` is
    second-resolution, so two records written within one second would sort by their random ids, and a reader could
    be shown a recovery before the thing it recovered.
    """
    raw = list(conn.execute(
        sa.select(t.unrecovered_tax, sa.literal_column("rowid").label("_rowid"))
        .where(t.unrecovered_tax.c.entity_id == entity_id, t.unrecovered_tax.c.entry_id == entry_id)))
    rows = [of_row(r) for r in raw]
    written = {r.id: r._rowid for r in raw}
    successor = {r.supersedes_id: r for r in rows if r.supersedes_id is not None}
    roots = sorted((r for r in rows if r.supersedes_id is None), key=lambda r: written[r.id])
    ordered: list[Unrecovered] = []
    for root in roots:
        current: Unrecovered | None = root
        while current is not None:
            ordered.append(current)
            current = successor.get(current.id)
    return ordered


def totals(conn: Connection, entity_id: str, *, where=()) -> dict[str, str]:
    """Sum the records matching ``where``, per commodity.  Commodities are never added together."""
    rows = conn.execute(
        sa.select(t.unrecovered_tax.c.commodity, sa.func.sum(t.unrecovered_tax.c.amount_micros))
        .where(t.unrecovered_tax.c.entity_id == entity_id, *where)
        .group_by(t.unrecovered_tax.c.commodity).order_by(t.unrecovered_tax.c.commodity))
    return {code: str(int(total)) for code, total in rows}


def record_unrecovered(ctx: WriteCtx, *, entry_id: str, commodity: str, amount_micros: str | Micros, reason: str,
                       posting_id: str | None = None, tax_code: str | None = None,
                       recoverable_until: str | None = None) -> Unrecovered:
    """Record that a recoverable tax was not recovered on this entry, and why.

    This writes no posting and moves no balance.  The arithmetic already happened when the entry was booked gross.
    This adds the half of that decision the entry could not carry.
    """
    entry = _entry(ctx, entry_id)
    commodity = _commodity(ctx, commodity)
    amount = amount_micros if isinstance(amount_micros, Micros) else \
        Micros.parse(amount_micros, path="amount_micros")
    if amount.value <= 0:
        raise validation("amount_micros must be a positive amount: this is tax that was NOT recovered",
                         path="amount_micros", reason="positive_required", value=amount.render())
    amount.check_quantum(all_commodities(ctx.conn, ctx.entity_id)[commodity], path="amount_micros")
    reason = _text(reason, "reason", max_len=4096)
    tax_code = None if tax_code is None else _text(tax_code, "tax_code", max_len=255)
    if recoverable_until is not None:
        recoverable_until = parse_date(recoverable_until, path="recoverable_until")
    posting_id = _check_posting(ctx, entry_id, posting_id)

    existing = live_for(ctx.conn, ctx.entity_id, entry_id, commodity)
    if existing is not None:
        raise HaddonError(IMMUTABLE,
                          f"entry {entry_id} already has an unrecovered {commodity} amount of "
                          f"{Micros(existing.amount_micros).render()}; resolve that one rather than adding to it",
                          details=[{"path": "entry_id", "entry_id": entry_id, "commodity": commodity,
                                    "record_id": existing.id, "reason": "already_recorded",
                                    "amount_micros": str(existing.amount_micros)}])

    return _append(ctx, entry_id=entry_id, posting_id=posting_id, commodity=commodity,
                   amount_micros=amount.value, state=UNRECOVERED, tax_code=tax_code, reason=reason,
                   recoverable_until=recoverable_until, recovered_entry_id=None, supersedes_id=None,
                   action="tax.unrecovered",
                   input={"entry_id": entry_id, "entry_dt": entry.dt, "commodity": commodity,
                          "amount_micros": amount.render(), "tax_code": tax_code, "posting_id": posting_id,
                          "recoverable_until": recoverable_until, "reason": reason})


def resolve(ctx: WriteCtx, *, record_id: str, outcome: str, reason: str,
            recovered_entry_id: str | None = None) -> Unrecovered:
    """Close a record: it was recovered after all, or it will not be.

    ``recovered`` names the entry that recovered it, which must be a different entry, because a recovery is its own
    economic event and posts its own legs.  ``forgone`` names nothing, so its reason has to explain it: the window
    closed, the claim was not worth making, or the document never became good enough.
    """
    if outcome not in OUTCOMES:
        raise validation(f"outcome must be one of {', '.join(OUTCOMES)}", path="outcome", reason="enum",
                         value=outcome, allowed=list(OUTCOMES))
    reason = _text(reason, "reason", max_len=4096)
    record = get_record(ctx.conn, ctx.entity_id, record_id)
    if record is None:
        raise not_found("unrecovered_tax", record_id)
    if record.state != UNRECOVERED:
        raise HaddonError(IMMUTABLE, f"unrecovered_tax {record_id} is already {record.state}",
                          details=[{"path": "record_id", "record_id": record_id, "state": record.state}])
    live = live_for(ctx.conn, ctx.entity_id, record.entry_id, record.commodity)
    if live is None or live.id != record_id:
        raise HaddonError(IMMUTABLE, f"unrecovered_tax {record_id} has already been superseded",
                          details=[{"path": "record_id", "record_id": record_id}])
    if outcome == RECOVERED:
        if recovered_entry_id is None:
            raise validation("recovered_entry_id is required: a recovery is an entry",
                             path="recovered_entry_id", reason="required")
        if recovered_entry_id == record.entry_id:
            raise validation("the entry that recovered it cannot be the entry it was unrecovered on",
                             path="recovered_entry_id", reason="same_entry", value=recovered_entry_id)
        _entry(ctx, recovered_entry_id)
    elif recovered_entry_id is not None:
        raise validation("recovered_entry_id belongs to a recovery, not to an amount being forgone",
                         path="recovered_entry_id", reason="not_recovered")

    return _append(ctx, entry_id=record.entry_id, posting_id=record.posting_id, commodity=record.commodity,
                   amount_micros=record.amount_micros, state=outcome, tax_code=record.tax_code, reason=reason,
                   recoverable_until=record.recoverable_until, recovered_entry_id=recovered_entry_id,
                   supersedes_id=record_id,
                   action="tax.recovered" if outcome == RECOVERED else "tax.forgone",
                   input={"record_id": record_id, "entry_id": record.entry_id, "commodity": record.commodity,
                          "amount_micros": str(record.amount_micros), "outcome": outcome,
                          "recovered_entry_id": recovered_entry_id, "reason": reason})


def _append(ctx: WriteCtx, *, entry_id: str, posting_id: str | None, commodity: str, amount_micros: int,
            state: str, tax_code: str | None, reason: str, recoverable_until: str | None,
            recovered_entry_id: str | None, supersedes_id: str | None, action: str, input: dict,
            id: str | None = None) -> Unrecovered:
    from haddon.repository.audit import record_action

    row_id = ctx.insert(t.unrecovered_tax, id, "ut", entry_id=entry_id, posting_id=posting_id,
                        commodity=commodity, amount_micros=amount_micros, state=state, tax_code=tax_code,
                        reason=reason, recoverable_until=recoverable_until,
                        recovered_entry_id=recovered_entry_id, supersedes_id=supersedes_id)
    record = Unrecovered(id=row_id, entry_id=entry_id, posting_id=posting_id, commodity=commodity,
                         amount_micros=amount_micros, state=state, tax_code=tax_code, reason=reason,
                         recoverable_until=recoverable_until, recovered_entry_id=recovered_entry_id,
                         supersedes_id=supersedes_id, created_at=ctx.now, created_by=ctx.actor)
    record_action(ctx, action=action, subject_kind="entry", subject_id=entry_id, reason=reason,
                  result=record.as_json(), input=input)
    return record


def _entry(ctx: WriteCtx, entry_id: str):
    row = ctx.conn.execute(sa.select(t.entries.c.id, t.entries.c.dt)
                           .where(t.entries.c.entity_id == ctx.entity_id,
                                  t.entries.c.id == entry_id)).first()
    if row is None:
        raise not_found("entry", entry_id)
    return row


def _commodity(ctx: WriteCtx, commodity: object) -> str:
    code = _text(commodity, "commodity", max_len=64)
    if code not in all_commodities(ctx.conn, ctx.entity_id):
        raise validation(f"{code} is not a commodity of these books", path="commodity",
                         reason="commodity_unknown", value=code)
    return code


def _check_posting(ctx: WriteCtx, entry_id: str, posting_id: object) -> str | None:
    """Check that a named ``posting_id`` is a leg of this entry, and return it."""
    if posting_id is None:
        return None
    code = _text(posting_id, "posting_id", max_len=255)
    row = ctx.conn.execute(sa.select(t.postings.c.id)
                           .where(t.postings.c.entity_id == ctx.entity_id, t.postings.c.id == code,
                                  t.postings.c.entry_id == entry_id)).first()
    if row is None:
        raise validation(f"posting {code} is not a leg of entry {entry_id}", path="posting_id",
                         reason="posting_not_of_entry", value=code, entry_id=entry_id)
    return code


def _text(value: object, path: str, *, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise validation(f"{path} is required", path=path, reason="required")
    if len(value) > max_len:
        raise validation(f"{path} is longer than {max_len} characters", path=path, reason="too_long",
                         max_length=max_len, length=len(value))
    return value
