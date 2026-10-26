"""An opening restatement imported onto a journal that already reaches back past it (issue 0002).

A corpus is a set of self-contained annual books.  Each year's ``<year>-opening.journal`` restates the previous
year's closing balance sheet as absolute amounts (``assets:cash:bank  4183.02 EUR``), with that year's net result
folded into a retained-earnings account, so the year can be read on its own.  The opening exists because a reader
of the annual book does not have the year before.

A continuous journal does have the year before.  Posting the restatement on top of it books every balance-sheet
account a second time, and nothing catches it: the restatement balances per commodity, so debits still equal
credits while every balance-sheet figure doubles.

So a restatement of a period the journal already holds is not posted.  It is verified against the continuous
position and recorded in the import event, which puts the redundant data to the one use it still has: an
independent check that the continuous journal agrees with the annual book it came from.  A restatement that
disagrees ends the whole import.  Nothing is skipped silently.

The identity.  In the stored sign convention assets and expenses are positive; liabilities, equity and income
are negative; and every posted entry balances per commodity.  So for any date

    A + L + E + P&L = 0.

The restatement carries balance-sheet lines only and balances on its own, so A' + L' + E' = 0.  It states the
same assets and liabilities as the continuous journal and differs only where the result was folded, so the
folded amount must be exactly the journal's accumulated result at the boundary:

    fold = -(A + L + E) = P&L.

:func:`verify_restatement` checks this per commodity and per account.  The balance sheet uses the same identity
to make its two sides meet (``haddon.statements.shape``), so a book that fails here would also print a sheet that
does not balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.engine import Connection

from haddon.domain.errors import UNBALANCED, HaddonError
from haddon.domain.micros import ZERO, Micros, sum_micros
from haddon.reports.balances import trial_balance
from haddon.reports.postings import BALANCE_SHEET_TYPES, PNL_TYPES, accounts_of

#: the only account type a restatement may differ from the journal on: the prior result is folded into equity
FOLD_TYPE = "equity"


@dataclass(frozen=True)
class Restatement:
    """One opening restatement that was verified instead of posted."""

    dt: str
    narration: str
    through: str                       # the continuous position it was compared against
    lines: int                         # legs the restatement carried
    fold_accounts: tuple[str, ...]     # the account id(s) the prior result was folded into
    fold_paths: tuple[str, ...]        # the same accounts by path, which is what a reader and a report key on
    folded: dict[str, str]             # commodity -> micros folded, which must equal the accumulated result
    compared: int                      # (account, commodity) pairs compared exactly

    def as_json(self) -> dict[str, Any]:
        return {"dt": self.dt, "narration": self.narration, "through": self.through, "lines": self.lines,
                "fold_accounts": list(self.fold_accounts), "fold_paths": list(self.fold_paths),
                "folded": self.folded, "compared": self.compared}


def day_before(dt: str) -> str:
    """The calendar day before ``dt``.  An opening dated 1 January is compared with 31 December."""
    return (date.fromisoformat(dt) - timedelta(days=1)).isoformat()


def verify_restatement(conn: Connection, entity_id: str, *, dt: str, narration: str,
                       stated: dict[tuple[str, str], Micros]) -> Restatement:
    """Check an opening restatement against the continuous position at ``day_before(dt)`` and describe it.

    ``stated`` is the restatement's own (account_id, commodity) -> amount, already converted to micros.  Every
    difference from the continuous position must sit on an equity account, and the differences must sum, per
    commodity, to exactly the accumulated income and expense of the journal so far.  Anything else (a balance
    that moved, a liability that absorbed part of the fold, an income account carried forward) raises
    :class:`HaddonError` ``unbalanced`` naming each account, and the whole import rolls back.
    """
    through = day_before(dt)
    accounts = accounts_of(conn, entity_id)
    position = trial_balance(conn, entity_id, through=through, include_zero=False)
    held = {(ln.account_id, ln.commodity): ln.amount for ln in position.lines if ln.type in BALANCE_SHEET_TYPES}
    result = _net_by_commodity(position.lines, PNL_TYPES)
    differences: dict[tuple[str, str], Micros] = {}
    for key in set(stated) | set(held):
        delta = stated.get(key, ZERO) - held.get(key, ZERO)
        if not delta.is_zero:
            differences[key] = delta

    problems = []
    for (account_id, commodity), delta in sorted(differences.items(), key=lambda kv: (accounts[kv[0][0]].path, kv[0][1])):
        if accounts[account_id].type != FOLD_TYPE:
            problems.append({"account_id": account_id, "path": accounts[account_id].path, "commodity": commodity,
                             "stated": stated.get((account_id, commodity), ZERO).render(),
                             "held": held.get((account_id, commodity), ZERO).render(),
                             "difference": delta.render(), "reason": "not_an_equity_account"})
    folded: dict[str, list[Micros]] = {}
    for (account_id, commodity), delta in differences.items():
        folded.setdefault(commodity, []).append(delta)
    net_folded = {c: sum_micros(v, path=f"folded[{c}]") for c, v in folded.items()}
    for commodity in sorted(set(net_folded) | set(c for c, a in result.items() if not a.is_zero)):
        want, got = result.get(commodity, ZERO), net_folded.get(commodity, ZERO)
        if want != got:
            problems.append({"commodity": commodity, "accumulated_result": want.render(), "folded": got.render(),
                             "reason": "fold_is_not_the_accumulated_result"})
    if problems:
        raise HaddonError(UNBALANCED,
                          f"the opening restatement dated {dt} does not agree with the journal through {through}: a "
                          f"restatement of a period the journal already holds may differ from it only by the "
                          f"accumulated result folded into equity",
                          details=problems)
    fold = sorted({a for a, _ in differences})
    return Restatement(dt=dt, narration=narration, through=through, lines=len(stated),
                       fold_accounts=tuple(fold), fold_paths=tuple(accounts[a].path for a in fold),
                       folded={c: a.render() for c, a in sorted(net_folded.items())},
                       compared=len(set(stated) | set(held)))


def _net_by_commodity(lines, types) -> dict[str, Micros]:
    """The sum of the lines whose account type is in ``types``, per commodity."""
    per: dict[str, list[Micros]] = {}
    for ln in lines:
        if ln.type in types:
            per.setdefault(ln.commodity, []).append(ln.amount)
    return {c: sum_micros(v, path=f"result[{c}]") for c, v in sorted(per.items())}
