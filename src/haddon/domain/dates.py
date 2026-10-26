"""Strict calendar dates: ``YYYY-MM-DD`` shape plus calendar-valid round trip (spec "Validate each D...")."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from haddon.domain.errors import validation

_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value: object, *, path: str = "dt") -> str:
    """Return the validated ``YYYY-MM-DD`` string or raise 422 ``validation``.  NULL is rejected before comparison."""
    if not isinstance(value, str):
        raise validation(f"{path}: a calendar date string is required", path=path, reason="date_required")
    if not _SHAPE.match(value):
        raise validation(f"{path}: {value!r} is not YYYY-MM-DD", path=path, reason="date_shape", value=value)
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise validation(f"{path}: {value!r} is not a calendar date", path=path, reason="date_invalid", value=value) from None
    if d.isoformat() != value:
        raise validation(f"{path}: {value!r} does not round-trip", path=path, reason="date_invalid", value=value)
    return value


def today(now: date | None = None) -> str:
    """The current date, on the one clock this system already keeps.

    ``repository.ctx.utc_now`` stamps every ``created_at`` in the record in UTC, so UTC is what "today" means
    here too: the day a report refuses to run past is the same day a write made at that moment is recorded
    under.  Reports and the record keeping different clocks would mean a report that refused at one hour and
    allowed at the next, which is worse than either rule on its own.
    """
    return (now or datetime.now(UTC).date()).isoformat()


def first_day(year: int) -> str:
    """Return the first day of a calendar year.

    Only the two commands that still take ``--year`` as shorthand for a range (import, export) use this.  Nothing
    stores a year and nothing validates one (issue 0002).
    """
    return f"{year:04d}-01-01"


def last_day(year: int) -> str:
    return f"{year:04d}-12-31"
