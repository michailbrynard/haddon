"""The fast half of chunk G5: what a period is called, and how report lines become a statement.

Both are pure (no database, no renderer), and both are where a document goes quietly wrong rather than
loudly. A period that names itself one thing and holds another, and a comparative column that drops a row and
takes its figure out of the total with it, are mistakes that produce a plausible page.
"""

from __future__ import annotations

import pytest

from haddon.domain.micros import Micros
from haddon.reports.types import Line
from haddon.statements.period import (CUSTOM, MONTH, QUARTER, YEAR, Period, a_year_earlier, classify,
                                      comparative, has_begun, preceding, same_span_last_year,
                                      through_today)
from haddon.statements.shape import build_section, presented


def m(text: str) -> Micros:
    return Micros.parse(text)


def line(path: str, type_: str, micros: str, commodity: str = "EUR") -> Line:
    return Line(f"acct-{path}", path, type_, commodity, m(micros))


# a period names itself
@pytest.mark.parametrize("start,end,granularity,name", [
    ("2026-01-01", "2026-12-31", YEAR, "2026"),
    ("2026-01-01", "2026-03-31", QUARTER, "Q1 2026"),
    ("2026-04-01", "2026-06-30", QUARTER, "Q2 2026"),
    ("2026-07-01", "2026-09-30", QUARTER, "Q3 2026"),
    ("2026-10-01", "2026-12-31", QUARTER, "Q4 2026"),
    ("2026-05-01", "2026-05-31", MONTH, "May 2026"),
    ("2026-02-01", "2026-02-28", MONTH, "Feb 2026"),
    # not a calendar unit, and so not named as one
    ("2026-02-01", "2026-04-30", CUSTOM, "1 Feb – 30 Apr 2026"),
    ("2026-04-02", "2026-06-30", CUSTOM, "2 Apr – 30 Jun 2026"),
    ("2026-04-01", "2026-06-29", CUSTOM, "1 Apr – 29 Jun 2026"),
])
def test_a_range_is_named_from_its_own_bounds(start, end, granularity, name):
    period = classify(start, end)
    assert (period.granularity, period.name) == (granularity, name)
    assert (period.start, period.end) == (start, end)


@pytest.mark.parametrize("start,end,prior_start,prior_end,prior_name", [
    ("2026-01-01", "2026-12-31", "2025-01-01", "2025-12-31", "2025"),
    ("2026-04-01", "2026-06-30", "2026-01-01", "2026-03-31", "Q1 2026"),
    ("2026-01-01", "2026-03-31", "2025-10-01", "2025-12-31", "Q4 2025"),   # back over the year boundary
    ("2026-01-01", "2026-01-31", "2025-12-01", "2025-12-31", "Dec 2025"),
    ("2026-03-01", "2026-03-31", "2026-02-01", "2026-02-28", "Feb 2026"),  # a short month, not 31 days back
    ("2026-02-14", "2026-03-20", "2026-01-10", "2026-02-13", "10 Jan – 13 Feb 2026"),
])
def test_the_comparative_is_the_period_immediately_before(start, end, prior_start, prior_end, prior_name):
    prior = preceding(classify(start, end))
    assert (prior.start, prior.end, prior.name) == (prior_start, prior_end, prior_name)


def test_the_period_before_ends_the_day_before_this_one_begins():
    """What makes the comparative balance sheet's date the right one: it is drawn the day before."""
    for start, end in [("2026-01-01", "2026-12-31"), ("2026-04-01", "2026-06-30"), ("2026-05-01", "2026-05-31"),
                       ("2026-03-01", "2026-03-31"), ("2026-02-14", "2026-03-20")]:
        period = classify(start, end)
        assert preceding(period).end < period.start
        assert (preceding(period).end, period.start) != (period.start, period.start)


@pytest.mark.parametrize("start,end,expected", [
    ("2026-04-01", "2026-06-30", ("2025-04-01", "2025-06-30", "Q2 2025")),
    ("2026-01-01", "2026-12-31", ("2025-01-01", "2025-12-31", "2025")),
    ("2026-05-01", "2026-05-31", ("2025-05-01", "2025-05-31", "May 2025")),
    ("2024-02-01", "2024-02-29", ("2023-02-01", "2023-02-28", "Feb 2023")),  # a leap day has nowhere to land
])
def test_the_same_period_a_year_earlier_stays_a_whole_period(start, end, expected):
    earlier = a_year_earlier(classify(start, end))
    assert (earlier.start, earlier.end, earlier.name) == expected


# the statement's sign
@pytest.mark.parametrize("type_,stored,shown", [
    ("income", "-1250000000000", "1250000000000"),     # revenue reads positive
    ("liability", "-42000000000", "42000000000"),      # so does a payable that is a payable
    ("equity", "-100000000000", "100000000000"),
    ("liability", "3500000000", "-3500000000"),        # and one standing the other way round reads negative
    ("asset", "610000000000", "610000000000"),         # debit-natured types are untouched
    ("expense", "7500000000", "7500000000"),
])
def test_credit_natured_types_are_shown_the_way_a_statement_reads_them(type_, stored, shown):
    assert presented(m(stored), type_) == m(shown)


# the comparative is a union
def test_an_account_only_the_prior_column_has_is_a_zero_and_not_a_missing_row():
    """Walking one column and looking the other up drops a row, and takes its figure out of that column's total
    with it: a total that does not add up and gives no sign of it."""
    here = [line("assets:receivables:trade", "asset", "1000000")]
    then = [line("assets:receivables:trade", "asset", "4000000"),
            line("assets:receivables:vat", "asset", "250000000")]

    section = build_section("asset", "Assets", "Total assets", here, then, show_comparative=True)
    rows = [r for g in section.groups for r in g.lines]
    assert [r.label for r in rows] == ["trade", "vat"]
    assert [r.amount.render() for r in rows] == ["1000000", "0"]
    assert [r.comparative.render() for r in rows] == ["4000000", "250000000"]
    # and the totals are of everything either column mentions
    assert section.total == m("1000000")
    assert section.comparative_total == m("254000000")


def test_a_group_of_one_keeps_its_whole_path_and_grows_no_heading():
    section = build_section("expense", "Expenses", "Total expenses",
                            [line("expenses:bank-charges", "expense", "12000000")], [], show_comparative=False)
    (group,) = section.groups
    assert group.heading is None and group.subtotal is None
    assert [r.label for r in group.lines] == ["bank-charges"]
    assert [r.comparative for r in group.lines] == [None]


def test_a_group_of_several_gets_a_heading_and_a_subtotal_of_its_own():
    section = build_section("expense", "Expenses", "Total expenses", [
        line("expenses:professional-services:accounting", "expense", "1200000000"),
        line("expenses:professional-services:legal", "expense", "300000000"),
    ], [], show_comparative=False)
    (group,) = section.groups
    assert group.heading == "professional-services"
    assert [r.label for r in group.lines] == ["accounting", "legal"]
    assert group.subtotal == m("1500000000") == section.total


def test_an_account_posted_at_its_own_group_level_keeps_its_whole_path():
    """Stripping the group segment would leave this line with no label at all. The path is longer and it is
    never ambiguous; a name invented for it would be neither."""
    section = build_section("liability", "Liabilities", "Total liabilities", [
        line("liabilities:trade-payables", "liability", "-8000000000"),
        line("liabilities:trade-payables:supplier-a", "liability", "-25000000"),
    ], [], show_comparative=True)
    (group,) = section.groups
    assert group.heading == "trade-payables"
    assert [r.label for r in group.lines] == ["liabilities · trade-payables", "supplier-a"]


def test_an_extra_row_is_totalled_with_the_accounts_and_carries_no_account_id():
    """The result to date inside equity: part of the section's total, and not a line in the chart."""
    from haddon.statements.shape import Extra

    section = build_section("equity", "Equity", "Total equity",
                            [line("equity:share-capital", "equity", "-5000000000")], [],
                            show_comparative=False,
                            extra=(Extra("Result to date", m("80000000"), None),))
    rows = [r for g in section.groups for r in g.lines]
    assert [r.account_id for r in rows] == ["acct-equity:share-capital", None]
    assert section.total == m("5080000000")


# a period that has not finished
@pytest.mark.parametrize("start,end,today,expected_end,to_date", [
    ("2026-04-01", "2026-06-30", "2026-09-12", "2026-06-30", False),  # wholly past: untouched
    ("2026-07-01", "2026-09-30", "2026-09-12", "2026-09-12", True),   # still running: cut off at today
    ("2026-09-01", "2026-09-30", "2026-09-12", "2026-09-12", True),
    ("2026-01-01", "2026-12-31", "2026-09-12", "2026-09-12", True),
    ("2026-07-01", "2026-09-30", "2026-09-30", "2026-09-30", False),  # finishing today is finished
])
def test_a_period_is_cut_off_at_today_only_when_it_has_not_finished(start, end, today, expected_end, to_date):
    period = through_today(classify(start, end), today)
    assert (period.end, period.to_date) == (expected_end, to_date)
    assert period.start == start


@pytest.mark.parametrize("start,end,today,name,heading", [
    ("2026-07-01", "2026-09-30", "2026-09-12", "Q3 2026 to date",
     "1 July – 12 September 2026 (quarter to date)"),
    ("2026-09-01", "2026-09-30", "2026-09-12", "Sep 2026 to date",
     "1 September – 12 September 2026 (month to date)"),
    ("2026-01-01", "2026-12-31", "2026-09-12", "2026 to date",
     "1 January – 12 September 2026 (year to date)"),
    ("2026-04-01", "2026-06-30", "2026-09-12", "Q2 2026", "1 April – 30 June 2026"),
])
def test_a_period_still_running_never_claims_to_be_the_whole_of_itself(start, end, today, name, heading):
    period = through_today(classify(start, end), today)
    assert period.name == name
    assert period.heading == heading


def test_a_period_that_has_not_begun_is_reported_as_such_rather_than_cut_down():
    """There is nothing to cut a period down to before it starts; refusing it is the caller's job."""
    assert not has_begun(classify("2026-10-01", "2026-12-31"), "2026-09-12")
    assert has_begun(classify("2026-07-01", "2026-09-30"), "2026-09-12")
    assert has_begun(classify("2026-09-12", "2026-09-12"), "2026-09-12")  # beginning today has begun


# the comparative column
@pytest.mark.parametrize("start,end,today,prior_start,prior_end,prior_name", [
    # wholly in the past: the period immediately before, exactly as it always was
    ("2026-04-01", "2026-06-30", "2026-09-12", "2026-01-01", "2026-03-31", "Q1 2026"),
    ("2025-01-01", "2025-12-31", "2026-09-12", "2024-01-01", "2024-12-31", "2024"),
    # still running: the same dates a year back, named by those dates
    ("2026-01-01", "2026-12-31", "2026-09-12", "2025-01-01", "2025-09-12", "1 Jan – 12 Sep 2025"),
    ("2026-07-01", "2026-09-30", "2026-09-12", "2025-07-01", "2025-09-12", "1 Jul – 12 Sep 2025"),
    ("2026-09-01", "2026-09-30", "2026-09-12", "2025-09-01", "2025-09-12", "1 Sep – 12 Sep 2025"),
])
def test_a_running_period_is_compared_like_for_like(start, end, today, prior_start, prior_end, prior_name):
    prior = comparative(through_today(classify(start, end), today))
    assert (prior.start, prior.end) == (prior_start, prior_end)
    assert prior.name == prior_name


def test_the_leap_day_lands_on_the_day_before_rather_than_raising():
    """29 February has nowhere to go in an ordinary year, and a comparative that threw would take the whole
    statement with it."""
    prior = same_span_last_year(Period("2028-01-01", "2028-02-29", "custom", to_date=True))
    assert (prior.start, prior.end) == ("2027-01-01", "2027-02-28")
