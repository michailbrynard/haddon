"""What a period's figures were built from: posted entries by origin, and the journals they were imported from.

Every other report in this package answers "what do the books say".  This one answers the question a reviewer
asks before believing that answer: is everything that belongs in this period actually in it?  It answers only
with things the record itself states.

Two facts, both counted straight out of the store:

* ``origins``: posted entries in the period per ``entries.origin``.  The vocabulary
  (``haddon.repository.entries.ORIGINS``) names the kinds of journal a book is made of (``invoice``,
  ``payroll``, ``adjustment``, ``source`` and the rest), so an origin that is absent from a period is a
  statement about that period, not an inference about it.
* ``journals``: the source journals those entries were imported from (``journal_mappings``), per path.  An
  entry booked from a loaded bank statement has no journal mapping, so a period whose entries are all bank-side
  shows entry counts and no journals, which is exactly the shape of a half-loaded book.

Nothing here decides whether a period is complete.  A count is reported; what a missing origin means is the
reader's to judge, because a company with no payroll has an empty payroll journal quite legitimately.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.repository import tables as t
from haddon.reports.scope import scope


@dataclass(frozen=True)
class Composition:
    """Counts of what is posted in one period of the continuous journal."""

    start: str | None
    end: str | None
    origins: dict[str, int]   # entries.origin -> posted entries dated in the period
    journals: dict[str, int]  # journal_mappings.journal_path -> those entries that came from an imported journal

    @property
    def entry_count(self) -> int:
        return sum(self.origins.values())


def composition(conn: Connection, entity_id: str, *, start: str | None = None,
                end: str | None = None) -> Composition:
    """Count posted entries between ``start`` and ``end`` by origin and by the journal each was imported from.

    Both bounds default to all history.
    """
    sc = scope(conn, entity_id, start=start, end=end)
    e = t.entries
    in_period = [e.c.entity_id == entity_id, e.c.status == "posted"]
    if sc.start is not None:
        in_period.append(e.c.dt >= sc.start)
    if sc.end is not None:
        in_period.append(e.c.dt <= sc.end)
    origins = {row.origin: int(row.n) for row in
               conn.execute(sa.select(e.c.origin, sa.func.count().label("n")).where(*in_period)
                            .group_by(e.c.origin).order_by(e.c.origin))}
    m = t.journal_mappings
    journals = {row.journal_path: int(row.n) for row in
                conn.execute(sa.select(m.c.journal_path, sa.func.count(sa.distinct(m.c.entry_id)).label("n"))
                             .select_from(m.join(e, m.c.entry_id == e.c.id))
                             .where(m.c.entity_id == entity_id, *in_period)
                             .group_by(m.c.journal_path).order_by(m.c.journal_path))}
    return Composition(sc.start, sc.end, origins, journals)
