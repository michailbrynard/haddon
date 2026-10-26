"""Entries and postings: the ``draft -> posted`` / ``draft -> void`` path with deterministic pre-validation (D6).

Every function runs inside ``write_transaction``.  Validation happens in a fixed order before any statement is
issued, so the error a caller sees does not depend on trigger firing order (review defect 6):

draft creation   1 shape (422 validation)  2 overflow (422)  3 commodity known (422 validation)
                 4 account known (422 account_unknown)  5 commodity allowed on account (422 validation)
                 6 quantum (422 precision)  7 period lock (409 period_locked)
seal             1 exists (404)  2 still draft (409 immutable)  3 version (412 stale_version)
                 4 period lock (409 period_locked)  5 two legs + balanced per commodity (422 unbalanced)
                 6 accounts known / not archived (422 account_unknown / account_archived, U8)
void             1 exists  2 still draft  3 version  (a locked draft may be voided: D4)
revise           1 exists  2 still draft  3 version  4 header  5 legs  6 period lock (old dt AND new dt)
                 7 no leg an allocation points at is changed or removed

On creation, content (422) comes before state (409), so an agent learns about a malformed leg even when the period
is also locked.  On seal, the spec's "recheck versions/policy/locks" order applies first.  Triggers stay the
fallback.

A draft is working state and a posted entry is accounting.  ``revise_draft`` (chunk G2) is the whole "didn't get it
right first time" path.  A draft may be rewritten in place as often as it takes, and the books never see it because
a draft is in no report and no export.  Once sealed, the entry is immutable.  In-place edits to posted entries were
considered and rejected, and the only correction left is ``corrections.reverse_entry``.  So ``revise_draft`` refuses
anything that is not a draft, with the same ``immutable`` error every other transition uses.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.canonical import Tag, canonical_tags, check_tags, parse_tags
from haddon.domain.dates import parse_date
from haddon.domain.errors import (ACCOUNT_ARCHIVED, ACCOUNT_UNKNOWN, IMMUTABLE, INTERNAL_ERROR, PERIOD_LOCKED,
                                  STALE_VERSION, UNBALANCED, HaddonError, not_found, validation)
from haddon.domain.micros import Micros, sum_micros
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.provisioning import all_accounts, all_commodities, allowed_commodities

ORIGINS = ("source", "manual", "agent", "reversal", "opening", "invoice", "payroll", "adjustment")
# Origins the repository assigns itself; a caller-supplied provenance of this kind is refused.
RESERVED_ORIGINS = frozenset({"reversal", "opening"})
MAX_LEGS = 1000  # spec: "entry 1000 legs"


@dataclass(frozen=True)
class Leg:
    account_id: str
    commodity: str
    amount: Micros | str  # Micros, or the wire form (base-10 integer string); never an int or float
    memo: str | None = None
    tax_code: str | None = None


@dataclass(frozen=True)
class Posting:
    id: str
    entry_id: str
    seq: int
    account_id: str
    commodity: str
    amount: Micros
    memo: str | None
    tax_code: str | None


@dataclass(frozen=True)
class Entry:
    id: str
    dt: str
    narration: str
    status: str
    origin: str
    version: int
    code: str = ""  # hledger tcode; '' when there is none
    comment: str = ""  # hledger tcomment, verbatim; '' when there is none
    tags: tuple[Tag, ...] = ()  # hledger ttags, in the order they appear in the comment
    legs: list[Posting] = field(default_factory=list)


def lock_through(conn: Connection) -> str | None:
    row = conn.execute(sa.select(t.period_locks.c.through)).first()
    if row is None:
        raise HaddonError(INTERNAL_ERROR, "period lock row missing: entity not provisioned")
    return row[0]


def is_locked(conn: Connection, dt: str) -> bool:
    through = lock_through(conn)
    return through is not None and dt <= through


def _parse_amount(leg: Leg, i: int) -> Micros:
    path = f"legs[{i}].amount"
    if isinstance(leg.amount, Micros):
        return leg.amount
    return Micros.parse(leg.amount, path=path)  # raises validation (shape) or overflow


def validate_legs(ctx: WriteCtx, legs: list[Leg], *, allow_archived: bool = True) -> list[Micros]:
    """Run phases 1-6 of draft validation and return the parsed amounts.

    Each phase checks every leg before the next phase starts, so the reported error always comes from the earliest
    phase that fails."""
    if not isinstance(legs, list) or not 1 <= len(legs) <= MAX_LEGS:
        raise validation(f"between 1 and {MAX_LEGS} legs are required", path="legs", reason="leg_count",
                         count=len(legs) if isinstance(legs, list) else None)
    # 1 shape
    bad = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, Leg):
            raise validation(f"legs[{i}] is not a Leg", path=f"legs[{i}]", reason="leg_shape")
        for fld in ("account_id", "commodity"):
            v = getattr(leg, fld)
            if not isinstance(v, str) or not v:
                bad.append({"path": f"legs[{i}].{fld}", "reason": "required"})
        for fld in ("memo", "tax_code"):
            v = getattr(leg, fld)
            if v is not None and not isinstance(v, str):
                bad.append({"path": f"legs[{i}].{fld}", "reason": "string_required"})
        if isinstance(leg.amount, (bool, int, float)) and not isinstance(leg.amount, Micros):
            bad.append({"path": f"legs[{i}].amount", "reason": "string_required", "type": type(leg.amount).__name__})
        elif not isinstance(leg.amount, (Micros, str)):
            bad.append({"path": f"legs[{i}].amount", "reason": "string_required", "type": type(leg.amount).__name__})
    if bad:
        raise HaddonError("validation", "malformed legs", details=bad)
    # 1b shape of amount strings (validation) and 2 overflow both come from Micros.parse, one leg at a time.
    # Collect them separately so a shape error on leg 5 is still reported before an overflow on leg 3.
    shape_errors, overflow_errors, amounts = [], [], []
    for i, leg in enumerate(legs):
        try:
            amounts.append(_parse_amount(leg, i))
        except HaddonError as exc:
            amounts.append(None)
            (overflow_errors if exc.code == "overflow" else shape_errors).extend(exc.details)
    if shape_errors:
        raise HaddonError("validation", "amounts must be base-10 integer strings", details=shape_errors)
    if overflow_errors:
        raise HaddonError("overflow", "amount outside the signed 64-bit range", details=overflow_errors)
    # 3 commodity known
    coms = all_commodities(ctx.conn, ctx.entity_id)
    bad = [{"path": f"legs[{i}].commodity", "reason": "commodity_unknown", "value": leg.commodity}
           for i, leg in enumerate(legs) if leg.commodity not in coms]
    if bad:
        raise HaddonError("validation", "unknown commodity", details=bad)
    # 4 account known
    accts = all_accounts(ctx.conn, ctx.entity_id)
    bad = [{"path": f"legs[{i}].account_id", "id": leg.account_id} for i, leg in enumerate(legs) if leg.account_id not in accts]
    if bad:
        raise HaddonError(ACCOUNT_UNKNOWN, "unknown account", details=bad)
    if not allow_archived:
        bad = [{"path": f"legs[{i}].account_id", "id": leg.account_id} for i, leg in enumerate(legs) if accts[leg.account_id].archived]
        if bad:
            raise HaddonError(ACCOUNT_ARCHIVED, "archived account", details=bad)
    # 5 commodity allowed on account
    allowed = allowed_commodities(ctx.conn, ctx.entity_id)
    bad = [{"path": f"legs[{i}].commodity", "reason": "commodity_not_allowed", "account_id": leg.account_id, "commodity": leg.commodity}
           for i, leg in enumerate(legs) if (leg.account_id, leg.commodity) not in allowed]
    if bad:
        raise HaddonError("validation", "commodity not allowed on account", details=bad)
    # 6 quantum
    bad = []
    for i, (leg, amt) in enumerate(zip(legs, amounts)):
        try:
            amt.check_quantum(coms[leg.commodity], path=f"legs[{i}].amount")
        except HaddonError as exc:
            bad.extend(exc.details)
    if bad:
        raise HaddonError("precision", "amount is not a multiple of the commodity quantum", details=bad)
    return amounts  # type: ignore[return-value]


def validate_header(ctx: WriteCtx, dt, narration, origin, *, allow_reserved: bool, code: str = "", comment: str = "",
                    tags: object = ()) -> tuple[Tag, ...]:
    """Header validation (date shape, narration, origin enum / reserved origins, note fields).

    No statement is issued.  Returns the validated tags, which the caller stores as canonical JSON (E1): the
    order is preserved because it is a deterministic function of the comment and the export has to reproduce it.
    """
    parse_date(dt)
    if not isinstance(narration, str) or not narration.strip():
        raise validation("narration is required", path="narration", reason="required")
    for name, value in (("code", code), ("comment", comment)):
        if not isinstance(value, str):
            raise validation(f"{name} must be a string", path=name, reason="string_required")
    checked = check_tags(tags)
    if origin not in ORIGINS:
        raise validation(f"origin {origin!r} is not one of {ORIGINS}", path="origin", reason="enum", value=origin)
    if origin in RESERVED_ORIGINS and not allow_reserved:
        raise validation(f"origin {origin!r} is assigned by the service, not by the caller", path="origin", reason="reserved_origin")
    return checked


def check_open(conn: Connection, dt: str, path: str = "dt") -> None:
    """Refuse a write dated on or before the period lock, and name the way through.

    The detail carries ``remedy: "reopen"`` because reopening is the only way a closed month changes.  Reopening is
    deliberately not gated on eligibility (chunk E2), so it is always a real next step and never advice that might
    itself be refused.  A correction to an already-closed month runs into this (chunk F2).
    """
    through = lock_through(conn)
    if through is not None and dt <= through:
        raise HaddonError(PERIOD_LOCKED,
                          f"{dt} is on or before the period lock {through}; reopen the period through an "
                          f"earlier date before changing it",
                          details=[{"path": path, "dt": dt, "locked_through": through, "remedy": "reopen"}])


def create_draft(ctx: WriteCtx, *, dt: str, narration: str, origin: str, legs: list[Leg], id: str | None = None,
                 code: str = "", comment: str = "", tags: Iterable[Any] = (), _allow_reserved_origin: bool = False) -> Entry:
    """Insert a draft header and its legs after the full fixed-order validation.

    Drafts exist only inside the calling ``write_transaction``.  Callers seal or void them before commit.

    ``code``, ``comment`` and ``tags`` are the transaction-level note fields migration 0002 added (E1), hledger's
    ``tcode``, ``tcomment`` and ``ttags``.  They default to empty ("this transaction has none"), not NULL.
    """
    checked = validate_header(ctx, dt, narration, origin, allow_reserved=_allow_reserved_origin, code=code,
                              comment=comment, tags=tags)
    amounts = validate_legs(ctx, legs)
    check_open(ctx.conn, dt)  # 7: state after content
    eid = ctx.insert(t.entries, id, "e", dt=dt, narration=narration, status="draft", origin=origin,
                     version=1, code=code, comment=comment, tags=canonical_tags(checked))
    for seq, (leg, amt) in enumerate(zip(legs, amounts), 1):
        ctx.insert(t.postings, f"{eid}.p{seq}", "p", entry_id=eid, seq=seq, account_id=leg.account_id, commodity=leg.commodity,
                   amount_micros=amt.value, memo=leg.memo, tax_code=leg.tax_code)
    return get_entry(ctx.conn, ctx.entity_id, eid)  # type: ignore[return-value]


#: The core does not know what an allocation is.  The reconciliation extension registers a function here that
#: answers "which postings of this entry does something already point at?".  With the extension absent or
#: disabled the answer is "none" and a draft revises freely.  This seam (issue 0004) means
#: ``tests/test_core_boundaries.py`` need not excuse a core module reading a reconciliation table.
_ALLOCATION_GUARD: Callable[[WriteCtx, str], set[str]] | None = None


def register_allocation_guard(guard) -> None:
    """Install the extension hook consulted before a draft's legs are rewritten."""
    global _ALLOCATION_GUARD
    _ALLOCATION_GUARD = guard


def _allocated_postings(ctx: WriteCtx, entry_id: str) -> set[str]:
    """The postings of one entry that a live explanation points at.

    A revision may not change or remove one of these.  The allocation says "this posting and that statement line
    are the same money", and silently moving the posting under it would make the allocation false.  The way through
    is to supersede the explanation first, and the refusal says so.
    """
    if _ALLOCATION_GUARD is None:
        return set()
    return _ALLOCATION_GUARD(ctx, entry_id)


def revise_draft(ctx: WriteCtx, entry_id: str, *, expected_version: int, legs: list[Leg] | None = None,
                 narration: str | None = None, dt: str | None = None, code: str | None = None,
                 comment: str | None = None, tags: Iterable[Any] | None = None) -> Entry:
    """Rewrite a draft in place.  ``None`` means "leave this as it is", and ``origin`` is never revisable.

    ``legs`` replaces the legs wholesale, matched to the existing postings by sequence, so a posting an allocation
    already points at keeps its id.  Everything a fresh draft's validation checks is checked here in the same fixed
    order.  Both the old and the new date must be in an open period.  The trigger demands that too, and a draft is
    not a way to write into a closed month.
    """
    e = _require_draft(ctx, entry_id, expected_version)
    checked = validate_header(ctx, e.dt if dt is None else dt, e.narration if narration is None else narration,
                              e.origin, allow_reserved=True, code=e.code if code is None else code,
                              comment=e.comment if comment is None else comment,
                              tags=e.tags if tags is None else tags)
    amounts = validate_legs(ctx, legs) if legs is not None else None
    check_open(ctx.conn, e.dt)  # the entry as it stands
    if dt is not None and dt != e.dt:
        check_open(ctx.conn, dt, path="dt")  # and where it is being moved to
    if legs is not None:
        allocated = _allocated_postings(ctx, e.id)
        if allocated:
            kept = {f"{e.id}.p{seq}": (leg.account_id, leg.commodity, amt.value)
                    for seq, (leg, amt) in enumerate(zip(legs, amounts), 1)}
            held = {p.id: (p.account_id, p.commodity, p.amount.value) for p in e.legs}
            moved = sorted(pid for pid in allocated if kept.get(pid) != held.get(pid))
            if moved:
                raise HaddonError(IMMUTABLE,
                                  f"entry {e.id!r} has legs an allocation points at; revising one of them would "
                                  f"move the money under a match that says it is the statement line",
                                  details=[{"path": "legs", "reason": "posting_allocated", "entry_id": e.id,
                                            "posting_ids": moved, "remedy": "replace_or_void_the_allocation"}])
    values: dict[str, Any] = {"version": e.version + 1}
    if dt is not None:
        values["dt"] = dt
    if narration is not None:
        values["narration"] = narration
    if code is not None:
        values["code"] = code
    if comment is not None:
        values["comment"] = comment
    if tags is not None:
        values["tags"] = canonical_tags(checked)
    res = ctx.execute(sa.update(t.entries).where(t.entries.c.id == e.id, t.entries.c.entity_id == ctx.entity_id,
                                                 t.entries.c.status == "draft", t.entries.c.version == e.version)
                      .values(**values))
    if res.rowcount != 1:  # pragma: no cover - single-writer transaction
        raise HaddonError(STALE_VERSION, f"entry {e.id!r} changed during the transaction", details=[{"id": e.id}])
    if legs is not None:
        _rewrite_legs(ctx, e, legs, amounts)
    return get_entry(ctx.conn, ctx.entity_id, entry_id)  # type: ignore[return-value]


def _rewrite_legs(ctx: WriteCtx, e: Entry, legs: list[Leg], amounts: list[Micros]) -> None:
    """Replace the postings of a draft, by sequence: update what is there, insert what is new, delete the rest."""
    held = {p.seq: p for p in e.legs}
    for seq, (leg, amt) in enumerate(zip(legs, amounts), 1):
        old = held.pop(seq, None)
        values = {"account_id": leg.account_id, "commodity": leg.commodity, "amount_micros": amt.value,
                  "memo": leg.memo, "tax_code": leg.tax_code}
        if old is None:
            ctx.insert(t.postings, f"{e.id}.p{seq}", "p", entry_id=e.id, seq=seq, **values)
        elif (old.account_id, old.commodity, old.amount.value, old.memo, old.tax_code) != \
                (leg.account_id, leg.commodity, amt.value, leg.memo, leg.tax_code):
            ctx.execute(sa.update(t.postings).where(t.postings.c.id == old.id,
                                                    t.postings.c.entity_id == ctx.entity_id).values(**values))
    for leftover in sorted(held, reverse=True):
        ctx.execute(sa.delete(t.postings).where(t.postings.c.id == held[leftover].id,
                                                t.postings.c.entity_id == ctx.entity_id))


def seal(ctx: WriteCtx, entry_id: str, *, expected_version: int) -> Entry:
    """``draft -> posted``.  Balanced per commodity, at least two legs, every account known and unarchived."""
    e = _require_draft(ctx, entry_id, expected_version)
    check_open(ctx.conn, e.dt)
    # 5 two legs + balanced per commodity (Python-int sums; SQLite SUM overflow would be an error too)
    if len(e.legs) < 2:
        raise HaddonError(UNBALANCED, "an entry needs at least two legs", details=[{"path": "legs", "reason": "two_legs_required", "count": len(e.legs)}])
    sums = {c: sum_micros(p.amount for p in e.legs if p.commodity == c) for c in sorted({p.commodity for p in e.legs})}
    off = [{"commodity": c, "sum": s.render()} for c, s in sums.items() if not s.is_zero]
    if off:
        raise HaddonError(UNBALANCED, "legs do not net to zero per commodity", details=off)
    # 6 accounts
    accts = all_accounts(ctx.conn, ctx.entity_id)
    missing = [{"path": f"legs[{p.seq - 1}].account_id", "id": p.account_id} for p in e.legs if p.account_id not in accts]
    if missing:
        raise HaddonError(ACCOUNT_UNKNOWN, "unknown account", details=missing)
    archived = [{"path": f"legs[{p.seq - 1}].account_id", "id": p.account_id} for p in e.legs if accts[p.account_id].archived]
    if archived:
        raise HaddonError(ACCOUNT_ARCHIVED, "archived account on a leg", details=archived)
    _transition(ctx, e, "posted")
    return get_entry(ctx.conn, ctx.entity_id, entry_id)  # type: ignore[return-value]


def void(ctx: WriteCtx, entry_id: str, *, expected_version: int) -> Entry:
    """``draft -> void``.  Allowed under a period lock (D4: a draft is not accounting)."""
    e = _require_draft(ctx, entry_id, expected_version)
    _transition(ctx, e, "void")
    return get_entry(ctx.conn, ctx.entity_id, entry_id)  # type: ignore[return-value]


def post_entry(ctx: WriteCtx, *, dt: str, narration: str, origin: str, legs: list[Leg], id: str | None = None,
               code: str = "", comment: str = "", tags: Iterable[Any] = (), _allow_reserved_origin: bool = False) -> Entry:
    """Draft and seal in one step (the shape every application path uses)."""
    d = create_draft(ctx, dt=dt, narration=narration, origin=origin, legs=legs, id=id, code=code, comment=comment,
                     tags=tags, _allow_reserved_origin=_allow_reserved_origin)
    return seal(ctx, d.id, expected_version=d.version)


def _require_draft(ctx: WriteCtx, entry_id: str, expected_version: int) -> Entry:
    e = get_entry(ctx.conn, ctx.entity_id, entry_id)
    if e is None:
        raise not_found("entry", entry_id)
    if e.status != "draft":
        raise HaddonError(IMMUTABLE, f"entry {entry_id!r} is {e.status}; no transition leaves {e.status}",
                          details=[{"id": entry_id, "status": e.status}])
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        raise validation("expected_version is required", path="expected_version", reason="required")
    if e.version != expected_version:
        raise HaddonError(STALE_VERSION, f"entry {entry_id!r} is at version {e.version}, not {expected_version}",
                          details=[{"id": entry_id, "current_version": e.version, "expected_version": expected_version}])
    return e


def _transition(ctx: WriteCtx, e: Entry, status: str) -> None:
    res = ctx.execute(sa.update(t.entries).where(t.entries.c.id == e.id, t.entries.c.entity_id == ctx.entity_id,
                                                 t.entries.c.status == "draft", t.entries.c.version == e.version)
                      .values(status=status, version=e.version + 1))
    if res.rowcount != 1:
        raise HaddonError(STALE_VERSION, f"entry {e.id!r} changed during the transaction", details=[{"id": e.id}])


def get_entry(conn: Connection, entity_id: str, entry_id: str) -> Entry | None:
    row = conn.execute(sa.select(t.entries).where(t.entries.c.entity_id == entity_id, t.entries.c.id == entry_id)).first()
    if row is None:
        return None
    legs = [Posting(p.id, p.entry_id, p.seq, p.account_id, p.commodity, Micros(p.amount_micros), p.memo, p.tax_code)
            for p in conn.execute(sa.select(t.postings).where(t.postings.c.entry_id == entry_id).order_by(t.postings.c.seq))]
    return Entry(id=row.id, dt=row.dt, narration=row.narration, status=row.status,
                 origin=row.origin, version=row.version, code=row.code, comment=row.comment,
                 tags=parse_tags(row.tags), legs=legs)


def leg_multiset(e: Entry) -> Counter:
    """Canonical leg multiset used by the correction gate ("compare canonical mirror leg multisets")."""
    return Counter((p.account_id, p.commodity, p.amount.value, p.memo, p.tax_code) for p in e.legs)
