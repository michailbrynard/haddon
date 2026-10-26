"""The one query behind every report: posted postings of one continuous journal, in date/seq order.

The range is optional on either side (``start=None`` is "from the beginning", ``end=None`` is "through the
latest").  Only ``status='posted'`` entries are official (never draft, never void).  Accumulation uses
``sum_micros`` (Python ints, range-checked), never SQLite ``SUM``: an aggregate that leaves the signed 64-bit
range is a 422 ``overflow``, not a wrapped integer, a REAL, or a database error.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Row

from haddon.domain.micros import Micros, sum_micros
from haddon.repository import tables as t
from haddon.repository.provisioning import Account, all_accounts
from haddon.reports.types import Line

OPENING = "opening"
BALANCE_SHEET_TYPES = ("asset", "liability", "equity")
PNL_TYPES = ("income", "expense")


def posted_postings(conn: Connection, entity_id: str, *, start: str | None = None, end: str | None = None,
                    account_id: str | None = None, include_opening: bool = True) -> list[Row]:
    """Return posted postings between ``start`` and ``end`` inclusive; either bound may be ``None``.

    Rows are ordered by entry date, entry creation time, entry id and leg seq.  That order is durable, whereas
    SQLite rowids are not stable across VACUUM.
    """
    e, p = t.entries, t.postings
    stmt = (sa.select(p.c.account_id, p.c.commodity, p.c.amount_micros, p.c.seq, p.c.memo, p.c.tax_code,
                      e.c.id.label("entry_id"), e.c.dt, e.c.narration, e.c.origin,
                      # E1 (migration 0002): the transaction-level note fields travel with the row, so the export
                      # and the oracle read them through the same one query as everything else.
                      e.c.code, e.c.comment, e.c.tags)
            .select_from(p.join(e, p.c.entry_id == e.c.id))
            .where(p.c.entity_id == entity_id, e.c.entity_id == entity_id,
                   e.c.status == "posted")
            .order_by(e.c.dt, e.c.created_at, e.c.id, p.c.seq))
    if start is not None:
        stmt = stmt.where(e.c.dt >= start)
    if end is not None:
        stmt = stmt.where(e.c.dt <= end)
    if account_id is not None:
        stmt = stmt.where(p.c.account_id == account_id)
    if not include_opening:
        stmt = stmt.where(e.c.origin != OPENING)
    return list(conn.execute(stmt))


def sum_lines(rows: Iterable[Row], accounts: dict[str, Account], *, include_zero: bool = False) -> tuple[Line, ...]:
    """Sum ``amount_micros`` per (account, commodity) in Python ints, ordered by account path then commodity."""
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in rows:
        buckets[(r.account_id, r.commodity)].append(int(r.amount_micros))
    lines = []
    for (aid, com), amounts in buckets.items():
        total = sum_micros(amounts, path=f"{accounts[aid].path}[{com}]")
        if include_zero or not total.is_zero:
            lines.append(Line(aid, accounts[aid].path, accounts[aid].type, com, total))
    return tuple(sorted(lines, key=lambda ln: (ln.path, ln.commodity)))


def accounts_of(conn: Connection, entity_id: str) -> dict[str, Account]:
    """The chart of this entity."""
    return all_accounts(conn, entity_id)


def net_by_commodity(lines: Iterable[Line]) -> dict[str, Micros]:
    per: dict[str, list[Micros]] = defaultdict(list)
    for ln in lines:
        per[ln.commodity].append(ln.amount)
    return {c: sum_micros(v, path=f"net[{c}]") for c, v in sorted(per.items())}
