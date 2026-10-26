"""Chunk G5: the statements as a document somebody sends somebody else.

A PDF is the one artefact of this system that leaves it.  It is emailed, filed and forwarded, and by the time
it is read the request that produced it is gone.  So every check here is about the document being unable to be
handsome and wrong at the same time:

* the figures on the page are the figures the API returns, to the micro, read back out of the PDF rather than
  out of the renderer's own data structures;
* the income statement is headed for the period asked for and the balance sheet is headed as at a date,
  because a balance sheet labelled with a date range is the commonest way one of these misleads;
* the open book, the unclosed periods and the composition of the period are on the face of the document, not
  only on a screen somebody had open once;
* and the two sides of the balance sheet are shown to meet, not asserted to.

These tests must not be able to pass against a document generated earlier.  Every assertion here reads bytes
returned by the request under test, so nothing is written to a path a previous run could have left behind, and
``test_the_document_is_drawn_fresh_on_every_request`` posts an entry between two requests and requires the
second document to have moved.
"""

from __future__ import annotations

import re

import pytest

from haddon.domain import dates
from haddon.domain.micros import Micros
from haddon.repository.ctx import WriteCtx
from haddon.repository.entries import Leg, post_entry
from haddon.repository.transaction import write_transaction
from haddon.reports import income_statement, trial_balance
from haddon.statements.period import a_year_earlier, classify, preceding

pytestmark = pytest.mark.slow

YEAR = 2025
#: one calendar year as a date range; a statement is a range over one continuous journal
START, END = "2025-01-01", "2025-12-31"


def pdf_text(body: bytes) -> str:
    from pypdf import PdfReader
    from io import BytesIO

    reader = PdfReader(BytesIO(body))
    return "\n".join(page.extract_text() for page in reader.pages)


def pdf_pages(body: bytes) -> int:
    from pypdf import PdfReader
    from io import BytesIO

    return len(PdfReader(BytesIO(body)).pages)


def grouped(micros: str, precision: int = 2) -> str:
    """The same figure the document writes: whole units, thousands separated."""
    negative = micros.startswith("-")
    digits = (micros[1:] if negative else micros).rjust(7, "0")
    whole, fraction = digits[:-6], digits[-6:][:precision]
    return f"{'-' if negative else ''}{int(whole):,}{'.' + fraction if fraction else ''}"


@pytest.fixture
def inc_conn(api_inc):
    """A reader on the same file the app is serving, for the independent comparison."""
    engine = api_inc.read_engine()
    with engine.connect() as conn:
        yield conn
    engine.dispose()


@pytest.fixture
def statements(api_inc):
    """The document for the whole 2025 book, and the response it came in."""
    response = api_inc.get(f"/reports/statements.pdf?from={START}&to={END}")
    assert response.status_code == 200, response.text[:400]
    return response


# it is a PDF, and it is served as one
def test_a_period_named_with_an_en_dash_is_still_downloadable(api_inc):
    """A custom range names itself from its bounds, and that name has to survive being a header.

    ``1 February – 15 June 2025`` is an ordinary thing to ask for, and its en dash is not in latin-1 -- which
    is what a header value is encoded as.  Before this the document rendered, the response was built, and the
    request then died with ``UnicodeEncodeError`` and rolled back: a 500 on a report with nothing wrong with
    it.  So the name is carried twice, ASCII and UTF-8, per RFC 6266.
    """
    response = api_inc.get("/reports/statements.pdf?from=2025-02-01&to=2025-06-15")
    assert response.status_code == 200, response.text[:400]
    assert response.content[:5] == b"%PDF-"

    disposition = response.headers["content-disposition"]
    # the header the transport actually has to be able to write
    disposition.encode("latin-1")
    # the real name, percent-encoded, is the one every current browser takes
    assert "filename*=UTF-8''" in disposition and "%E2%80%93" in disposition
    # and the plain filename is still there for anything that only reads that, with the dash folded not dropped
    assert re.search(r'filename="[\x20-\x7e]+\.pdf"', disposition)
    assert "1-feb" in disposition and "15-jun" in disposition

    # the period is headed the way it named itself, so the file and the page agree
    assert "For the period 1 February – 15 June 2025" in pdf_text(response.content)


def test_the_response_is_a_pdf_offered_as_a_download(statements):
    assert statements.headers["content-type"] == "application/pdf"
    assert statements.content[:5] == b"%PDF-"
    disposition = statements.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="') and ".pdf" in disposition
    # the name is given twice, ASCII and UTF-8, so a period whose name is not ASCII can still be downloaded
    assert disposition.endswith("-statements.pdf")
    assert "filename*=UTF-8''" in disposition
    assert "2025" in disposition
    # a statement of an open book is a snapshot of a moment: a cache would hand back yesterday's figures
    assert statements.headers["cache-control"] == "no-store"


# the figures are the API's figures
def test_every_figure_on_the_page_is_the_figure_the_api_returns(api_inc, inc_conn, statements):
    """Read back out of the PDF, compared with ``haddon.reports`` and not with the renderer's own numbers.

    This is the assertion the whole document rests on. A PDF that agreed only with the code that drew it would
    prove that WeasyPrint ran, not that the statement is of these books.
    """
    text = pdf_text(statements.content)
    stmt = income_statement(inc_conn, api_inc.entity_id, start=START, end=END)
    sheet = trial_balance(inc_conn, api_inc.entity_id, through=END)

    seen = 0
    for line in (*stmt.lines, *sheet.of_type("asset", "liability", "equity")):
        if line.commodity != "USD":
            continue
        # statement sign: the credit types read positive, which is what the document writes
        signed = -line.amount if line.type in ("income", "liability", "equity") else line.amount
        assert grouped(signed.render()) in text, f"{line.path} {signed.render()} is not on the page"
        seen += 1
    assert seen > 5, seen

    # and the one figure the statement is read for
    assert grouped((-stmt.net["USD"]).render()) in text


def test_the_two_sides_of_the_balance_sheet_are_shown_to_meet(statements):
    text = pdf_text(statements.content)
    assert "Assets equal liabilities plus equity" in text
    assert "Total liabilities and equity" in text and "Total assets" in text
    assert "differ by" not in text


# headed for what it is
def test_the_income_statement_is_headed_for_the_period_asked_for(api_inc):
    response = api_inc.get("/reports/statements.pdf?from=2025-04-01&to=2025-06-30")
    assert response.status_code == 200
    text = pdf_text(response.content)
    assert "For the period 1 April – 30 June 2025" in text
    # the range names itself: the caller sent dates and the document worked out that they are a quarter
    assert "Q2 2025" in text and "Q1 2025" in text


def test_a_balance_sheet_is_headed_as_at_a_date_and_never_with_a_range(api_inc):
    response = api_inc.get("/reports/statements.pdf?from=2025-04-01&to=2025-06-30")
    text = pdf_text(response.content)
    # the balance sheet's own heading block: the statement name, then the date it is drawn at
    assert re.search(r"^BALANCE SHEET\nAs at 30 June 2025$", text, flags=re.M)
    # and the one heading it must never carry
    assert not re.search(r"(?i)^balance sheet\nfor the period", text, flags=re.M)
    assert "As at 1 April" not in text
    # while the income statement, which is for a period, is headed that way
    assert re.search(r"^INCOME STATEMENT\nFor the period 1 April – 30 June 2025$", text, flags=re.M)


# it says what it is
def test_an_open_book_is_marked_draft_and_says_when_it_was_drawn(api_inc, statements):
    """The marking and the provenance line, and nothing else.

    A statement of a book still open to posting is a draft as a matter of record rather than of opinion, so
    the marking follows ``lock_through`` and is not a choice anybody makes. The footer is what tells two copies
    of the same statement apart: an open book moves, and a document that could not be dated against the record
    would be indistinguishable from one drawn a month earlier.
    """
    text = pdf_text(statements.content)
    assert api_inc.scalar("SELECT through FROM period_locks") is None  # nothing is closed
    assert "DRAFT" in text
    assert re.search(r"Drawn \d+ \w+ \d{4}, \d{2}:\d{2} UTC", text)
    assert "All amounts in USD" in text


# what the page deliberately does not say
def test_the_event_sequence_is_not_printed_on_the_document(api_inc, statements):
    """The date it was drawn is provenance a reader can act on; the event sequence is not.

    ``snapshot_seq`` is this service's own bookkeeping -- the position in the event log the figures were read
    at -- and on an emailed PDF it is a number whose reader has nothing to look it up against.  It stays on
    the JSON faces, whose reader is a program that can.  This is the whole of the check: the string is gone
    from the page and still published beside the same figures.
    """
    text = pdf_text(statements.content)
    assert "records to event" not in text
    assert re.search(r"Drawn \d+ \w+ \d{4}, \d{2}:\d{2} UTC", text)     # the dating itself is untouched

    served = api_inc.json(f"/reports/statements?from={START}&to={END}")
    assert served["snapshot_seq"] == api_inc.scalar("SELECT max(seq) FROM events")


def test_the_sign_convention_is_not_explained_on_the_document(api_inc, statements):
    """The paragraph explaining which way round the credit-natured accounts read is off the PDF, not gone.

    A statement is a form its reader already knows how to read, and the note was printed on every document
    that left the service.  The JSON face keeps it: its reader is a program or an interface building its own
    page, and neither can be assumed to know the convention the way an accountant holding the PDF does.
    """
    text = pdf_text(statements.content)
    assert "statement sign" not in text
    assert "the other way round" not in text

    served = api_inc.json(f"/reports/statements?from={START}&to={END}")
    assert "Shown in statement sign" in served["footnote"]


# every page is placed
def test_every_page_is_numbered_and_the_count_is_the_real_one(statements):
    pages = pdf_pages(statements.content)
    text = pdf_text(statements.content)
    assert pages >= 1
    for n in range(1, pages + 1):
        assert f"Page {n} of {pages}" in text


# it cannot be a stale document
def test_the_document_is_drawn_fresh_on_every_request(api_inc):
    """The trap this is here for: a test that passes against a document generated before the change it claims
    to check. Nothing here touches a path on disk, and a write between the two requests has to move the page."""
    before = api_inc.get(f"/reports/statements.pdf?from={START}&to={END}")
    assert before.status_code == 200
    amount = "3141590000"  # 3,141.59 USD: a whole multiple of the commodity quantum
    expense = api_inc.scalar("SELECT id FROM accounts WHERE type='expense' AND archived=0 LIMIT 1")
    cash = api_inc.scalar("SELECT id FROM accounts WHERE is_cash=1 AND archived=0 LIMIT 1")
    engine = api_inc.engine()
    try:
        with write_transaction(engine) as conn:
            ctx = WriteCtx.open(conn, "agent:test")
            post_entry(ctx, dt=f"{YEAR}-06-15", narration="A posting made between two documents",
                       origin="manual",
                       legs=[Leg(expense, "USD", Micros.parse(amount)),
                             Leg(cash, "USD", Micros.parse(f"-{amount}"))])
    finally:
        engine.dispose()

    moved = api_inc.json(f"/reports/income-statement?from={START}&to={END}")["net_micros"]["USD"]
    result = grouped(str(-int(moved)))          # the bottom line, the way the statement writes it
    after = api_inc.get(f"/reports/statements.pdf?from={START}&to={END}")
    assert after.status_code == 200
    assert result not in pdf_text(before.content), "the first document already held the figure the write made"
    assert result in pdf_text(after.content)


# nothing is reported before it happens
@pytest.fixture
def mid_august(monkeypatch):
    """Today, pinned. Depending on the real clock would make this suite start failing on a date nobody chose."""
    monkeypatch.setattr(dates, "today", lambda: "2025-08-15")
    return "2025-08-15"


def test_a_period_that_has_not_begun_is_refused_rather_than_drawn_empty(api_inc, mid_august):
    """An income statement for a quarter that does not exist yet can only be empty, and an empty statement of
    a quarter that has not begun is not a fact about the books."""
    response = api_inc.get("/reports/statements.pdf?from=2025-10-01&to=2025-12-31")
    assert response.status_code == 422
    detail = response.json()["error"]["details"][0]
    assert detail["reason"] == "in_the_future"
    assert detail["today"] == mid_august and detail["value"] == "2025-10-01"


@pytest.mark.parametrize("path,reason", [
    ("/reports/income-statement?from=2025-10-01&to=2025-12-31", "in_the_future"),
    ("/reports/composition?from=2025-10-01&to=2025-12-31", "in_the_future"),
    ("/reports/balance-sheet?as_of=2025-12-31", "in_the_future"),
    ("/reports/trial-balance?as_of=2025-12-31", "in_the_future"),
])
def test_every_report_refuses_a_date_the_books_have_not_reached(api_inc, mid_august, path, reason):
    response = api_inc.get(path)
    assert response.status_code == 422, response.text[:300]
    assert response.json()["error"]["details"][0]["reason"] == reason


def test_a_range_that_merely_ends_in_the_future_is_a_normal_thing_to_ask_for(api_inc, mid_august):
    """Flows and stocks differ: the quarter you are standing in reports what has happened so far, while a
    position at a future date is not a partial answer to anything."""
    ok = api_inc.get("/reports/income-statement?from=2025-07-01&to=2025-09-30")
    assert ok.status_code == 200
    assert (ok.json()["start"], ok.json()["end"]) == ("2025-07-01", "2025-09-30")


def test_a_period_still_running_is_drawn_to_date_and_headed_that_way(api_inc, mid_august):
    """The heading must not be able to claim more than the figures cover. A PDF outlives the request that made
    it, so `Q3 2025` on a document drawn in mid-August would assert a quarter's worth of figures forever."""
    response = api_inc.get("/reports/statements.pdf?from=2025-07-01&to=2025-09-30")
    assert response.status_code == 200
    text = pdf_text(response.content)
    assert "For the period 1 July – 15 August 2025 (quarter to date)" in text
    assert "Q3 2025 TO DATE" in text.upper()               # the column head is set in caps and says it too
    assert re.search(r"^BALANCE SHEET\nAs at 15 August 2025$", text, flags=re.M)
    assert "30 September" not in text
    # This book begins on 1 January 2025, so the span a running period would be compared with (the same dates
    # a year earlier) is one these books have no record of, and no column is drawn for it.  Which column a
    # running period would be compared with, given a book that reaches back that far, is
    # `test_a_running_period_is_compared_with_the_same_dates_a_year_earlier` below.
    assert "15 AUG 2024" not in text.upper() and "1 JUL – 15 AUG 2024" not in text.upper()
    assert "Q2 2025" not in text


def test_a_period_wholly_in_the_past_is_untouched(api_inc, mid_august):
    response = api_inc.get("/reports/statements.pdf?from=2025-04-01&to=2025-06-30")
    assert response.status_code == 200
    text = pdf_text(response.content)
    assert "For the period 1 April – 30 June 2025" in text
    # no to-date wording anywhere it would be a claim: not in the heading, not in a column head.  "Result to
    # date" is a row label on every balance sheet and means something else entirely: the span from the
    # journal's beginning, which is the same at every date the sheet is drawn at.
    assert "(quarter to date)" not in text and "(year to date)" not in text
    assert "TO DATE" not in text.upper().replace("RESULT TO DATE", "").replace("THE RESULT TO DATE", "")


# year to date
@pytest.fixture
def mid_september(monkeypatch):
    """Today, pinned. A suite that read the real clock would start failing on a date nobody chose."""
    monkeypatch.setattr(dates, "today", lambda: "2026-09-12")
    return "2026-09-12"


def ytd_pdf(api, year=2026):
    """The document a YTD request produces: the whole calendar year, which today cuts short."""
    response = api.get(f"/reports/statements.pdf?from={year}-01-01&to={year}-12-31")
    assert response.status_code == 200, response.text[:300]
    return pdf_text(response.content)


def test_a_year_to_date_is_headed_to_date_and_drawn_at_today(api_small, mid_september):
    text = ytd_pdf(api_small)
    assert "For the period 1 January – 12 September 2026 (year to date)" in text
    assert "2026 TO DATE" in text.upper()
    assert re.search(r"^BALANCE SHEET\nAs at 12 September 2026$", text, flags=re.M)
    assert "31 December 2026" not in text


def test_the_comparative_for_a_year_to_date_is_the_same_span_and_not_the_whole_prior_year(api_small, mid_september):
    """Nine months against twelve is wrong by roughly a third, and wrong in the flattering direction.

    The column is headed by its own dates rather than by ``2025``, so the span is stated and not implied.
    """
    from test_api_statements_view import post_pair

    # a book that reaches back into the span being compared with; without one there is nothing to compare
    # against and the document is drawn in a single column (see "one column, or two" below)
    post_pair(api_small, "2025-03-20", "4000000", commodity="USD", debit="assets:cash", credit="income:sales")

    text = ytd_pdf(api_small)
    assert "1 JAN – 12 SEP 2025" in text.upper()
    assert "12 SEP 2025" in text.upper()            # the comparative balance sheet is drawn there too
    assert "31 DEC 2025" not in text.upper()        # and never at the prior year's end


def test_a_running_period_is_compared_with_the_same_dates_a_year_earlier(api_small, mid_august):
    """The choice of comparative span for a running period, on a book that reaches back far enough to have one.

    ``test_a_period_still_running_is_drawn_to_date_and_headed_that_way`` asserts the headings against the real
    imported book, which begins in 2025 and therefore has no 2024 column to draw.  This is the other half: the
    quarter before a running quarter has finished and the running one has not, so the comparison is the same
    dates a year back, headed by those dates so the span is stated rather than implied.
    """
    from test_api_statements_view import post_pair

    post_pair(api_small, "2024-07-20", "4000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2025-07-20", "9000000", commodity="USD", debit="assets:cash", credit="income:sales")

    response = api_small.get("/reports/statements.pdf?from=2025-07-01&to=2025-09-30")
    assert response.status_code == 200, response.text[:300]
    text = pdf_text(response.content)
    assert "1 JUL – 15 AUG 2024" in text.upper()
    assert "Q2 2025" not in text, "a running quarter is not compared with the whole quarter before it"


# one arrangement, two faces (issue 0008 section B)
def test_the_json_document_is_the_statement_the_pdf_draws(api_small, mid_september):
    """Every shaped figure ``/reports/statements`` publishes is read back out of the PDF bytes.

    The interface renders that JSON; the PDF is drawn server-side.  They come from one arrangement
    (``haddon.statements.shape``), and this is the assertion that keeps it that way: a sign, a subtotal, a total,
    the bottom line, the footnote and the balance check all have to be on the page, not merely in the response.
    """
    from test_api_statements_view import post_pair

    post_pair(api_small, "2026-02-04", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2026-02-10", "30000000", commodity="USD", debit="expenses:office", credit="assets:cash")

    text = ytd_pdf(api_small)
    served = api_small.json("/reports/statements?from=2026-01-01&to=2026-12-31")
    per = next(s for s in served["statements"] if s["commodity"] == "USD")
    view, sheet = per["income_statement"], per["balance_sheet"]

    # the heading the document is drawn with is the one the JSON names, worked out from the same bounds
    assert f"For the period {served['period']['heading']}" in text

    seen = 0
    for section in (*view["sections"], *sheet["sections"]):
        assert grouped(section["total_micros"]) in text, section["title"]
        for group in section["groups"]:
            for line in group["lines"]:
                if line["account_id"] is not None:
                    assert grouped(line["amount_micros"]) in text, line["label"]
                    seen += 1
    assert seen >= 3, seen

    assert grouped(view["result_micros"]) in text
    assert view["result_label"] in text
    assert grouped(sheet["total_funding_micros"]) in text
    assert "Assets equal liabilities plus equity" in text

    # and the JSON's own check is the one the page states, not a second subtraction
    assert sheet["difference_micros"] == "0"
    assert "differ by" not in text


def test_the_omitted_commodities_are_the_ones_the_json_document_names(api_small):
    """The footnote is one list, so an emailed document cannot be short of a commodity the screen named.

    The failing case is a commodity the comparative column held and the current one does not: a balance settled
    inside the period is gone from the sheet drawn at its end, and a list built from that end alone drops it.
    Both faces draw the comparative column, so both have to account for it.
    """
    from test_api_statements_view import post_pair

    created = api_small.post("/accounts", key="acct-eur-payables-pdf",
                             json={"path": "liabilities:payables-eur", "type": "liability",
                                   "commodities": ["EUR"], "is_cash": False})
    assert created.status_code == 201, created.text
    post_pair(api_small, "2026-02-04", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2025-11-10", "5000000", commodity="EUR",
              debit="assets:cash-eur", credit="liabilities:payables-eur")
    post_pair(api_small, "2026-01-15", "5000000", commodity="EUR",
              debit="liabilities:payables-eur", credit="assets:cash-eur")

    params = {"from": "2026-01-01", "to": "2026-03-31"}
    served = api_small.json("/reports/statements", params=params)
    response = api_small.get("/reports/statements.pdf", params=params)
    assert response.status_code == 200, response.text[:400]

    assert served["other_commodities"] == ["EUR"]
    text = pdf_text(response.content)
    clause = re.search(r"These books also hold amounts in ([^.]*) in this period", text)
    assert clause, "the document does not say what it leaves out, and the JSON face does"
    assert clause.group(1).strip() == "EUR"


# one column, or two (issue 0011 change 3)
# The difference these two tests exist to hold apart:
#
#   a book whose first period this is       -> nothing to compare against -> one column
#   a period before this one in which       -> something to compare against, and what it says is "nothing
#   nothing happened                           moved" -> two columns, the second of them zeros
#
# They look identical in the figures and are not the same fact, and "every number in the prior column is zero"
# cannot tell them apart.  That is why the suppression is on whether anything was ever recorded at or before the
# comparative date (``api.routes.reports._anything_recorded_through``) and not on the figures.  Both read the
# rendered PDF, because the column is a thing on a page: a test of the shaping alone (``show_comparative``) would
# pass against a document that still drew the empty column.
def quarter_pdf(api, from_="2026-01-01", to="2026-03-31"):
    response = api.get(f"/reports/statements.pdf?from={from_}&to={to}")
    assert response.status_code == 200, response.text[:300]
    return pdf_text(response.content)


def test_a_book_with_no_period_before_this_one_is_drawn_in_a_single_column(api_small, mid_september):
    """A first set of accounts has nothing beside it.  A blank column is not "nothing"; it is a comparison the
    reader will try to make and cannot.

    Every trace of the second column has to go with it: the column head, the figures, the comparative balance
    sheet date, and the balance check's "at both dates", which would otherwise claim a date the page does not
    show.
    """
    from test_api_statements_view import post_pair

    post_pair(api_small, "2026-02-04", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2026-02-10", "30000000", commodity="USD", debit="expenses:office", credit="assets:cash")

    text = quarter_pdf(api_small)
    # the column heads carry one period each, and the rows under them carry one figure each
    assert re.search(r"^ACCOUNT Q1 2026$", text, flags=re.M), text
    assert re.search(r"^ACCOUNT 31 MAR 2026$", text, flags=re.M), text
    assert re.search(r"^sales 100\.00$", text, flags=re.M), text
    assert re.search(r"^Profit for the period 70\.00$", text, flags=re.M), text
    # nothing anywhere names the period there is no column for
    assert "Q4 2025" not in text and "31 DEC 2025" not in text.upper()
    assert "at both dates" not in text

    # and the JSON face is the same statement: one column there too, not a null the interface has to guess at
    served = api_small.json("/reports/statements?from=2026-01-01&to=2026-03-31")
    assert served["comparative"] is None
    view = served["statements"][0]["income_statement"]
    assert view["comparative_start"] is None and view["comparative_end"] is None
    assert view["comparative_result_micros"] is None
    assert all(line["comparative_micros"] is None
               for section in view["sections"] for group in section["groups"] for line in group["lines"])
    assert served["statements"][0]["balance_sheet"]["comparative_as_of"] is None


def test_a_period_before_this_one_in_which_nothing_moved_still_heads_a_column(api_small, mid_september):
    """Zeros are an answer.  The column stays, and it reads zero.

    The books were open through the whole of Q4 2025 and every figure in it is nil, which is a fact about these
    books and the one a reader of a quiet quarter has come for.  Collapsing the column here would leave them
    unable to tell a quiet quarter from a book that did not exist yet -- the two states this pair holds apart --
    so this is the test that fails if the suppression is ever re-cut as "the prior column is all zeros".
    """
    from test_api_statements_view import post_pair

    # posted inside the comparative quarter and reversed inside it: a real period, and nil at the end of it
    post_pair(api_small, "2025-11-04", "1000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2025-11-05", "1000000", commodity="USD", debit="income:sales", credit="assets:cash")
    post_pair(api_small, "2026-02-04", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")

    text = quarter_pdf(api_small)
    assert re.search(r"^ACCOUNT Q1 2026 Q4 2025$", text, flags=re.M), text
    assert re.search(r"^ACCOUNT 31 MAR 2026 31 DEC 2025$", text, flags=re.M), text
    assert re.search(r"^sales 100\.00 0\.00$", text, flags=re.M), text
    assert re.search(r"^Profit for the period 100\.00 0\.00$", text, flags=re.M), text
    assert "at both dates" in text, "the check is stated per column, and there are two of them"

    served = api_small.json("/reports/statements?from=2026-01-01&to=2026-03-31")
    assert served["comparative"] == {"start": "2025-10-01", "end": "2025-12-31", "name": "Q4 2025"}
    view = served["statements"][0]["income_statement"]
    assert view["comparative_result_micros"] == "0", "zero, which is not the same as absent"


def test_a_comparative_column_is_kept_when_the_record_before_it_is_outside_it(api_small, mid_september):
    """The boundary the check is drawn at: a posting before the comparative period, not inside it.

    The comparative balance sheet is cumulative -- it is drawn AT its date, not over its span -- so a book with
    a position taken on before the comparative quarter has a comparative column with real figures in it even
    though nothing was posted during that quarter.  The question is whether anything stands at the comparative
    date, which is why the check is ``<= prior.end`` and not "inside the prior period".
    """
    from test_api_statements_view import post_pair

    post_pair(api_small, "2025-03-15", "40000000", commodity="USD", debit="assets:cash", credit="income:sales")
    post_pair(api_small, "2026-02-04", "100000000", commodity="USD", debit="assets:cash", credit="income:sales")

    text = quarter_pdf(api_small)
    assert re.search(r"^ACCOUNT Q1 2026 Q4 2025$", text, flags=re.M), text
    # nothing moved in Q4 2025, so the income statement column is nil, and the sheet beside it is not
    assert re.search(r"^Total revenue 100\.00 0\.00$", text, flags=re.M), text
    assert re.search(r"^cash 140\.00 40\.00$", text, flags=re.M), text
