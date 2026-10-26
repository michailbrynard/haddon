"""The guards a continuous journal needs and an annual one did not (issue 0002).

* The double count.  In a continuous journal the danger is an opening restatement imported on top of the
  period it restates, so a report shows every balance twice.  The synthetic shape is below; the corpus books
  are covered in ``test_import_inc_bv.py`` and by the continuity oracle.
* A sub-period balance sheet balances.  A sheet is cumulative and a result is a movement, so the result row
  must cover the same span the sheet does, from the journal's beginning rather than the period's start.
  Otherwise every period that does not begin at the journal's beginning silently loses the profit before it.
* The aggregate overflow is a 422 at a report entry point, not only in ``sum_micros``.
* Reports write nothing, proven against a connection SQLite itself will not let write.
* ``include_zero``, the flag both ways, which nothing else asserts directly.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from haddon.db.connection import make_engine
from haddon.domain.errors import HaddonError
from haddon.domain.micros import Micros
from haddon.reports import account_balance, income_statement, movements, trial_balance
from haddon.reports.postings import BALANCE_SHEET_TYPES

from haddon.repository import entries

from repo_helpers import pair, post

USD = "USD"


# an opening is not a restatement
def test_an_opening_restated_over_the_period_it_restates_would_double_every_balance(fx):
    """The bug shape, stated as arithmetic rather than as a story about the importer.

    Post a year, then post its own closing position again as an "opening" the way an annual book's carry-forward
    journal states it.  Every balance-sheet figure doubles while the journal still balances per commodity, which
    is exactly why no invariant catches it and why the importer refuses to post one
    (``haddon.importing.restatement``).
    """
    post(fx, "2025-03-04", "500000000", narration="a cost", debit="expenses:office", credit="assets:cash")
    with fx.reader() as conn:
        cash = account_balance(conn, fx.entity_id, account_id=fx.acct("assets:cash"), commodity=USD,
                               through="2025-12-31")
    assert cash.render() == "-500000000"

    # the restatement: the same closing position, booked again on 1 January
    with fx.tx() as ctx:
        entries.post_entry(ctx, dt="2026-01-01", narration="opening balances carried forward", origin="opening",
                           legs=pair(fx, "500000000", debit="equity:retained-earnings", credit="assets:cash"),
                           _allow_reserved_origin=True)
    with fx.reader() as conn:
        doubled = account_balance(conn, fx.entity_id, account_id=fx.acct("assets:cash"), commodity=USD,
                                  through="2026-12-31")
        tb = trial_balance(conn, fx.entity_id, through="2026-12-31")
    assert doubled.render() == "-1000000000" == f"-{2 * 500000000}"   # twice the cash that ever moved
    # and the journal is still perfectly balanced, which is the whole trap: 2 x 0 = 0
    assert sum(int(ln.amount.render()) for ln in tb.lines) == 0


def test_a_continuous_journal_carries_a_balance_over_a_year_end_with_no_opening_at_all(fx):
    """The acceptance criterion of issue 0002, as a balance: December's cash is January's cash, unassisted."""
    post(fx, "2025-12-30", "250000000", narration="December", debit="expenses:office", credit="assets:cash")
    post(fx, "2026-01-02", "125000000", narration="January", debit="expenses:office", credit="assets:cash")
    with fx.reader() as conn:
        aid = fx.acct("assets:cash")
        assert account_balance(conn, fx.entity_id, account_id=aid, commodity=USD, through="2025-12-31").render() \
            == "-250000000"
        assert account_balance(conn, fx.entity_id, account_id=aid, commodity=USD, through="2026-01-31").render() \
            == "-375000000"
        # and the movements of January alone are January's, not the running balance
        jan = movements(conn, fx.entity_id, start="2026-01-01", end="2026-01-31")
        assert {ln.path: ln.amount.render() for ln in jan.lines if ln.account_id == aid} \
            == {"assets:cash": "-125000000"}


# a balance sheet of any range meets in the middle
def _post_entry(api, dt: str, amount: str, *, debit: str, credit: str, narration: str = "a booking") -> None:
    body = {"dt": dt, "narration": narration, "reason": "a booking, for a report to read",
            "legs": [{"account": debit, "commodity": USD, "amount": amount},
                     {"account": credit, "commodity": USD, "amount": f"-{amount}"}]}
    response = api.post("/entries", key=str(uuid.uuid4()), json=body)
    assert response.status_code == 201, response.text[:400]


@pytest.fixture
def two_years(api_small):
    """A profitable 2025 and a small January 2026: the shape every sub-period report has to survive."""
    _post_entry(api_small, "2025-06-01", "1000.00", debit="assets:cash", credit="income:sales",
                narration="the year that made the money")
    _post_entry(api_small, "2026-01-15", "40.00", debit="expenses:office", credit="assets:cash",
                narration="January")
    return api_small


def test_a_balance_sheet_json_carries_what_a_caller_needs_to_balance_it(two_years):
    """``totals`` is the net of the asset/liability/equity sections, which is not zero on its own: the result
    has not been posted to equity and never is in a continuous journal.  The payload therefore also carries
    ``result_micros``, the accumulated income and expense through the same date, and the two are what a
    caller adds up."""
    sheet = two_years.json("/reports/balance-sheet?as_of=2026-01-31")
    assert sheet["totals"] == {USD: "960000000"}                       # 1000.00 earned less 40.00 spent
    assert sheet["result_micros"] == {USD: "-960000000"}
    for code, total in sheet["totals"].items():
        assert int(total) + int(sheet["result_micros"][code]) == 0
    # and it is the same figure the income statement from the beginning reports, to the micro
    accumulated = two_years.json("/reports/income-statement?to=2026-01-31")
    assert accumulated["net_micros"] == sheet["result_micros"]


@pytest.mark.slow
def test_the_statements_pdf_of_a_sub_period_balances(two_years):
    """January 2026 on its own.  The sheet at 31 January holds 2025's profit; a result row covering only
    January would leave that profit in neither equity nor the result line, and the document would print
    "differ by" with the whole of the prior year in it."""
    from test_statements_pdf import pdf_text  # noqa: PLC0415  -- one shared PDF reader

    response = two_years.get("/reports/statements.pdf?from=2026-01-01&to=2026-01-31&comparative=false")
    assert response.status_code == 200, response.text[:400]
    text = pdf_text(response.content)
    assert "differ by" not in text
    assert "Assets equal liabilities plus equity" in text


def test_the_pdf_and_the_ui_read_the_same_result_row(two_years):
    """The route's promise is that the document and the UI cannot drift.  The UI asks for the result with an
    open start (``web/src/routes/Reports.tsx``); so the document must, and this pins the figure both get."""
    engine = make_engine(two_years.path, busy_timeout_ms=200, immediate=False)
    try:
        with engine.connect() as conn:
            through_january = income_statement(conn, two_years.entity_id, start=None, end="2026-01-31")
            january_only = income_statement(conn, two_years.entity_id, start="2026-01-01", end="2026-01-31")
            sheet = trial_balance(conn, two_years.entity_id, through="2026-01-31")
    finally:
        engine.dispose()
    assert through_january.net[USD].render() == "-960000000"
    assert january_only.net[USD].render() == "40000000"       # the January-only row the route used to use
    net = sum(int(ln.amount.render()) for ln in sheet.lines if ln.type in BALANCE_SHEET_TYPES)
    assert net + int(through_january.net[USD].render()) == 0  # the sheet meets only with the cumulative row
    assert net + int(january_only.net[USD].render()) != 0     # and not with January's


# the flag, and the refusals
def test_include_zero_reports_a_pair_that_nets_to_zero_only_when_asked(fx):
    """A pair that moved and came back is a fact about the books; a pair that never moved is not.  Neither is
    reported by default, and ``include_zero`` reports the first without inventing the second."""
    post(fx, "2026-02-01", "70000000", narration="out", debit="expenses:office", credit="assets:cash")
    post(fx, "2026-02-02", "70000000", narration="back", debit="assets:cash", credit="expenses:office")
    with fx.reader() as conn:
        quiet = trial_balance(conn, fx.entity_id, through="2026-12-31")
        loud = trial_balance(conn, fx.entity_id, through="2026-12-31", include_zero=True)
        quiet_mv = movements(conn, fx.entity_id, start="2026-01-01", end="2026-12-31")
        loud_mv = movements(conn, fx.entity_id, start="2026-01-01", end="2026-12-31", include_zero=True)
    assert quiet.lines == () and quiet_mv.lines == ()
    zeroed = {(ln.path, ln.commodity) for ln in loud.lines}
    assert zeroed == {("assets:cash", USD), ("expenses:office", USD)}
    assert all(ln.amount.is_zero for ln in loud.lines)
    assert {(ln.path, ln.commodity) for ln in loud_mv.lines} == zeroed
    # an account with no posting at all is still absent, whatever the flag says
    assert "liabilities:payables" not in {ln.path for ln in loud.lines}


def test_an_aggregate_that_leaves_the_signed_64_bit_range_is_a_422_overflow_at_a_report(fx):
    """``sum_micros`` is unit-covered (``test_no_float_reaches_db.py``); this is the same refusal reached
    through a *report*, which is where a caller meets it.  A wrapped integer or a float would be a silently
    wrong balance, so the report refuses instead."""
    huge = str((2 ** 62 // 10 ** 4) * 10 ** 4)            # the largest whole-cent amount under 2**62
    for i in range(5):                                    # five of them leave the signed range on the third
        post(fx, f"2026-0{i + 1}-01", huge, narration=f"huge {i}", debit="expenses:office", credit="assets:cash")
    with fx.reader() as conn:
        with pytest.raises(HaddonError) as ei:
            trial_balance(conn, fx.entity_id)
        assert ei.value.details[0]["reason"] == "int64_overflow"
        assert ei.value.details[0]["path"] == "expenses:office[USD]"
        with pytest.raises(HaddonError) as ei:
            income_statement(conn, fx.entity_id)
        assert ei.value.details[0]["reason"] == "int64_overflow"
        with pytest.raises(HaddonError) as ei:
            account_balance(conn, fx.entity_id, account_id=fx.acct("assets:cash"), commodity=USD)
        assert ei.value.details[0]["reason"] == "int64_overflow"


def test_reports_work_on_a_query_only_connection_and_write_nothing(fx):
    """Every report is a read.  Proven by asking SQLite to enforce it rather than by counting rows afterwards:
    ``PRAGMA query_only`` makes any write on the connection an error, so a report that wrote would raise here
    instead of passing quietly."""
    post(fx, "2026-04-01", "33000000", narration="a cost", debit="expenses:office", credit="assets:cash")
    before = _table_counts(fx)
    engine = make_engine(fx.path, busy_timeout_ms=200, immediate=False)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA query_only=ON")
            assert conn.exec_driver_sql("PRAGMA query_only").scalar() == 1
            assert trial_balance(conn, fx.entity_id).lines
            assert movements(conn, fx.entity_id, start="2026-01-01", end="2026-12-31").lines
            assert income_statement(conn, fx.entity_id).lines
            assert account_balance(conn, fx.entity_id, account_id=fx.acct("assets:cash"), commodity=USD)
            # and the pragma is really biting on this connection
            with pytest.raises(Exception):
                conn.exec_driver_sql("INSERT INTO events(id) VALUES ('x')")
    finally:
        engine.dispose()
    assert _table_counts(fx) == before


def _table_counts(fx) -> dict[str, int]:
    with fx.reader() as conn:
        names = [r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))]
        return {n: conn.execute(sa.text(f'SELECT count(*) FROM "{n}"')).scalar_one() for n in names}
