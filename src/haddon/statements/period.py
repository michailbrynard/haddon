"""What a date range is called, and what came before it.

A statement wants two things a pair of dates does not carry: a name short enough to head a figure column, and
the period it should be compared with.  Both are calendar facts, so both are derived rather than asked for.
``2026-04-01..2026-06-30`` is the second quarter of 2026 and its comparative is the first, and a caller that had
to say so could say it wrong.

The granularity is inferred from the bounds rather than passed in, which keeps one thing true: the period the
document is drawn for is exactly the period that was requested.  A caller naming a granularity that disagreed
with its own dates would produce a statement headed one thing and containing another, and that is the failure
this module exists to make unrepresentable.

A range that is not a whole calendar year, quarter or month is simply itself: it is headed by its own dates and
compared with the equal-length range immediately before it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")
SHORT_MONTHS = tuple(m[:3] for m in MONTHS)

YEAR, QUARTER, MONTH, CUSTOM = "year", "quarter", "month", "custom"

#: what a partly-elapsed period of each kind is called in prose
UNIT = {YEAR: "year", QUARTER: "quarter", MONTH: "month", CUSTOM: "period"}


def _d(text: str) -> date:
    y, m, dd = (int(x) for x in text.split("-"))
    return date(y, m, dd)


def _s(value: date) -> str:
    return value.isoformat()


def _last_day(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


@dataclass(frozen=True)
class Period:
    """A closed date range, what it is called, and which calendar unit it happens to be."""

    start: str
    end: str
    granularity: str
    #: the period had not finished when it was asked for, so ``end`` is today rather than the period's own last
    #: day.  It changes nothing about the figures and everything about what the statement may be called.
    to_date: bool = False

    @property
    def name(self) -> str:
        """Short enough to head a figure column: ``2026``, ``Q2 2026``, ``Jun 2026``, or the dates."""
        s, e = _d(self.start), _d(self.end)
        if self.granularity == YEAR:
            base = str(s.year)
        elif self.granularity == QUARTER:
            base = f"Q{(s.month - 1) // 3 + 1} {s.year}"
        elif self.granularity == MONTH:
            base = f"{SHORT_MONTHS[s.month - 1]} {s.year}"
        else:
            left = (f"{s.day} {SHORT_MONTHS[s.month - 1]}" if s.year == e.year
                    else f"{s.day} {SHORT_MONTHS[s.month - 1]} {s.year}")
            base = f"{left} – {e.day} {SHORT_MONTHS[e.month - 1]} {e.year}"
        return f"{base} to date" if self.to_date else base

    @property
    def heading(self) -> str:
        """The long form a statement is headed with, which must never claim more than the figures cover.

        A quarter that has not finished, headed ``Q3 2026``, says the figures are a quarter's worth. They are
        not, and by the time anybody reads the PDF the request that produced it is gone. So a partly-elapsed
        period is headed by the dates it actually covers, and says in words which kind of period it is part of.
        """
        words = _range_words(self.start, self.end)
        return f"{words} ({UNIT[self.granularity]} to date)" if self.to_date else words


def _range_words(start: str, end: str) -> str:
    """Spell a range out as ``1 April – 30 June 2026``, giving the year once when both ends share it."""
    s, e = _d(start), _d(end)
    left = f"{s.day} {MONTHS[s.month - 1]}" if s.year == e.year else f"{s.day} {MONTHS[s.month - 1]} {s.year}"
    return f"{left} – {e.day} {MONTHS[e.month - 1]} {e.year}"


def has_begun(period: Period, today: str) -> bool:
    """Return whether the period has started. One that has not can only be reported empty, which is not a fact."""
    return period.start <= today


def through_today(period: Period, today: str) -> Period:
    """Cut the period down to as far as it has actually run.

    A period wholly in the past comes back untouched: no new wording, no change. One that contains today is
    cut off at today and marked ``to_date``, so every heading derived from it says so. A period that has not
    begun is the caller's to refuse (``has_begun``): there is nothing here to cut it down to.
    """
    if period.end <= today:
        return period
    return replace(period, end=today, to_date=True)


def classify(start: str, end: str) -> Period:
    """Make a ``Period`` of the range, working out from its own bounds which calendar unit it is, if any."""
    s, e = _d(start), _d(end)
    if s.day == 1 and e == _last_day(e.year, e.month) and s.year == e.year:
        if s.month == 1 and e.month == 12:
            return Period(start, end, YEAR)
        if s.month == e.month:
            return Period(start, end, MONTH)
        if s.month in (1, 4, 7, 10) and e.month == s.month + 2:
            return Period(start, end, QUARTER)
    return Period(start, end, CUSTOM)


def preceding(period: Period) -> Period:
    """The period immediately before this one: the previous calendar unit, or an equal-length range."""
    s, e = _d(period.start), _d(period.end)
    if period.granularity == YEAR:
        return Period(f"{s.year - 1}-01-01", f"{s.year - 1}-12-31", YEAR)
    if period.granularity in (QUARTER, MONTH):
        months = 3 if period.granularity == QUARTER else 1
        index = s.year * 12 + (s.month - 1) - months            # months since year 0, stepped back one period
        first = date(index // 12, index % 12 + 1, 1)
        # a quarter always starts in January, April, July or October, so the last month of the period before it
        # is inside the same year and this cannot roll over
        return Period(_s(first), _s(_last_day(first.year, first.month + months - 1)), period.granularity)
    span = (e - s).days + 1
    prior_end = s - timedelta(days=1)
    return Period(_s(prior_end - timedelta(days=span - 1)), _s(prior_end), CUSTOM)


def comparative(period: Period) -> Period:
    """Return the period a statement's comparative column is read against.

    A period wholly in the past is compared with the period immediately before it, which is the ordinary thing a
    statement does and what a reader expects of one.

    A period still running is compared with the same dates a year earlier, because the period before it has
    finished and this one has not.  Nine months of a year against the whole of the year before is wrong by
    roughly a third, and it is wrong in the flattering direction, which is the kind of error that survives
    review.  The same dates a year back are like for like by construction, for a quarter to date as much as for
    a year to date, and the column is headed by those dates so the span is stated rather than implied.
    """
    return same_span_last_year(period) if period.to_date else preceding(period)


def same_span_last_year(period: Period) -> Period:
    """Return the identical dates one calendar year back.

    The result is named by its dates, because it is a span rather than a period anybody has a word for:
    ``1 Jul – 12 Sep 2025``.
    """
    return classify(_s(_step_year(_d(period.start))), _s(_step_year(_d(period.end))))


def a_year_earlier(period: Period) -> Period:
    """Return the same period one calendar year back, the comparison that shows what a period usually holds.

    This is not the comparative column: that is the period immediately before (``preceding``), which is what
    a statement compares figures with.  This one is for asking what a period of this kind normally contains,
    which is a question about the shape of a year rather than about a trend.
    """
    s, e = _d(period.start), _d(period.end)
    if period.granularity == CUSTOM or period.to_date:
        return same_span_last_year(period)
    return classify(_s(_step_year(s)), _s(_last_day(e.year - 1, e.month)))


def _step_year(value: date) -> date:
    """One year back, with 29 February landing on 28 February rather than raising."""
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)
