"""Register: postings in date/seq order, optionally for one account with a running balance.

A journal is continuous (issue 0002), so any ``[start, end]`` range is valid and there is no cross-year refusal.
When the register is for one account the running balance is still accumulated from the beginning of the journal,
not from ``start``: a range is a window onto the ledger, not a reset of it.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.engine import Connection

from haddon.domain.errors import ACCOUNT_UNKNOWN, HaddonError
from haddon.domain.micros import Micros, sum_micros
from haddon.reports.postings import accounts_of, posted_postings
from haddon.reports.scope import scope
from haddon.reports.types import Register, RegisterLine


def register(conn: Connection, entity_id: str, *, account_id: str | None = None, start: str | None = None,
             end: str | None = None) -> Register:
    sc = scope(conn, entity_id, start=start, end=end)
    accounts = accounts_of(conn, entity_id)
    if account_id is not None and account_id not in accounts:
        raise HaddonError(ACCOUNT_UNKNOWN, f"account {account_id!r} not found", details=[{"path": "account_id", "id": account_id}])
    # The running total needs every posting up to `end`, including those before `start`; only the display is windowed.
    rows = posted_postings(conn, entity_id, start=None, end=sc.end, account_id=account_id)
    running: dict[str, int] = defaultdict(int)  # Python ints: a running total past int64 is a 422 overflow, not a wrap
    lines = []
    for r in rows:
        amount = Micros(int(r.amount_micros))
        bal: Micros | None = None
        if account_id is not None:
            running[r.commodity] += amount.value
            bal = sum_micros([running[r.commodity]], path=f"running[{r.commodity}]")
        if sc.start is not None and r.dt < sc.start:
            continue
        lines.append(RegisterLine(r.entry_id, r.dt, r.seq, r.narration, r.origin, r.account_id, accounts[r.account_id].path,
                                  r.commodity, amount, r.memo, r.tax_code, bal))
    return Register(sc.start, sc.end, account_id, tuple(lines))
