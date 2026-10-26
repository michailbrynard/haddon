"""Date-range scoping for every report (issue 0002).

A journal is one continuous ledger, so a report covers any valid ``[start, end]`` range and may leave either side
open: ``start=None`` means "from the beginning", ``end=None`` means "through the latest posting".  There is no
book year, no cross-year refusal and no carry boundary; the postings themselves are continuous.

``tests/test_reports_scoping.py`` inspects the package and fails if a report function still takes a
``book_year``: continuity is a structural guarantee, not a convention.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Connection

from haddon.domain.dates import parse_date
from haddon.domain.errors import validation


@dataclass(frozen=True)
class DateRange:
    """An inclusive date range; either bound may be open (``None``)."""

    start: str | None
    end: str | None


def date_range(start: object = None, end: object = None) -> DateRange:
    """Validate an optional ``start``/``end`` pair and require ``start <= end`` when both are given."""
    s = None if start is None else parse_date(start, path="start")
    e = None if end is None else parse_date(end, path="end")
    if s is not None and e is not None and s > e:
        raise validation(f"start {s} is after end {e}", path="start", reason="range_order", start=s, end=e)
    return DateRange(s, e)


def scope(conn: Connection, entity_id: str, *, start: object = None, end: object = None) -> DateRange:
    """Return the date range a report covers.

    The connection and entity are accepted for symmetry with the old book scope.  A continuous journal needs
    neither to bound a query by date.
    """
    del conn, entity_id
    return date_range(start, end)
