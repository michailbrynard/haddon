"""Balances and movements over a continuous journal: trial balance, account balance, movements, income statement.

A balance is every posted amount up to a date (openings included), because a journal is one continuous ledger
(issue 0002).  A movement is the postings inside a range, openings excluded.  A balance-sheet report therefore
needs no carry row and an income statement is just movements of the P&L accounts.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.engine import Connection

from haddon.domain.errors import ACCOUNT_UNKNOWN, HaddonError, validation
from haddon.domain.micros import Micros
from haddon.repository.provisioning import ACCOUNT_TYPES, get_commodity
from haddon.reports.postings import PNL_TYPES, accounts_of, net_by_commodity, posted_postings, sum_lines
from haddon.reports.scope import scope
from haddon.reports.types import IncomeStatement, Movements, TrialBalance


def trial_balance(conn: Connection, entity_id: str, *, through: str | None = None,
                  include_zero: bool = False) -> TrialBalance:
    """Balance per account and commodity over every posted entry dated on or before ``through``.

    ``through`` defaults to all history.  Opening entries are included.  Pairs that net to zero are omitted unless
    ``include_zero``; pairs with no posting at all never appear.
    """
    sc = scope(conn, entity_id, end=through)
    rows = posted_postings(conn, entity_id, start=sc.start, end=sc.end)
    lines = sum_lines(rows, accounts_of(conn, entity_id), include_zero=include_zero)
    return TrialBalance(sc.end, lines)


def account_balance(conn: Connection, entity_id: str, *, account_id: str, commodity: str,
                    through: str | None = None) -> Micros:
    """Return one account's balance in one commodity through ``through`` (``ZERO`` if nothing posted)."""
    sc = scope(conn, entity_id, end=through)
    accounts = accounts_of(conn, entity_id)
    if account_id not in accounts:
        raise HaddonError(ACCOUNT_UNKNOWN, f"account {account_id!r} not found", details=[{"path": "account_id", "id": account_id}])
    if get_commodity(conn, entity_id, commodity) is None:
        raise validation(f"unknown commodity {commodity!r}", path="commodity", reason="commodity_unknown", value=commodity)
    rows = posted_postings(conn, entity_id, start=sc.start, end=sc.end, account_id=account_id)
    lines = sum_lines((r for r in rows if r.commodity == commodity), accounts, include_zero=True)
    return lines[0].amount if lines else Micros(0)


def movements(conn: Connection, entity_id: str, *, start: str | None = None, end: str | None = None,
              account_types: Sequence[str] = ACCOUNT_TYPES, include_zero: bool = False) -> Movements:
    """Period movements between ``start`` and ``end`` (defaults: all history).  Opening entries are excluded."""
    _check_types(account_types)
    sc = scope(conn, entity_id, start=start, end=end)
    accounts = accounts_of(conn, entity_id)
    rows = posted_postings(conn, entity_id, start=sc.start, end=sc.end, include_opening=False)
    lines = sum_lines((r for r in rows if accounts[r.account_id].type in account_types), accounts, include_zero=include_zero)
    return Movements(sc.start, sc.end, lines)


def income_statement(conn: Connection, entity_id: str, *, start: str | None = None,
                     end: str | None = None) -> IncomeStatement:
    """Income and expense movements over a range (openings excluded) and their per-commodity net."""
    mv = movements(conn, entity_id, start=start, end=end, account_types=PNL_TYPES)
    return IncomeStatement(mv.start, mv.end, mv.lines, net_by_commodity(mv.lines))


def _check_types(account_types: Sequence[str]) -> None:
    bad = [x for x in account_types if x not in ACCOUNT_TYPES]
    if bad or not account_types:
        raise validation(f"account_types must be a non-empty subset of {ACCOUNT_TYPES}", path="account_types", reason="enum", value=list(account_types))
