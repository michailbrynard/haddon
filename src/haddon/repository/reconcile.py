"""Bank against books: for one cash account and commodity, what the statement says and what the ledger says.

This is the comparison a person reviewing a month actually acts on.  ``close_checks`` computes a version of it,
but only where a printed closing balance was recorded for exactly that date, and only as one outstanding item among
however many the close window produces.  A reviewer wants the same arithmetic at any date, whether or not the bank
printed a balance there.

So the statement side is derived rather than looked up.  A statement declares its opening balance and every line
it carries is a movement, so what the bank says the account held at ``as_of`` is that opening plus every line up to
and including that date.  Where the bank did print a balance for the date, it is reported beside the derived
figure.  Two independent sources agreeing is worth showing, and disagreeing is worth knowing.

``difference`` is ledger minus statement, the same direction ``close_checks`` uses, so a positive difference means
the books hold more than the bank does.

It also answers the other half of reconciliation, which the balance comparison does not (issue 0004, pitfall 2):
how much of what the bank printed the journal has explained.  A statement can agree to the penny and still have
lines nobody has classified, so ``unexplained_lines`` and ``unexplained_micros`` sit beside the difference rather
than being folded into it.

Nothing here is specific to a bank or an entity.  It reads ``statements``, ``statement_lines`` and
``explanations`` for whatever cash accounts the chart has, and works the same for a card, a loan or a custodial
wallet.

This lives here rather than in ``haddon.reports`` because that package holds reports over date ranges of the
continuous journal (issue 0002).  A reconciliation compares a statement record against the ledger at a date, and
the date is the subject rather than a narrowing of some period.  Its nearest relative is ``close_checks``, which
lives here for the same reason.  The ledger side comes from ``reports.balances.account_balance``, read through the
date.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.micros import Micros
from haddon.repository import tables as t


@dataclass(frozen=True)
class Coverage:
    """A span of the period no loaded statement covers: days nothing is known about."""

    start: str
    end: str

    def as_json(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class AccountReconciliation:
    """One cash account and commodity, compared at one date."""

    account_id: str
    account_path: str
    commodity: str
    as_of: str
    statement_micros: Micros | None      # opening + every loaded line through as_of; None when nothing is loaded
    ledger_micros: Micros | None         # the posted book balance at as_of; None when it cannot be computed
    asserted_micros: Micros | None       # a printed closing balance for exactly as_of, when one was recorded
    statement_ids: tuple[str, ...]
    uncovered: tuple[Coverage, ...]
    line_count: int = 0                  # loaded lines through as_of
    unexplained_lines: int = 0           # of those, the ones with no live explanation
    unexplained_micros: Micros | None = None

    @property
    def difference(self) -> Micros | None:
        if self.statement_micros is None or self.ledger_micros is None:
            return None
        return self.ledger_micros - self.statement_micros

    @property
    def agrees(self) -> bool:
        d = self.difference
        return d is not None and d.value == 0

    @property
    def explained(self) -> bool:
        """Whether every loaded line has a live explanation.

        This is a different question from ``agrees``.  A statement can balance to the penny with nothing classified
        at all."""
        return self.unexplained_lines == 0

    def as_json(self) -> dict:
        def m(value: Micros | None) -> str | None:
            return None if value is None else value.render()

        return {"account_id": self.account_id, "account_path": self.account_path, "commodity": self.commodity,
                "as_of": self.as_of, "statement_micros": m(self.statement_micros),
                "ledger_micros": m(self.ledger_micros), "asserted_micros": m(self.asserted_micros),
                "difference_micros": m(self.difference), "agrees": self.agrees,
                "statement_ids": list(self.statement_ids),
                "line_count": self.line_count, "unexplained_lines": self.unexplained_lines,
                "unexplained_micros": m(self.unexplained_micros), "explained": self.explained,
                "uncovered": [c.as_json() for c in self.uncovered]}


@dataclass(frozen=True)
class Reconciliation:
    entity_id: str
    as_of: str
    start: str
    accounts: tuple[AccountReconciliation, ...]

    @property
    def agrees(self) -> bool:
        """True only when every account that can be compared agrees, and none is uncomparable."""
        return bool(self.accounts) and all(a.agrees for a in self.accounts)

    @property
    def explained(self) -> bool:
        return bool(self.accounts) and all(a.explained for a in self.accounts)

    def as_json(self) -> dict:
        return {"as_of": self.as_of, "start": self.start, "agrees": self.agrees,
                "explained": self.explained, "accounts": [a.as_json() for a in self.accounts]}


def _cash_pairs(conn: Connection, entity_id: str) -> list[tuple[str, str, str]]:
    """``(account_id, path, commodity)`` for every cash account in the chart, archived included.

    The same set ``close_checks`` demands statements for, and for the same reason: an account closed mid-year
    still holds the months before it closed.
    """
    rows = conn.execute(
        sa.select(t.accounts.c.id, t.accounts.c.path, t.account_commodities.c.commodity)
        .select_from(t.accounts.join(t.account_commodities,
                                     sa.and_(t.account_commodities.c.entity_id == t.accounts.c.entity_id,
                                             t.account_commodities.c.account_id == t.accounts.c.id)))
        .where(t.accounts.c.entity_id == entity_id, t.accounts.c.is_cash == 1)
        .order_by(t.accounts.c.path, t.account_commodities.c.commodity))
    return [(r.id, r.path, r.commodity) for r in rows]


def _statements(conn: Connection, entity_id: str, account_id: str, commodity: str):
    """Every live statement for this account and commodity, earliest period first."""
    rows = conn.execute(
        sa.select(t.statements.c.id, t.statements.c.period_start, t.statements.c.period_end,
                  t.statements.c.opening_micros, t.statements.c.closing_micros)
        .where(t.statements.c.entity_id == entity_id, t.statements.c.account_id == account_id,
               t.statements.c.commodity == commodity, t.live_statement_clause())
        .order_by(t.statements.c.period_start, t.statements.c.id)).all()
    return rows


def _asserted(conn: Connection, entity_id: str, account_id: str, commodity: str, as_of: str) -> Micros | None:
    """A printed closing balance recorded for exactly ``as_of``, if the bank gave one there."""
    row = conn.execute(
        sa.select(t.statements.c.closing_micros)
        .where(t.statements.c.entity_id == entity_id, t.statements.c.account_id == account_id,
               t.statements.c.commodity == commodity, t.statements.c.period_end == as_of,
               t.live_statement_clause())
        .order_by(t.statements.c.id.desc()).limit(1)).first()
    return None if row is None else Micros(int(row.closing_micros))


def _line_totals(conn: Connection, entity_id: str, statement_ids, as_of: str) -> tuple[int, int, int, int]:
    """``(moved, line_count, unexplained_lines, unexplained_micros)`` over the lines through ``as_of``.

    "Unexplained" means no live explanation at all.  A partly allocated line is explained but incomplete, and that
    is the close gate's business (``nothing_partial``), not this one's.
    """
    if not statement_ids:
        return 0, 0, 0, 0
    rows = conn.execute(
        sa.select(t.statement_lines.c.id, t.statement_lines.c.amount_micros,
                  sa.exists().where(sa.and_(t.explanations.c.entity_id == entity_id,
                                            t.explanations.c.line_id == t.statement_lines.c.id,
                                            t.live_explanation_clause(entity_id))).label("explained"))
        .where(t.statement_lines.c.entity_id == entity_id,
               t.statement_lines.c.statement_id.in_(list(statement_ids)),
               t.statement_lines.c.dt <= as_of)).all()
    moved = sum(int(r.amount_micros) for r in rows)
    open_rows = [r for r in rows if not r.explained]
    return moved, len(rows), len(open_rows), sum(int(r.amount_micros) for r in open_rows)


def _ledger(conn: Connection, entity_id: str, account_id: str, commodity: str, as_of: str) -> Micros | None:
    """The posted book balance at ``as_of``, or ``None`` when it cannot be computed.

    ``None``, never zero.  "The balance cannot be read" and "the balance is nothing" are different answers, and a
    reconciliation that printed 0.00 for the first would be wrong.
    """
    from haddon.reports.balances import account_balance  # local: reports imports the repository

    try:
        return account_balance(conn, entity_id, account_id=account_id, commodity=commodity, through=as_of)
    except Exception:  # noqa: BLE001 - any failure is "unknown", and unknown is not agreement
        return None


def uncovered_spans(start: str, end: str, intervals) -> tuple[Coverage, ...]:
    """The same gap arithmetic ``close_checks`` uses, so the two can never disagree about coverage."""
    from haddon.repository.close_checks import uncovered

    return tuple(Coverage(s, e) for s, e in uncovered(start, end, intervals))


def reconcile(conn: Connection, entity_id: str, *, as_of: str, start: str | None = None) -> Reconciliation:
    """Compare bank against books at ``as_of`` for every cash account and commodity in the chart.

    ``start`` bounds only the coverage question (which days of the period have no statement loaded) and defaults
    to the first day of ``as_of``'s month.  The balances are always cumulative to ``as_of``.  A balance is not a
    period figure, and treating it as one is how reconciliations come out wrong.

    The statement side runs from the earliest loaded statement's declared opening.  Where the loaded statements
    leave a gap, the running figure is short by that much, which is what ``uncovered`` reports.  Read the two
    together: a difference beside a gap means a missing statement rather than a bad book.
    """
    window_start = start or f"{as_of[:7]}-01"
    out: list[AccountReconciliation] = []
    for account_id, path, commodity in _cash_pairs(conn, entity_id):
        loaded = _statements(conn, entity_id, account_id, commodity)
        covering = [s for s in loaded if s.period_start <= as_of]
        statement_micros: Micros | None = None
        moved, line_count, open_lines, open_micros = _line_totals(
            conn, entity_id, [s.id for s in covering], as_of)
        if covering:
            statement_micros = Micros(int(covering[0].opening_micros) + moved)
        out.append(AccountReconciliation(
            account_id=account_id, account_path=path, commodity=commodity, as_of=as_of,
            statement_micros=statement_micros,
            ledger_micros=_ledger(conn, entity_id, account_id, commodity, as_of),
            asserted_micros=_asserted(conn, entity_id, account_id, commodity, as_of),
            statement_ids=tuple(s.id for s in covering),
            uncovered=uncovered_spans(window_start, as_of,
                                      [(s.period_start, s.period_end) for s in loaded]),
            line_count=line_count, unexplained_lines=open_lines,
            unexplained_micros=None if not covering else Micros(open_micros)))
    return Reconciliation(entity_id=entity_id, as_of=as_of, start=window_start, accounts=tuple(out))
