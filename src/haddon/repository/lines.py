"""Explaining a statement line: book it, link it, split it, ignore it, or change your mind about all of that.

A loaded statement is an input.  This module answers how the journal accounts for what the bank printed, with
four task-shaped writes and one read:

``book_line``     post the entry this line is, and hold the line against the cash posting it created.
``link_line``     the entry already exists; say which posting(s) of it this line is, and for how much.  One line
                  against several postings is the split, so there is no separate splitting verb.
``ignore_line``   this line will not be booked, and here is why.
``reexplain_line``  supersede whatever was said before.  If the old explanation booked an entry, the entry is
                  reversed (or corrected onto the new legs) through the ordinary correction path.
``line_state``    what has become of one line: its live explanation, its allocations, its unexplained remainder
                  and the whole append-only chain of how it got there.

A line has exactly one current status, and keeps its whole history.  An explanation is never edited.  A new one
names the one it supersedes, and ``explanations.supersedes_id`` is UNIQUE, so the history of a line is a chain
rather than a set of competing opinions.  "Live" is derived (``live_explanations``), so there is one notion of
current, computed in SQL rather than kept in a status column.

An allocation is not an economic event and must never be able to become one.  ``link_line`` posts nothing.  An
allocation can only point at a posting some entry already created, so linking, relinking and superseding leave
every balance unchanged, and no report reads any column here.  ``allocations.amount_micros`` is how much of a line
one posting explains.  It apportions two numbers that already exist, and the database refuses it if it disagrees
with either (``bad_allocations``) or if the allocations together exceed what there is to allocate
(``overallocated``).

Partial allocation is a state, not an error (issue 0004, pitfall 2).  A line explained for part of its amount, or
a cash posting allocated for part of its amount, is representable and visible.  ``line_state`` reports the
remainder, and the reconciliation close gate refuses a month holding one.

Correcting is reversing, not editing.  Nothing here rewrites a posted entry.  ``reexplain_line`` reverses the entry
the old explanation booked: the original stays posted and stays in every sum, and an opposite entry is posted
beside it.  When new legs are supplied it also posts the replacement in the same transaction, through
``corrections.reverse_entry``.  A change that lands in a closed month is refused by the triggers and by the checks
in front of them, and the refusal says to reopen the period.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.errors import (
    ALLOCATION_INVALID,
    IMMUTABLE,
    OVERALLOCATED,
    PERIOD_LOCKED,
    UNBALANCED,
    HaddonError,
    not_found,
    validation,
)
from haddon.domain.micros import Micros, sum_micros
from haddon import extensions
from haddon.repository import corrections as corrections_repo
from haddon.repository import tables as t
from haddon.repository.audit import AuditAction, record_action
from haddon.repository.ctx import WriteCtx
from haddon.repository.entries import (
    Entry,
    Leg,
    check_open,
    get_entry,
    lock_through,
    post_entry,
    register_allocation_guard,
)

#: The origin recorded on an entry booked from a statement line.
DEFAULT_ORIGIN = "source"

BOOKED, LINKED, IGNORED = "booked", "linked", "ignored"
KINDS = (BOOKED, LINKED, IGNORED)


@dataclass(frozen=True)
class Part:
    """One part of an explanation: how much of the line this posting accounts for."""

    posting_id: str
    amount: str | Micros


@dataclass(frozen=True)
class Allocation:
    id: str
    posting_id: str
    entry_id: str
    amount_micros: int

    def as_json(self) -> dict[str, Any]:
        return {"allocation_id": self.id, "posting_id": self.posting_id, "entry_id": self.entry_id,
                "amount_micros": str(self.amount_micros)}


@dataclass(frozen=True)
class Explanation:
    """One thing somebody said about a line, and what it pointed at."""

    id: str
    line_id: str
    kind: str
    reason: str
    entry_id: str | None
    correction_id: str | None
    supersedes_id: str | None
    allocations: tuple[Allocation, ...]
    created_at: str
    created_by: str

    @property
    def allocated_micros(self) -> int:
        return sum(a.amount_micros for a in self.allocations)

    def as_json(self) -> dict[str, Any]:
        return {"explanation_id": self.id, "line_id": self.line_id, "kind": self.kind, "reason": self.reason,
                "entry_id": self.entry_id, "correction_id": self.correction_id,
                "supersedes_id": self.supersedes_id,
                "allocations": [a.as_json() for a in self.allocations],
                "allocated_micros": str(self.allocated_micros),
                "created_at": self.created_at, "created_by": self.created_by}


@dataclass(frozen=True)
class LineState:
    """What has become of one statement line."""

    line_id: str
    statement_id: str
    statement_live: bool
    line_no: int
    source_key: str
    dt: str
    amount_micros: int
    commodity: str
    account_id: str
    description: str
    reference: str | None
    raw: str
    status: str
    live: Explanation | None
    history: tuple[Explanation, ...]

    @property
    def allocated_micros(self) -> int:
        return 0 if self.live is None else self.live.allocated_micros

    @property
    def unexplained_micros(self) -> int:
        if self.live is not None and self.live.kind == IGNORED:
            return 0
        remaining = abs(self.amount_micros) - abs(self.allocated_micros)
        return remaining if self.amount_micros >= 0 else -remaining

    def as_json(self) -> dict[str, Any]:
        return {"line_id": self.line_id, "statement_id": self.statement_id,
                "statement_live": self.statement_live, "line_no": self.line_no,
                "source_key": self.source_key, "dt": self.dt,
                "amount_micros": str(self.amount_micros), "commodity": self.commodity,
                "account_id": self.account_id, "description": self.description,
                "reference": self.reference, "raw": self.raw, "status": self.status,
                "allocated_micros": str(self.allocated_micros),
                "unexplained_micros": str(self.unexplained_micros),
                "explanation": None if self.live is None else self.live.as_json(),
                "history": [x.as_json() for x in self.history]}


#: The six statuses a line can be in.  ``unexplained`` and ``partly_explained`` are what the close gates look
#: for.  ``superseded_input`` is a line whose statement has been replaced, which is history, not work.
UNEXPLAINED, PARTLY, BOOKED_STATUS, LINKED_STATUS = "unexplained", "partly_explained", "booked", "linked"
IGNORED_STATUS, SUPERSEDED_INPUT = "ignored", "superseded_input"


def _line(conn: Connection, entity_id: str, line_id: str):
    row = conn.execute(
        sa.select(t.statement_lines, t.statements.c.account_id, t.statements.c.commodity,
                  t.statements.c.period_start, t.statements.c.period_end,
                  t.statements.c.document_id)
        .select_from(t.statement_lines.join(t.statements,
                                            sa.and_(t.statements.c.entity_id == t.statement_lines.c.entity_id,
                                                    t.statements.c.id == t.statement_lines.c.statement_id)))
        .where(t.statement_lines.c.entity_id == entity_id, t.statement_lines.c.id == line_id)).first()
    if row is None:
        raise not_found("statement line", line_id)
    return row


def _statement_is_live(conn: Connection, entity_id: str, statement_id: str) -> bool:
    from haddon.repository.statements import is_live  # the one definition of "nothing replaced it"

    return is_live(conn, entity_id, statement_id)


def _allocations_of(conn: Connection, entity_id: str, explanation_ids: Sequence[str]) -> dict[str, list[Allocation]]:
    if not explanation_ids:
        return {}
    rows = conn.execute(
        sa.select(t.allocations, t.postings.c.entry_id)
        .select_from(t.allocations.join(t.postings, sa.and_(t.postings.c.entity_id == t.allocations.c.entity_id,
                                                            t.postings.c.id == t.allocations.c.posting_id)))
        .where(t.allocations.c.entity_id == entity_id,
               t.allocations.c.explanation_id.in_(list(explanation_ids)))
        .order_by(t.allocations.c.created_at, t.allocations.c.id))
    out: dict[str, list[Allocation]] = {}
    for r in rows:
        out.setdefault(r.explanation_id, []).append(
            Allocation(id=r.id, posting_id=r.posting_id, entry_id=r.entry_id,
                       amount_micros=int(r.amount_micros)))
    return out


def explanations_of(conn: Connection, entity_id: str, line_id: str) -> list[Explanation]:
    """Every explanation ever written for this line, in chain order from the first."""
    rows = conn.execute(sa.select(t.explanations)
                        .where(t.explanations.c.entity_id == entity_id,
                               t.explanations.c.line_id == line_id)
                        .order_by(t.explanations.c.created_at, t.explanations.c.id)).all()
    parts = _allocations_of(conn, entity_id, [r.id for r in rows])
    by_id = {r.id: r for r in rows}
    ordered, seen = [], set()
    head = next((r for r in rows if r.supersedes_id is None), None)
    while head is not None and head.id not in seen:
        seen.add(head.id)
        ordered.append(head)
        nxt = next((r for r in rows if r.supersedes_id == head.id), None)
        head = nxt
    ordered.extend(r for r in rows if r.id not in seen)  # defensive: never silently drop a row
    del by_id
    return [Explanation(id=r.id, line_id=r.line_id, kind=r.kind, reason=r.reason, entry_id=r.entry_id,
                        correction_id=r.correction_id, supersedes_id=r.supersedes_id,
                        allocations=tuple(parts.get(r.id, ())), created_at=r.created_at,
                        created_by=r.created_by) for r in ordered]


def live_explanation(conn: Connection, entity_id: str, line_id: str) -> Explanation | None:
    chain = explanations_of(conn, entity_id, line_id)
    superseded = {x.supersedes_id for x in chain if x.supersedes_id}
    live = [x for x in chain if x.id not in superseded]
    return live[-1] if live else None


def line_state(conn: Connection, entity_id: str, line_id: str) -> LineState:
    row = _line(conn, entity_id, line_id)
    chain = explanations_of(conn, entity_id, line_id)
    superseded = {x.supersedes_id for x in chain if x.supersedes_id}
    live = next((x for x in chain if x.id not in superseded), None)
    statement_live = _statement_is_live(conn, entity_id, row.statement_id)
    amount = int(row.amount_micros)
    if not statement_live:
        status = SUPERSEDED_INPUT
    elif live is None:
        status = UNEXPLAINED
    elif live.kind == IGNORED:
        status = IGNORED_STATUS
    elif abs(live.allocated_micros) < abs(amount):
        status = PARTLY
    else:
        status = BOOKED_STATUS if live.kind == BOOKED else LINKED_STATUS
    return LineState(line_id=row.id, statement_id=row.statement_id, statement_live=statement_live,
                     line_no=int(row.line_no), source_key=row.source_key, dt=row.dt,
                     amount_micros=amount, commodity=row.commodity, account_id=row.account_id,
                     description=row.description, reference=row.reference, raw=row.raw, status=status,
                     live=None if not statement_live else live, history=tuple(chain))


def _reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise validation("reason is required: an explanation records why somebody said this", path="reason",
                         reason="required")
    if len(value) > 4096:
        raise validation("reason is longer than 4096 characters", path="reason", reason="too_long")
    return value


def _check_open(conn: Connection, dt: str, *, path: str, what: str) -> None:
    through = lock_through(conn)
    if through is not None and dt <= through:
        raise HaddonError(PERIOD_LOCKED,
                          f"{what} is dated {dt}, on or before the period lock {through}; reopen the period to "
                          f"change how it is explained",
                          details=[{"path": path, "dt": dt, "locked_through": through}])


def _check_allocations_open(conn: Connection, explanation_id: str, *, path: str) -> None:
    """Refuse to retire an explanation whose allocations rest on postings in a closed period.

    Inserting an allocation checks both dates, the line's and the posting's entry's (``allocations_insert``).
    Releasing one needs the same care.  Superseding a linked explanation has no ``entry_id`` to look at, so without
    this an allocation against a posting in a closed month would quietly stop being live, and that month's
    postings would become unallocated without anybody reopening it.  A locked period has to fail closed in both
    directions (issue 0004, pitfall 3).
    """
    through = lock_through(conn)
    if through is None:
        return
    rows = conn.execute(
        sa.select(t.postings.c.id, t.entries.c.id.label("entry_id"), t.entries.c.dt)
        .select_from(t.allocations
                     .join(t.postings, sa.and_(t.postings.c.entity_id == t.allocations.c.entity_id,
                                               t.postings.c.id == t.allocations.c.posting_id))
                     .join(t.entries, sa.and_(t.entries.c.entity_id == t.postings.c.entity_id,
                                              t.entries.c.id == t.postings.c.entry_id)))
        .where(t.allocations.c.explanation_id == explanation_id, t.entries.c.dt <= through)
        .order_by(t.entries.c.dt, t.postings.c.id)).all()
    if rows:
        raise HaddonError(PERIOD_LOCKED,
                          f"this explanation allocates {len(rows)} posting(s) dated on or before the period "
                          f"lock {through}; releasing them would leave a closed month's postings unexplained, "
                          f"so reopen the period first",
                          details=[{"path": path, "posting_id": r.id, "entry_id": r.entry_id, "dt": r.dt,
                                    "locked_through": through, "remedy": "reopen"} for r in rows])


def _require_unexplained(ctx: WriteCtx, row) -> None:
    live = live_explanation(ctx.conn, ctx.entity_id, row.id)
    if live is not None:
        raise HaddonError(IMMUTABLE,
                          f"statement line {row.id} is already explained ({live.kind}); use reexplain_line to "
                          f"say something else about it",
                          details=[{"path": "line_id", "line_id": row.id, "explanation_id": live.id,
                                    "kind": live.kind, "remedy": "reexplain_line"}])


def _require_live_statement(ctx: WriteCtx, row) -> None:
    if not _statement_is_live(ctx.conn, ctx.entity_id, row.statement_id):
        raise HaddonError(IMMUTABLE,
                          f"statement {row.statement_id} has been replaced; explain the lines of the statement "
                          f"that replaced it",
                          details=[{"path": "line_id", "line_id": row.id, "statement_id": row.statement_id}])


def _posting(ctx: WriteCtx, posting_id: str):
    row = ctx.conn.execute(
        sa.select(t.postings, t.entries.c.dt.label("entry_dt"), t.entries.c.status.label("entry_status"))
        .select_from(t.postings.join(t.entries, sa.and_(t.entries.c.entity_id == t.postings.c.entity_id,
                                                        t.entries.c.id == t.postings.c.entry_id)))
        .where(t.postings.c.entity_id == ctx.entity_id, t.postings.c.id == posting_id)).first()
    if row is None:
        raise not_found("posting", posting_id)
    return row


def _allocated_elsewhere(conn: Connection, entity_id: str, posting_id: str, *, excluding: str | None) -> int:
    """How much of this posting live explanations already account for."""
    rows = conn.execute(
        sa.select(t.allocations.c.amount_micros)
        .select_from(t.allocations.join(t.explanations,
                                        sa.and_(t.explanations.c.entity_id == t.allocations.c.entity_id,
                                                t.explanations.c.id == t.allocations.c.explanation_id)))
        .where(t.allocations.c.entity_id == entity_id, t.allocations.c.posting_id == posting_id,
               sa.true() if excluding is None else t.allocations.c.explanation_id != excluding,
               t.live_explanation_clause(entity_id)))
    return sum(abs(int(r.amount_micros)) for r in rows)


def check_parts(ctx: WriteCtx, row, parts: Sequence[Part], *, superseding: str | None = None
                ) -> list[tuple[str, Micros]]:
    """Validate the parts of one explanation against the line and the postings, before anything is written.

    Every refusal the database would raise as ``allocation_invalid`` or ``overallocated`` is raised here first,
    with the arithmetic in the details.  The triggers stay as the guarantee underneath (decisions.md D6), but a
    caller should learn which posting was over-allocated, not merely that one was.
    """
    if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)) or not parts:
        raise validation("at least one part is required: which posting is this line?", path="parts",
                         reason="required")
    line_amount = Micros(int(row.amount_micros))
    checked: list[tuple[str, Micros]] = []
    seen: set[str] = set()
    for i, part in enumerate(parts):
        path = f"parts[{i}]"
        if not isinstance(part, Part):
            raise validation(f"{path} must be a Part", path=path, reason="shape")
        if part.posting_id in seen:
            raise validation(f"{path}.posting_id names posting {part.posting_id} twice; give one amount per "
                             f"posting", path=f"{path}.posting_id", reason="duplicate_posting")
        seen.add(part.posting_id)
        amount = part.amount if isinstance(part.amount, Micros) else \
            Micros.parse(part.amount, path=f"{path}.amount")
        posting = _posting(ctx, part.posting_id)
        if amount.is_zero:
            raise HaddonError(ALLOCATION_INVALID, f"{path}.amount is zero: an allocation explains something",
                              details=[{"path": f"{path}.amount", "posting_id": part.posting_id}])
        if (amount.value > 0) != (line_amount.value > 0):
            raise HaddonError(ALLOCATION_INVALID,
                              f"{path}.amount {amount.render()} has the opposite sign to the line "
                              f"({line_amount.render()}); an allocation apportions the line, it does not "
                              f"negate it",
                              details=[{"path": f"{path}.amount", "posting_id": part.posting_id,
                                        "amount_micros": amount.render(),
                                        "line_micros": line_amount.render()}])
        if (amount.value > 0) != (int(posting.amount_micros) > 0):
            raise HaddonError(ALLOCATION_INVALID,
                              f"{path}.amount {amount.render()} has the opposite sign to posting "
                              f"{part.posting_id} ({posting.amount_micros})",
                              details=[{"path": f"{path}.amount", "posting_id": part.posting_id,
                                        "amount_micros": amount.render(),
                                        "posting_micros": str(int(posting.amount_micros))}])
        if posting.account_id != row.account_id or posting.commodity != row.commodity:
            raise HaddonError(ALLOCATION_INVALID,
                              f"posting {part.posting_id} is {posting.account_id} {posting.commodity}, but the "
                              f"line is {row.account_id} {row.commodity}; a line is the same money as the "
                              f"posting it explains",
                              details=[{"path": f"{path}.posting_id", "posting_id": part.posting_id,
                                        "posting_account_id": posting.account_id,
                                        "posting_commodity": posting.commodity,
                                        "line_account_id": row.account_id, "line_commodity": row.commodity}])
        if posting.entry_status != "posted":
            raise HaddonError(ALLOCATION_INVALID,
                              f"posting {part.posting_id} belongs to entry {posting.entry_id}, which is "
                              f"{posting.entry_status}; a line is explained by the books, not by a draft",
                              details=[{"path": f"{path}.posting_id", "posting_id": part.posting_id,
                                        "entry_id": posting.entry_id, "entry_status": posting.entry_status}])
        _check_open(ctx.conn, posting.entry_dt, path=f"{path}.posting_id",
                    what=f"entry {posting.entry_id!r}")
        already = _allocated_elsewhere(ctx.conn, ctx.entity_id, part.posting_id, excluding=superseding)
        if already + abs(amount.value) > abs(int(posting.amount_micros)):
            raise HaddonError(OVERALLOCATED,
                              f"posting {part.posting_id} is {abs(int(posting.amount_micros))} and already "
                              f"explains {already}; this would take it to {already + abs(amount.value)}",
                              details=[{"path": f"{path}.amount", "posting_id": part.posting_id,
                                        "posting_micros": str(abs(int(posting.amount_micros))),
                                        "already_allocated_micros": str(already),
                                        "requested_micros": str(abs(amount.value))}])
        checked.append((part.posting_id, amount))
    total = sum_micros([amount for _, amount in checked], path="parts")
    if abs(total.value) > abs(line_amount.value):
        raise HaddonError(OVERALLOCATED,
                          f"the parts sum to {total.render()}, which is more than the line's "
                          f"{line_amount.render()}",
                          details=[{"path": "parts", "allocated_micros": total.render(),
                                    "line_micros": line_amount.render(),
                                    "line_id": row.id}])
    return checked


def _contra_legs(row, legs: Sequence[Leg]) -> list[Leg]:
    """The non-cash side of the entry, checked against the line before anything is written.

    The caller does not supply the cash leg.  It is the statement line itself, on the line's own account, for the
    line's own amount.  That makes the booking and the allocation agree by construction, and it is why booking a
    line can never introduce money the statement did not have.
    """
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)) or not legs:
        raise validation("booking a line needs at least one contra leg: what was this line FOR?",
                         path="legs", reason="required")
    for i, leg in enumerate(legs):
        if not isinstance(leg, Leg):
            raise validation(f"legs[{i}] must be a Leg", path=f"legs[{i}]", reason="shape")
        if leg.commodity != row.commodity:
            raise validation(f"legs[{i}].commodity is {leg.commodity!r} but the line is in {row.commodity!r}",
                             path=f"legs[{i}].commodity", reason="commodity_mismatch", value=leg.commodity,
                             expected=row.commodity)
    amounts = [leg.amount if isinstance(leg.amount, Micros) else Micros.parse(leg.amount,
                                                                              path=f"legs[{i}].amount_micros")
               for i, leg in enumerate(legs)]
    total = sum_micros(amounts, path="legs")
    expected = -Micros(int(row.amount_micros))
    if total != expected:
        raise HaddonError(UNBALANCED,
                          f"the contra legs sum to {total.render()}, but the line is "
                          f"{int(row.amount_micros)} so they must sum to {expected.render()}",
                          details=[{"path": "legs", "reason": "booking_does_not_match_the_line",
                                    "line_id": row.id, "legs_micros": total.render(),
                                    "expected_micros": expected.render(),
                                    "difference_micros": (total - expected).render(),
                                    "line_amount_micros": str(int(row.amount_micros))}])
    return [Leg(leg.account_id, leg.commodity, amount, leg.memo, leg.tax_code)
            for leg, amount in zip(legs, amounts)]


def _write_explanation(ctx: WriteCtx, *, row, kind: str, reason: str, entry_id: str | None,
                       correction_id: str | None, supersedes_id: str | None,
                       parts: Sequence[tuple[str, Micros]], id: str | None = None) -> Explanation:
    explanation_id = ctx.insert(t.explanations, id, "x", line_id=row.id, kind=kind, reason=reason,
                                entry_id=entry_id, correction_id=correction_id, supersedes_id=supersedes_id)
    saved = []
    for posting_id, amount in parts:
        allocation_id = ctx.insert(t.allocations, None, "al", explanation_id=explanation_id, line_id=row.id,
                                   posting_id=posting_id, amount_micros=amount.value)
        posting_entry = ctx.conn.execute(sa.select(t.postings.c.entry_id)
                                         .where(t.postings.c.entity_id == ctx.entity_id,
                                                t.postings.c.id == posting_id)).scalar_one()
        saved.append(Allocation(allocation_id, posting_id, posting_entry, amount.value))
    return Explanation(id=explanation_id, line_id=row.id, kind=kind, reason=reason, entry_id=entry_id,
                       correction_id=correction_id, supersedes_id=supersedes_id, allocations=tuple(saved),
                       created_at=ctx.now, created_by=ctx.actor)


@dataclass(frozen=True)
class Explained:
    """One classification act: the line, what was said about it, and the one audit action that recorded it."""

    line_id: str
    explanation: Explanation
    entry: Entry | None
    correction: corrections_repo.Correction | None
    audit: AuditAction

    def as_json(self) -> dict[str, Any]:
        return {"line_id": self.line_id, "explanation": self.explanation.as_json(),
                "entry_id": None if self.entry is None else self.entry.id,
                "correction_id": None if self.correction is None else self.correction.id,
                "audit": self.audit.as_json()}


def _audit(ctx: WriteCtx, *, action: str, row, explanation: Explanation, reason: str,
           input: dict[str, Any]) -> AuditAction:
    return record_action(ctx, action=action, subject_kind="statement_line", subject_id=row.id, reason=reason,
                         result={"line_id": row.id, "statement_id": row.statement_id,
                                 **explanation.as_json()},
                         input=input)


def book_line(ctx: WriteCtx, *, line_id: str, legs: Sequence[Leg], narration: str, reason: str,
              dt: str | None = None, memo: str | None = None, tax_code: str | None = None,
              origin: str = DEFAULT_ORIGIN, code: str = "", comment: str = "", tags: Iterable[Any] = (),
              entry_id: str | None = None, explanation_id: str | None = None) -> Explained:
    """Post the entry this line is, and hold the line against the cash posting it created.

    One call and one audit action.  The entry goes through the ordinary posting path, with every leg, account,
    precision, balance and period-lock check any other booking runs.  Its cash leg is the line itself, so the entry
    balances because it agrees with the statement, not by coincidence.  ``legs`` is the contra side and must sum to
    the negative of the line.  ``dt`` defaults to the line's date.
    """
    reason = _reason(reason)
    row = _line(ctx.conn, ctx.entity_id, line_id)
    _require_live_statement(ctx, row)
    # The explanation reaches into the line's own period, so a closed month refuses first, before any check on
    # the line's own state, which cannot be changed in a closed month either way.
    _check_open(ctx.conn, row.dt, path="line_id", what=f"statement line {row.id!r}")
    _require_unexplained(ctx, row)
    dt = row.dt if dt is None else dt
    check_open(ctx.conn, dt)
    contra = _contra_legs(row, legs)
    cash = Leg(row.account_id, row.commodity, Micros(int(row.amount_micros)), memo, tax_code)
    entry = post_entry(ctx, dt=dt, narration=narration, origin=origin, legs=[cash, *contra], id=entry_id,
                       code=code, comment=comment, tags=tags)
    cash_posting = next(p for p in entry.legs
                        if p.account_id == row.account_id and p.commodity == row.commodity
                        and int(p.amount) == int(row.amount_micros))
    explanation = _write_explanation(ctx, row=row, kind=BOOKED, reason=reason, entry_id=entry.id,
                                     correction_id=None, supersedes_id=None,
                                     parts=[(cash_posting.id, Micros(int(row.amount_micros)))],
                                     id=explanation_id)
    # The document behind the line is evidence for the entry too, which is what makes
    # `GET /provenance?entry_id=...` answer for a booked line without walking the allocations.
    ctx.insert(t.evidence_links, None, "ev", document_id=row.document_id, entry_id=entry.id,
               statement_id=None, page=None, locator=f"line {row.line_no}",
               caption=f"statement line {row.id} dated {row.dt} for {row.account_id} {row.commodity}")
    audit = _audit(ctx, action="line.booked", row=row, explanation=explanation, reason=reason,
                   input={"line_id": row.id, "dt": dt, "narration": narration, "origin": origin,
                          "legs": [{"account_id": leg.account_id, "commodity": leg.commodity,
                                    "amount_micros": leg.amount.render(), "memo": leg.memo,
                                    "tax_code": leg.tax_code} for leg in contra]})
    return Explained(line_id=row.id, explanation=explanation, entry=entry, correction=None, audit=audit)


def link_line(ctx: WriteCtx, *, line_id: str, parts: Sequence[Part], reason: str,
              explanation_id: str | None = None) -> Explained:
    """Say which existing posting(s) this line is, and for how much.  Several parts make a split.

    Nothing is posted and no balance moves, because an allocation points at money the books already hold.  A line
    may be explained for less than its full amount.  That is a partial allocation: ``line_state`` reports it, and
    it blocks the reconciliation close gate until somebody finishes it.
    """
    reason = _reason(reason)
    row = _line(ctx.conn, ctx.entity_id, line_id)
    _require_live_statement(ctx, row)
    _check_open(ctx.conn, row.dt, path="line_id", what=f"statement line {row.id!r}")
    _require_unexplained(ctx, row)
    checked = check_parts(ctx, row, parts)
    explanation = _write_explanation(ctx, row=row, kind=LINKED, reason=reason, entry_id=None,
                                     correction_id=None, supersedes_id=None, parts=checked, id=explanation_id)
    audit = _audit(ctx, action="line.linked", row=row, explanation=explanation, reason=reason,
                   input={"line_id": row.id, "parts": [{"posting_id": p, "amount_micros": a.render()}
                                                       for p, a in checked]})
    return Explained(line_id=row.id, explanation=explanation, entry=None, correction=None, audit=audit)


def ignore_line(ctx: WriteCtx, *, line_id: str, reason: str, explanation_id: str | None = None) -> Explained:
    """Record that this line will not be booked, and why.

    ``reason`` is the whole content of the decision, so it is required, and it is what a reviewer reads later.
    The close gates count an ignored line as explained, which is why the reason has to be worth reading.
    """
    reason = _reason(reason)
    row = _line(ctx.conn, ctx.entity_id, line_id)
    _require_live_statement(ctx, row)
    _check_open(ctx.conn, row.dt, path="line_id", what=f"statement line {row.id!r}")
    _require_unexplained(ctx, row)
    explanation = _write_explanation(ctx, row=row, kind=IGNORED, reason=reason, entry_id=None,
                                     correction_id=None, supersedes_id=None, parts=(), id=explanation_id)
    audit = _audit(ctx, action="line.ignored", row=row, explanation=explanation, reason=reason,
                   input={"line_id": row.id})
    return Explained(line_id=row.id, explanation=explanation, entry=None, correction=None, audit=audit)


def _cash_posting_of(entry: Entry, row):
    """The posting of ``entry`` with this line's account, commodity and amount, or None."""
    return next((p for p in entry.legs
                 if p.account_id == row.account_id and p.commodity == row.commodity
                 and int(p.amount) == int(row.amount_micros)), None)


def _entry_of_posting(ctx: WriteCtx, posting_id: str) -> str:
    return ctx.conn.execute(sa.select(t.postings.c.entry_id)
                            .where(t.postings.c.entity_id == ctx.entity_id,
                                   t.postings.c.id == posting_id)).scalar_one()


def reexplain_line(ctx: WriteCtx, *, line_id: str, kind: str, reason: str,
                   legs: Sequence[Leg] | None = None, parts: Sequence[Part] | None = None,
                   narration: str | None = None, dt: str | None = None, memo: str | None = None,
                   tax_code: str | None = None, origin: str = DEFAULT_ORIGIN, code: str = "",
                   comment: str = "", tags: Iterable[Any] = (),
                   explanation_id: str | None = None) -> Explained:
    """Change your mind about a line: supersede its live explanation with a new one of any kind.

    The old explanation is kept, because it is how the line reached its previous state and a reviewer has to be
    able to see it.  The new one names it.  What happens to the books depends on what the old explanation did:

    * it booked an entry and the new explanation books a replacement: one correction.  The original is mirrored
      and the replacement posted beside it, in this transaction;
    * it booked an entry and the new explanation links or ignores: one reversal.  The money the old booking put
      in the books comes back out, because it was wrong;
    * it linked or ignored and the new explanation books: an ordinary posting.  Nothing was ever posted, so there
      is nothing to correct;
    * it linked or ignored and so does the new one: nothing is posted.

    "The entry it booked" means the one standing today.  If that entry has since been corrected on its own, the
    correction chain is followed to its replacement, and that is what gets corrected or reversed.  Linking the
    line to the standing replacement's own cash posting posts nothing and records the line as booked by it.

    A closed period refuses, and says to reopen it.
    """
    reason = _reason(reason)
    if kind not in KINDS:
        raise validation(f"kind must be one of {', '.join(KINDS)}", path="kind", reason="kind_unknown",
                         value=kind)
    row = _line(ctx.conn, ctx.entity_id, line_id)
    _require_live_statement(ctx, row)
    _check_open(ctx.conn, row.dt, path="line_id", what=f"statement line {row.id!r}")
    live = live_explanation(ctx.conn, ctx.entity_id, row.id)
    if live is None:
        raise HaddonError(IMMUTABLE,
                          f"statement line {row.id} has no explanation to supersede; book, link or ignore it "
                          f"instead",
                          details=[{"path": "line_id", "line_id": row.id,
                                    "remedy": "book_line, link_line or ignore_line"}])
    entry: Entry | None = None
    correction: corrections_repo.Correction | None = None
    checked: list[tuple[str, Micros]] = []
    # The entry the old explanation booked may since have been corrected on its own (``POST /entries/{id}/
    # correct``).  So what is undone is its standing replacement, not the original, which already has its
    # mirror.  ``standing`` stays None when the chain ended in a reversal, or the line never booked anything.
    standing: Entry | None = None
    if live.entry_id is not None:
        if get_entry(ctx.conn, ctx.entity_id, live.entry_id) is None:
            raise not_found("entry", live.entry_id)
        standing_id = corrections_repo.standing_entry(ctx.conn, ctx.entity_id, live.entry_id)
        if standing_id is not None:
            standing = get_entry(ctx.conn, ctx.entity_id, standing_id)
            _check_open(ctx.conn, standing.dt, path="line_id", what=f"entry {standing.id!r}")
    # A `linked` explanation has no entry of its own, so the check above sees nothing.  Its allocations still
    # hold postings, though, and superseding it releases them, so check the postings themselves.
    _check_allocations_open(ctx.conn, live.id, path="line_id")
    recorded_kind = kind
    if kind == BOOKED:
        contra = _contra_legs(row, legs or ())
        cash = Leg(row.account_id, row.commodity, Micros(int(row.amount_micros)), memo, tax_code)
        entry_dt = row.dt if dt is None else dt
        if standing is None:
            # Nothing the line booked still stands (it was linked or ignored, or its booking was reversed), so
            # there is nothing to correct and the new booking is an ordinary posting.
            check_open(ctx.conn, entry_dt)
            entry = post_entry(ctx, dt=entry_dt, narration=narration or row.description, origin=origin,
                               legs=[cash, *contra], code=code, comment=comment, tags=tags)
        else:
            replacement = corrections_repo.Replacement(
                dt=entry_dt, narration=narration or standing.narration, origin=origin,
                legs=[cash, *contra], code=code, comment=comment, tags=tuple(tags))
            correction = corrections_repo.reverse_entry(ctx, original_id=standing.id, dt=standing.dt,
                                                        reason=reason, narration=narration,
                                                        replacement=replacement)
            entry = get_entry(ctx.conn, ctx.entity_id, correction.replacement_id)
        checked = [(_cash_posting_of(entry, row).id, Micros(int(row.amount_micros)))]
    elif kind == LINKED:
        checked = check_parts(ctx, row, parts or (), superseding=live.id)
        onto_standing = standing is not None and any(_entry_of_posting(ctx, p) == standing.id for p, _ in checked)
        if onto_standing:
            # The line is being pointed at the entry its own booking already stands as.  Post nothing, and keep
            # the line as the owner of that entry (recorded as booked, not linked), so a later re-explanation
            # still corrects or reverses it rather than booking the money a second time.
            own_cash = _cash_posting_of(standing, row)
            if own_cash is None or checked != [(own_cash.id, Micros(int(row.amount_micros)))]:
                raise HaddonError(IMMUTABLE,
                                  f"entry {standing.id!r} is what statement line {row.id} booked; it can be "
                                  f"pointed at its cash posting for the whole line, or re-booked, but not "
                                  f"split across",
                                  details=[{"path": "parts", "line_id": row.id, "entry_id": standing.id,
                                            "remedy": "link the whole line to the entry's cash posting, or "
                                                      "reexplain kind=booked"}])
            entry, recorded_kind = standing, BOOKED
        elif standing is not None:
            correction = corrections_repo.reverse_entry(ctx, original_id=standing.id, dt=standing.dt,
                                                        reason=reason, narration=narration)
    else:  # IGNORED
        if standing is not None:
            correction = corrections_repo.reverse_entry(ctx, original_id=standing.id, dt=standing.dt,
                                                        reason=reason, narration=narration)
    explanation = _write_explanation(
        ctx, row=row, kind=recorded_kind, reason=reason,
        entry_id=None if entry is None else entry.id,
        correction_id=None if correction is None else correction.id, supersedes_id=live.id, parts=checked,
        id=explanation_id)
    audit = _audit(ctx, action="line.reexplained", row=row, explanation=explanation, reason=reason,
                   input={"line_id": row.id, "kind": kind, "supersedes_id": live.id,
                          "parts": [{"posting_id": p, "amount_micros": a.render()} for p, a in checked],
                          "legs": None if legs is None else
                          [{"account_id": leg.account_id, "commodity": leg.commodity,
                            "amount_micros": str(leg.amount), "memo": leg.memo,
                            "tax_code": leg.tax_code} for leg in legs]})
    return Explained(line_id=row.id, explanation=explanation, entry=entry, correction=correction, audit=audit)


def allocations_of_postings(conn: Connection, entity_id: str, posting_ids: Sequence[str]) -> dict[str, list[dict]]:
    """The live allocations against each of these postings, for the "what explains this entry?" direction."""
    if not posting_ids:
        return {}
    rows = conn.execute(
        sa.select(t.allocations, t.explanations.c.kind, t.statement_lines.c.dt,
                  t.statement_lines.c.description, t.statement_lines.c.statement_id)
        .select_from(t.allocations.join(t.explanations,
                                        sa.and_(t.explanations.c.entity_id == t.allocations.c.entity_id,
                                                t.explanations.c.id == t.allocations.c.explanation_id))
                     .join(t.statement_lines,
                           sa.and_(t.statement_lines.c.entity_id == t.allocations.c.entity_id,
                                   t.statement_lines.c.id == t.allocations.c.line_id)))
        .where(t.allocations.c.entity_id == entity_id, t.allocations.c.posting_id.in_(list(posting_ids)),
               t.live_explanation_clause(entity_id))
        .order_by(t.allocations.c.created_at, t.allocations.c.id))
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.posting_id, []).append(
            {"allocation_id": r.id, "explanation_id": r.explanation_id, "kind": r.kind, "line_id": r.line_id,
             "statement_id": r.statement_id, "dt": r.dt, "description": r.description,
             "amount_micros": str(int(r.amount_micros))})
    return out


def _allocated_postings_of_entry(ctx: WriteCtx, entry_id: str) -> set[str]:
    """The postings of one entry that a live explanation points at.  This is the core's draft-revision guard
    (issue 0004).

    ``entries.revise_draft`` must refuse to move a posting an allocation rests on, but the journal core may not
    know what an allocation is.  So the check lives here and the core calls it through
    ``entries.register_allocation_guard``.  With the reconciliation extension absent the core finds no guard and a
    draft revises freely.

    The switch is checked here rather than at registration.  ``haddon.api.app`` imports the line routes
    unconditionally, so importing the API registers this guard whatever ``HADDON_RECONCILIATION`` says, and the
    switch can be flipped after import.  A guard that ran anyway would have the core querying ``allocations`` and
    ``explanations`` with the extension off, which against a database without those tables is an
    ``OperationalError``.  So the extension itself answers "not running" (issue 0004's exit gate 6).
    """
    if not extensions.enabled(extensions.RECONCILIATION):
        return set()
    rows = ctx.conn.execute(
        sa.select(t.allocations.c.posting_id)
        .select_from(t.allocations.join(t.explanations,
                                        sa.and_(t.explanations.c.entity_id == t.allocations.c.entity_id,
                                                t.explanations.c.id == t.allocations.c.explanation_id))
                     .join(t.postings, sa.and_(t.postings.c.entity_id == t.allocations.c.entity_id,
                                               t.postings.c.id == t.allocations.c.posting_id)))
        .where(t.allocations.c.entity_id == ctx.entity_id, t.postings.c.entry_id == entry_id,
               t.live_explanation_clause(ctx.entity_id)))
    return {r.posting_id for r in rows}


register_allocation_guard(_allocated_postings_of_entry)


def follow_correction(ctx: WriteCtx, correction: corrections_repo.Correction, reason: str) -> list[dict]:
    """Move a line booked to a corrected entry onto the replacement, in the correction's own transaction.

    ``POST /entries/{id}/correct`` is the core's command and knows nothing about statement lines.  Without this, a
    line that booked the original would be left pointing at an entry that now has a mirror: still counted as
    explained, shown as unbooked, and impossible to re-explain.  So the line gets a superseding ``booked``
    explanation naming the replacement and holding its cash posting, which is what
    ``reexplain_line(kind="booked")`` would have written.  The old explanation is kept and nothing is rewritten.

    A replacement that no longer carries the line's money (its account, commodity and amount) cannot be what the
    line booked, so the whole correction is refused and pointed at ``reexplain``, which handles the line and the
    entry together.  A plain reversal moves nothing.  The line still says what it booked, and re-explaining it
    now posts afresh.
    """
    if not extensions.enabled(extensions.RECONCILIATION) or correction.replacement_id is None:
        return []
    chain = corrections_repo.replaced_chain(ctx.conn, ctx.entity_id, correction.original_id)
    rows = ctx.conn.execute(
        sa.select(t.explanations.c.id, t.explanations.c.line_id)
        .where(t.explanations.c.entity_id == ctx.entity_id, t.explanations.c.kind == BOOKED,
               t.explanations.c.entry_id.in_(chain), t.live_explanation_clause(ctx.entity_id))
        .order_by(t.explanations.c.created_at, t.explanations.c.id)).all()
    if not rows:
        return []
    replacement = get_entry(ctx.conn, ctx.entity_id, correction.replacement_id)
    moved = []
    for r in rows:
        row = _line(ctx.conn, ctx.entity_id, r.line_id)
        cash = _cash_posting_of(replacement, row)
        if cash is None:
            raise HaddonError(IMMUTABLE,
                              f"entry {correction.original_id!r} is what statement line {row.id} booked, and the "
                              f"replacement no longer carries that line's {row.account_id} {row.commodity} "
                              f"{int(row.amount_micros)}; re-explain the line instead, which corrects the "
                              f"entry and the line together",
                              details=[{"path": "replacement.legs", "line_id": row.id,
                                        "entry_id": correction.original_id,
                                        "remedy": f"POST /lines/{row.id}/reexplain"}])
        _check_open(ctx.conn, row.dt, path="line_id", what=f"statement line {row.id!r}")
        _check_allocations_open(ctx.conn, r.id, path="line_id")
        checked = check_parts(ctx, row, [Part(cash.id, Micros(int(row.amount_micros)))], superseding=r.id)
        explanation = _write_explanation(ctx, row=row, kind=BOOKED, reason=reason, entry_id=replacement.id,
                                         correction_id=correction.id, supersedes_id=r.id, parts=checked)
        moved.append({"line_id": row.id, "statement_id": row.statement_id, **explanation.as_json()})
    return moved


corrections_repo.register_correction_follower(follow_correction)
