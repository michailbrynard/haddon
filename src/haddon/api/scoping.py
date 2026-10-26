"""Date-range scoping at the HTTP edge (issue 0002).

A journal is continuous, so the API accepts any inclusive ``from``/``to`` range and refuses only a range whose
explicit start is in the future (a statement of a period that has not begun can only be empty) or a point
(``as_of``/``through``) date in the future, because a position at a future date is not a partial answer to
anything.  A range that merely ends in the future is fine: the period you are standing in reports what has
happened so far.  There is no book year and no cross-year refusal: dates go through to ``haddon.reports``
unchanged, and the conversion to hledger's exclusive end stays in the export adapter, never here.
"""

from __future__ import annotations

from haddon.domain import dates
from haddon.domain.dates import parse_date
from haddon.domain.errors import validation
from haddon.reports.scope import date_range


def not_yet_happened(value: object, *, path: str) -> str:
    """Parse ``value`` and refuse it if it is after today.

    Used for a range start and for point dates such as a trial balance's ``through``.  Range ends are not passed
    through here.
    """
    value = parse_date(value, path=path)
    now = dates.today()
    if value > now:
        raise validation(f"{path} {value} has not happened yet; these books can be reported through {now}",
                         path=path, reason="in_the_future", value=value, today=now)
    return value


def scoped_range(conn, entity_id: str, *, start: object = None, end: object = None) -> tuple[str | None, str | None]:
    """Return the validated inclusive range.  Either bound may be ``None`` (open).

    Only the start is refused for being in the future.  A range that ends in the future is a normal request (the
    current quarter so far), so the end is checked for shape but not against the clock.
    """
    del conn, entity_id
    sc = date_range(start, end)
    if sc.start is not None:
        not_yet_happened(sc.start, path="from")
    return sc.start, sc.end
