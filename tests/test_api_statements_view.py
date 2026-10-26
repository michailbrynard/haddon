"""Issue 0008 section B: the statements are arranged once, and the JSON the interface reads is that arrangement.

The statement shape (ordering, sign, grouping, subtotals, the result row and the balance check) is built once,
in Python, and both the interface and the PDF draw from it.  Two implementations of one arrangement would let an
emailed document and the screen it was read from disagree, invisibly, because each is internally consistent.

``/reports/statements`` is the canonical shaped view.  Every assertion here checks it against an independent
source rather than against itself:

* the figures against the raw report lines the unshaped endpoints publish;
* the balance check against arithmetic done in the test, not read back from the response;
* the commodities and the footnote against the entries that were posted; and
* the refusals against the PDF face, which must refuse a period the same way.

The tests are in the fast tier: they post through the real API over Fernside Instruments' chart and need no
hledger import.
"""

from __future__ import annotations

import uuid

import pytest

from haddon.domain.micros import Commodity, Micros


def post_pair(api, dt: str, micros_str: str, *, commodity: str, debit: str, credit: str) -> None:
    """A balanced two-leg entry through the ordinary journal route.  ``micros_str`` is in micro-units."""
    decimal = Micros.parse(micros_str).to_decimal_string(Commodity(commodity, 2))
    body = {"dt": dt, "narration": f"{debit} / {credit}", "reason": "a posting for the statements test",
            "legs": [{"account": debit, "commodity": commodity, "amount": decimal},
                     {"account": credit, "commodity": commodity, "amount": f"-{decimal}"}]}
    response = api.post("/entries", key=str(uuid.uuid4()), json=body)
    assert response.status_code == 201, response.text


def usd_books(api) -> None:
    """A sale and a cost in the base commodity: profit 700.00, one asset of 700.00."""
    post_pair(api, "2026-02-04", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api, "2026-02-10", "30000000", commodity="USD", debit="expenses:office", credit="assets:cash")


# The shape agrees with the report
def test_the_income_statement_is_shaped_from_the_raw_movements(api_small):
    usd_books(api_small)
    raw = api_small.json("/reports/income-statement", params={"from": "2026-01-01", "to": "2026-12-31"})
    served = api_small.json("/reports/statements", params={"from": "2026-01-01", "to": "2026-12-31"})

    view = served["statements"][0]["income_statement"]
    assert served["commodity"] == "USD" and view["commodity"] == "USD"

    # Every shaped line is one raw line, turned the way a statement reads it (credit types negated).
    raw_line = {(ln["account_id"], ln["commodity"]): ln for ln in raw["lines"]}
    lines = [ln for s in view["sections"] for g in s["groups"] for ln in g["lines"]]
    sales = next(ln for ln in lines if ln["label"] == "sales")
    office = next(ln for ln in lines if ln["label"] == "office")

    # Income is stored credit-negative; the statement shows revenue positive, flipping the raw sign exactly once.
    raw_sales = raw_line[(sales["account_id"], "USD")]
    assert raw_sales["type"] == "income" and int(raw_sales["amount_micros"]) < 0
    assert sales["amount_micros"] == "100000000"
    assert int(sales["amount_micros"]) == -int(raw_sales["amount_micros"])

    # An expense is already shown in its own direction, so the raw figure passes through unchanged.
    raw_office = raw_line[(office["account_id"], "USD")]
    assert raw_office["type"] == "expense"
    assert office["amount_micros"] == "30000000" == raw_office["amount_micros"]

    # The bottom line is the service's own net, turned the statement's way: a profit is positive.
    assert view["result_micros"] == "70000000"
    assert view["result_label"] == "Profit for the period"


def test_the_balance_sheet_publishes_the_result_row_and_the_check(api_small):
    usd_books(api_small)
    served = api_small.json("/reports/statements", params={"from": "2026-01-01", "to": "2026-12-31"})
    sheet = served["statements"][0]["balance_sheet"]

    # Assets 700, funded by equity's result-to-date row, though nothing has posted to equity.
    assert sheet["total_assets_micros"] == "70000000"
    assert sheet["total_funding_micros"] == "70000000"
    # the check is first-class output, not a subtraction each reader performs: zero because the result is carried
    assert sheet["difference_micros"] == "0"
    assert sheet["checks"], "the check is published per column shown"
    assert sheet["checks"][0] == {"date": sheet["as_of"], "difference_micros": "0"}
    assert all(check["difference_micros"] == "0" for check in sheet["checks"]), sheet["checks"]

    equity = next(s for s in sheet["sections"] if s["key"] == "equity")
    synthetic = [ln for g in equity["groups"] for ln in g["lines"] if ln["account_id"] is None]
    assert [ln["label"] for ln in synthetic] == ["Result to date"]
    assert synthetic[0]["amount_micros"] == "70000000"

    # The raw report agrees on the same figure, from the other end.
    raw = api_small.json("/reports/balance-sheet", params={"as_of": sheet["as_of"]})
    assert raw["result_micros"]["USD"] == "-70000000", "the raw net is debit-positive; the statement negates it"


def test_a_comparative_column_matches_the_raw_prior_period(api_small):
    post_pair(api_small, "2025-03-15", "40000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2026-03-15", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")
    served = api_small.json("/reports/statements", params={"from": "2026-01-01", "to": "2026-03-31"})

    view = served["statements"][0]["income_statement"]
    assert served["comparative"] == {"start": "2025-10-01", "end": "2025-12-31", "name": "Q4 2025"}

    revenue = next(ln for s in view["sections"] for g in s["groups"] for ln in g["lines"]
                   if ln["account_id"] and ln["label"].startswith("sales"))
    assert revenue["amount_micros"] == "100000000"
    # The prior column is zero, not absent: the books were open through Q4 2025 (the 2025-03 posting predates it)
    # and nothing moved in it.  Having had no Q4 2025 at all is a different fact
    # (``test_a_book_whose_first_period_this_is_has_no_comparative``).
    assert revenue["comparative_micros"] == "0"
    assert view["comparative_result_micros"] == "0"


def test_a_book_whose_first_period_this_is_has_no_comparative(api_small):
    """Nothing was recorded before this period, so there is no period before it to publish a column for.

    A first period gets a null comparative, the same as ``comparative=false`` produces, rather than a column of
    zeros standing for a period these books never had.  Contrast the test above: a prior period that exists and
    is nil keeps its column and fills it with zeros.
    """
    usd_books(api_small)                                   # every posting is in 2026
    served = api_small.json("/reports/statements", params={"from": "2026-01-01", "to": "2026-03-31"})

    assert served["comparative"] is None
    view = served["statements"][0]["income_statement"]
    assert (view["comparative_start"], view["comparative_end"]) == (None, None)
    assert view["comparative_result_micros"] is None
    assert served["statements"][0]["balance_sheet"]["comparative_as_of"] is None
    lines = [ln for s in view["sections"] for g in s["groups"] for ln in g["lines"]]
    assert lines and all(ln["comparative_micros"] is None for ln in lines)

    # The check is stated once, for the one column there is.
    checks = served["statements"][0]["balance_sheet"]["checks"]
    assert [c["date"] for c in checks] == ["2026-03-31"]


def test_asking_for_no_comparative_and_having_none_look_the_same_on_the_wire(api_small):
    """One representation for "no second column", whichever reason there is for it.

    A caller rendering the response has one thing to branch on rather than two, and the interface's
    ``document.comparative !== null`` already is that branch.
    """
    usd_books(api_small)
    params = {"from": "2026-01-01", "to": "2026-03-31"}
    asked_without = api_small.json("/reports/statements", params={**params, "comparative": "false"})
    had_none = api_small.json("/reports/statements", params=params)
    assert asked_without["statements"] == had_none["statements"]
    assert asked_without["comparative"] is had_none["comparative"] is None


# Every commodity, and the footnote
def test_every_commodity_is_shaped_and_the_unnamed_ones_are_named(api_small):
    usd_books(api_small)
    post_pair(api_small, "2026-02-12", "5000000", commodity="EUR", debit="expenses:office", credit="assets:cash-eur")

    served = api_small.json("/reports/statements", params={"from": "2026-01-01", "to": "2026-12-31"})
    assert served["commodity"] == "USD"
    assert [s["commodity"] for s in served["statements"]] == ["USD", "EUR"]
    assert served["other_commodities"] == ["EUR"]
    assert "EUR" in served["footnote"], "a single-commodity statement must say what it leaves out"
    assert "statement sign" in served["footnote"]

    # Each commodity's own statement is self-consistent and balances on its own.
    for per in served["statements"]:
        sheet = per["balance_sheet"]
        assert sheet["commodity"] == per["commodity"]
        assert sheet["difference_micros"] == "0", per["commodity"]


# The refusal is the PDF's refusal
def test_a_period_that_has_not_begun_is_refused_the_same_way_as_the_pdf(api_small):
    usd_books(api_small)
    params = {"from": "2099-01-01", "to": "2099-03-31"}
    json_response = api_small.get("/reports/statements", params=params)
    pdf_response = api_small.get("/reports/statements.pdf", params=params)
    assert json_response.status_code == pdf_response.status_code == 422
    assert json_response.json()["error"]["details"][0]["reason"] == "in_the_future"
    assert pdf_response.json()["error"]["details"][0]["reason"] == "in_the_future"


@pytest.mark.parametrize("path", ["/reports/statements", "/reports/statements.pdf"])
def test_an_unknown_commodity_is_a_404_on_both_faces(api_small, path):
    response = api_small.get(path, params={"from": "2026-01-01", "to": "2026-12-31", "commodity": "XYZ"})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


# A label belongs to the statement it is the bottom line of
def test_each_commodity_is_told_whether_its_own_bottom_line_is_a_profit(api_small):
    """A document of several commodities has several bottom lines, and one of them says nothing about the rest.

    The interface prints this label under the figure it belongs to.  A single document-wide label read off the
    leading commodity is how a loss comes to be headed "Profit for the period", the figure and the word over it
    disagreeing on the same screen.
    """
    usd_books(api_small)                                                            # USD: a profit of 700.00
    post_pair(api_small, "2026-02-12", "5000000", commodity="EUR",                   # EUR: only a cost
              debit="expenses:office", credit="assets:cash-eur")

    served = api_small.json("/reports/statements", params={"from": "2026-01-01", "to": "2026-12-31"})
    labels = {s["commodity"]: s["income_statement"]["result_label"] for s in served["statements"]}
    results = {s["commodity"]: s["income_statement"]["result_micros"] for s in served["statements"]}

    assert results == {"USD": "70000000", "EUR": "-5000000"}
    assert labels == {"USD": "Profit for the period", "EUR": "Loss for the period"}
    assert "result_label" not in served, "the label is per statement; a document-wide one is a label over a "\
                                        "figure it does not describe"


def test_a_commodity_only_the_comparative_column_held_is_still_named(api_small):
    """A position that is gone by the period end was still there in the column beside it.

    ``other_commodities`` and the footnote are read off both columns.  Built from the current column alone they
    would miss a commodity whose balances netted to zero since the comparative date, while the document still
    shows that date's column.
    """
    created = api_small.post("/accounts", key="acct-eur-payables",
                             json={"path": "liabilities:payables-eur", "type": "liability",
                                   "commodities": ["EUR"], "is_cash": False})
    assert created.status_code == 201, created.text

    post_pair(api_small, "2026-02-04", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")
    # an EUR position taken on before the comparative date and settled inside the period being reported
    post_pair(api_small, "2025-11-10", "5000000", commodity="EUR",
              debit="assets:cash-eur", credit="liabilities:payables-eur")
    post_pair(api_small, "2026-01-15", "5000000", commodity="EUR",
              debit="liabilities:payables-eur", credit="assets:cash-eur")

    served = api_small.json("/reports/statements", params={"from": "2026-01-01", "to": "2026-03-31"})
    assert served["comparative"]["end"] == "2025-12-31", "the comparative column is drawn where EUR was held"
    assert served["other_commodities"] == ["EUR"]
    assert "EUR" in served["footnote"]
