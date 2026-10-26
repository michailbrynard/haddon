"""Reports over a continuous journal (issue 0002): date ranges, no annual book, no carry entry.

The annual model is gone, and this file makes its return a structural failure rather than a convention someone
has to remember: the first half walks ``haddon.reports`` with ``inspect`` and fails if a
``book_year`` parameter, a ``carry_forward``/``CarryForward*`` name or a ``multi_year_movements`` helper reappears
anywhere in the package.  The second half proves the continuous behaviour the structure promises, through the real
repository API (``repo_helpers``): a single ``trial_balance(through=...)`` spans the calendar-year boundary because
the postings themselves are continuous, ``movements`` are the postings inside a range with openings excluded, and an
open range bound is allowed.

Sign convention is hledger's (debit positive): income is negative when earned.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import haddon.reports
from haddon.domain.errors import HaddonError
from haddon.domain.micros import ZERO, Micros
from haddon.repository import entries as en
from haddon.repository.entries import Leg
from haddon.reports import account_balance, income_statement, movements, register, trial_balance
from haddon.reports.scope import DateRange, date_range, scope

from repo_helpers import AGENT, Fixture, m, post

# 1. structural: the annual model is gone
SCOPE_PARAMS = {"start", "end", "through"}
#: each report entry point names the range parameter(s) it takes; every bound defaults to open (``None``)
ENTRY_POINT_SCOPE = {
    "trial_balance": ("through",),
    "account_balance": ("through",),
    "movements": ("start", "end"),
    "income_statement": ("start", "end"),
    "register": ("start", "end"),
    "composition": ("start", "end"),
}
#: names the continuous model removed; any one of them coming back is a failure
REMOVED = ("carry_forward", "verify_carry_forward", "multi_year_movements")


def _report_functions():
    """Every module-level function defined anywhere under ``haddon.reports``, qualified for a failure message."""
    mods = [haddon.reports] + [importlib.import_module(mi.name)
                               for mi in pkgutil.walk_packages(haddon.reports.__path__, "haddon.reports.")]
    seen: set = set()
    for mod in mods:
        for name, obj in vars(mod).items():
            if inspect.isfunction(obj) and obj.__module__.startswith("haddon.reports") and obj not in seen:
                seen.add(obj)
                yield f"{obj.__module__}.{name}", obj


def _book_year_params(fn) -> list[str]:
    return [name for name in inspect.signature(fn).parameters if name in ("book_year", "book_years")]


def test_no_function_under_haddon_reports_accepts_a_book_year():
    """Scan every signature in the package: add ``book_year`` to any helper and this fails."""
    offenders = []
    checked = 0
    for qual, fn in _report_functions():
        checked += 1
        if _book_year_params(fn):
            offenders.append(f"{qual}{inspect.signature(fn)}")
    assert checked >= 10, f"the scan looked at almost nothing ({checked} functions); the gate would prove nothing"
    assert not offenders, "the annual model is back: " + "; ".join(offenders)


def test_the_book_year_scan_would_catch_a_reintroduced_one():
    def sneaky(conn, entity_id, book_year):  # the shape this file exists to refuse
        return None

    assert _book_year_params(sneaky) == ["book_year"]


def test_report_entry_points_take_a_date_range_not_a_year():
    """Every connection-taking report is scoped by bounded dates, each bound optional."""
    for name, bounds in ENTRY_POINT_SCOPE.items():
        fn = getattr(haddon.reports, name)
        sig = inspect.signature(fn)
        assert list(sig.parameters)[:2] == ["conn", "entity_id"], f"{name}{sig}"
        for bound in bounds:
            assert bound in sig.parameters, f"{name} takes no {bound!r}: {sig}"
            assert sig.parameters[bound].default is None, f"{name}.{bound} must allow an open bound: {sig}"
        assert not SCOPE_PARAMS.isdisjoint(sig.parameters), name
        assert not any(p in sig.parameters for p in ("book_year", "book_years", "years", "from_year")), name


def test_the_annual_and_carry_surface_is_gone():
    leaked = [name for name in REMOVED if hasattr(haddon.reports, name)]
    leaked += [name for name in dir(haddon.reports) if name.startswith("CarryForward")]
    assert not leaked, f"the annual model is back: {sorted(set(leaked))}"
    assert not set(REMOVED) & set(haddon.reports.__all__)


def test_public_surface_is_exactly_the_continuous_reports():
    functions = {name for name in dir(haddon.reports)
                 if not name.startswith("_") and inspect.isfunction(getattr(haddon.reports, name))}
    assert functions == set(ENTRY_POINT_SCOPE)
    assert set(haddon.reports.__all__) == functions | {
        "Line", "TrialBalance", "Movements", "IncomeStatement", "Register", "RegisterLine", "Composition"}


# 2. behaviour: one continuous journal
def test_a_single_trial_balance_spans_the_calendar_year_boundary(fx: Fixture):
    """No book and no carry row: a December posting and a January one are summed by one continuous balance."""
    post(fx, "2025-12-15", "1000000", debit="assets:cash", credit="income:sales", narration="sale Dec")
    post(fx, "2026-01-20", "250000", debit="assets:cash", credit="income:sales", narration="sale Jan")
    cash, sales = fx.acct("assets:cash"), fx.acct("income:sales")
    with fx.reader() as conn:
        dec = trial_balance(conn, fx.entity_id, through="2025-12-31")
        jan = trial_balance(conn, fx.entity_id, through="2026-01-31")
    assert dec.through == "2025-12-31" and jan.through == "2026-01-31"
    assert dec.get(cash, "USD") == m("1000000") and dec.get(sales, "USD") == m("-1000000")
    assert jan.get(cash, "USD") == m("1250000") and jan.get(sales, "USD") == m("-1250000")
    # continuity is the postings themselves: no annual boundary to carry across, and no books table to carry into
    assert fx.q("SELECT name FROM sqlite_master WHERE type='table' AND name='books'") == []
    assert fx.q("SELECT count(*) FROM entries WHERE origin='opening'") == [(0,)]


def test_movements_sum_only_the_range_and_exclude_openings(fx: Fixture):
    """A balance includes openings; a movement is the range and never the opening, and the P&L is its own slice."""
    with fx.tx(AGENT) as ctx:  # the opening is a balance-sheet posting like any other entry
        en.post_entry(ctx, dt="2025-12-31", narration="Opening", origin="opening",
                      legs=[Leg(fx.acct("assets:cash"), "USD", m("5000000")),
                            Leg(fx.acct("equity"), "USD", m("-5000000"))], _allow_reserved_origin=True)
    post(fx, "2025-06-01", "7000000", debit="assets:cash", credit="income:sales", narration="before the range")
    post(fx, "2025-12-15", "1000000", debit="assets:cash", credit="income:sales", narration="inside")
    post(fx, "2026-01-20", "250000", debit="assets:cash", credit="income:sales", narration="inside")
    post(fx, "2026-03-01", "900000", debit="assets:cash", credit="income:sales", narration="after the range")
    cash, sales, equity = fx.acct("assets:cash"), fx.acct("income:sales"), fx.acct("equity")
    with fx.reader() as conn:
        mv = movements(conn, fx.entity_id, start="2025-12-01", end="2026-01-31")
        pnl = income_statement(conn, fx.entity_id, start="2025-12-01", end="2026-01-31")
        tb = trial_balance(conn, fx.entity_id, through="2026-01-31")
    assert (mv.start, mv.end) == ("2025-12-01", "2026-01-31")
    assert mv.get(cash, "USD") == m("1250000")     # opening and out-of-range postings are excluded
    assert mv.get(sales, "USD") == m("-1250000")
    assert pnl.lines == tuple(ln for ln in mv.lines if ln.type in ("income", "expense"))  # P&L movements only
    assert pnl.net == {"USD": m("-1250000")}
    # the balance is the whole continuous history through the date, opening included (that is the difference)
    assert tb.get(cash, "USD") == m("13250000") and tb.get(sales, "USD") == m("-8250000")
    assert tb.get(equity, "USD") == m("-5000000")


def test_an_open_bound_is_allowed_on_either_side(fx: Fixture):
    post(fx, "2025-12-15", "1000000", debit="assets:cash", credit="income:sales")
    post(fx, "2026-01-20", "250000", debit="assets:cash", credit="income:sales")
    cash = fx.acct("assets:cash")
    with fx.reader() as conn:
        from_start = movements(conn, fx.entity_id, end="2025-12-31")
        to_end = movements(conn, fx.entity_id, start="2026-01-01")
        all_mv = movements(conn, fx.entity_id)
        tb_all = trial_balance(conn, fx.entity_id)
    assert (from_start.start, from_start.end) == (None, "2025-12-31")
    assert (to_end.start, to_end.end) == ("2026-01-01", None)
    assert from_start.get(cash, "USD") == m("1000000") and to_end.get(cash, "USD") == m("250000")
    assert (all_mv.start, all_mv.end) == (None, None) and all_mv.get(cash, "USD") == m("1250000")
    assert tb_all.through is None and tb_all.get(cash, "USD") == m("1250000")


def test_an_unknown_account_is_still_refused_and_an_absent_pair_reads_zero(fx: Fixture):
    with fx.reader() as conn:
        assert account_balance(conn, fx.entity_id, account_id=fx.acct("assets:cash"), commodity="JPY") == ZERO
        with pytest.raises(HaddonError) as ei:
            account_balance(conn, fx.entity_id, account_id="nope", commodity="USD")
        assert ei.value.code == "account_unknown"
        with pytest.raises(HaddonError) as ei:
            account_balance(conn, fx.entity_id, account_id=fx.acct("assets:cash"), commodity="GBP")
        assert ei.value.details[0]["reason"] == "commodity_unknown"
        with pytest.raises(HaddonError):
            register(conn, fx.entity_id, account_id="nope")
    assert not isinstance(ZERO, float) and isinstance(ZERO, Micros)


# 3. the range rule itself
def test_date_range_refuses_a_reversed_range_and_allows_an_open_bound():
    assert date_range("2025-01-01", "2025-12-31") == DateRange("2025-01-01", "2025-12-31")
    assert date_range(None, "2025-12-31") == DateRange(None, "2025-12-31")
    assert date_range("2025-01-01", None) == DateRange("2025-01-01", None)
    assert date_range(None, None) == DateRange(None, None)
    with pytest.raises(HaddonError) as ei:
        date_range("2026-01-31", "2026-01-01")
    e = ei.value
    assert (e.status, e.code) == (422, "validation")
    assert e.details[0]["reason"] == "range_order"


def test_scope_applies_the_same_rule(fx: Fixture):
    with fx.reader() as conn:
        assert scope(conn, fx.entity_id, start="2025-06-01").end is None
        assert scope(conn, fx.entity_id, end="2026-06-01").start is None
        with pytest.raises(HaddonError) as ei:
            scope(conn, fx.entity_id, start="2026-01-31", end="2026-01-01")
    assert ei.value.details[0]["reason"] == "range_order"
