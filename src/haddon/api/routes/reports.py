"""Trial balance, balance sheet, income statement and composition, each over one continuous journal.

A journal is continuous (issue 0002), so every route takes an inclusive ``from``/``to`` date range rather than a
book year.  ``trial-balance`` and ``balance-sheet`` are at a date and take ``as_of`` (an open ``through``);
``income-statement`` and ``composition`` take the range itself.  An omitted bound is open: no ``from`` means
"from the beginning", no ``to`` means "through the latest posting".

The balance sheet is the trial balance's asset, liability and equity lines, grouped.  The income statement is the
income and expense movements, with the opening entry excluded.  Both come from the same computation the register
and the verifier use, so the three cannot disagree about what was booked.

``totals`` is the per-commodity net of the lines in the report.  On a trial balance that is every posted amount,
so on a balanced book it is zero and works as a cheap integrity check.  On a balance sheet it is the net of the
asset, liability and equity sections alone, and it is not zero: nothing posts the result to equity in a
continuous journal, so the accumulated income and expense is the missing side.  The balance sheet therefore also
carries ``result_micros``, the figure ``/income-statement`` reports with an open start through the same date, and
``totals[c] + result_micros[c]`` balances to zero.  That is the row the UI and the PDF both draw inside equity.

``composition`` reports no amounts, only how many posted entries a period holds per origin and per source
journal.  It sits here rather than beside ``/close-checks`` because its question ("is this statement thin because
the period was quiet, or because only part of the book is loaded?") comes up while reading one of the other
reports, over the same date range.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Response

from haddon.api import schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.evidence import content_disposition
from haddon.api.registry import entity_context
from haddon.api.scoping import not_yet_happened, scoped_range
from haddon.api.wire import entity_base, micros_map, scope_fields
from haddon.domain import dates
from haddon.domain.errors import not_found, validation
from haddon.repository import tables as t
from haddon.repository.entries import lock_through
from haddon.repository.events import high_water
from haddon.repository.provisioning import get_commodity
from haddon.reports.balances import income_statement, trial_balance
from haddon.reports.composition import composition
from haddon.reports.postings import BALANCE_SHEET_TYPES, net_by_commodity
from haddon.statements import period as periods
from haddon.statements.period import Period
from haddon.statements.render import ColumnLabels, DocumentContext, document_html, drawn_words, to_pdf
from haddon.statements.shape import (balance_sheet_view, commodities_of, income_statement_view,
                                     sign_footnote)

router = APIRouter(prefix="/v1/entities/{entity_id}/reports", tags=["reports"], responses=schemas.ERRORS)


def _url(entity_id: str, path: str, **params: str | None) -> str:
    query = "&".join(f"{name}={value}" for name, value in params.items() if value is not None)
    base = f"{entity_base(entity_id)}/reports/{path}"
    return f"{base}?{query}" if query else base


def _links(entity_id: str, start: str | None, end: str | None) -> dict[str, str]:
    """Link the sibling reports over the same range.  An open bound is left out of the query, not spelled ``None``."""
    return {"trial_balance": _url(entity_id, "trial-balance", as_of=end),
            "balance_sheet": _url(entity_id, "balance-sheet", as_of=end),
            "income_statement": _url(entity_id, "income-statement", **{"from": start, "to": end}),
            "composition": _url(entity_id, "composition", **{"from": start, "to": end}),
            "statements_pdf": _url(entity_id, "statements.pdf", **{"from": start, "to": end}),
            "accounts": f"{entity_base(entity_id)}/accounts"}


# One statement shape, shared by the JSON and the PDF (issue 0008 section B).  The arrangement of a statement
# (sections, groups, signs, subtotals, the result row, and whether the two sides of the sheet meet) is decided
# once, in ``haddon.statements.shape``.  These functions are the only path from the journal to that arrangement,
# so ``/reports/statements`` (JSON, which the interface renders) and ``/reports/statements.pdf`` cannot be built
# from different numbers.
def _anything_recorded_through(conn, entity_id: str, date: str) -> bool:
    """Report whether these books hold a posted entry at or before ``date``.

    That is whether a period before this one exists in these books, as opposed to merely having a name.  The
    comparative is derived from the calendar (``haddon.statements.period``), so a prior period can always be
    named, even for a book's first year.  But a column of blanks under that name compares the period against
    nothing while looking like a comparison.

    The question is about a record, not a figure.  A prior period whose figures are all zero is still a prior
    period: the books were open and nothing moved, and a reader should be shown that.  Suppressing the column
    because every number is zero would make "nothing happened" and "nothing was ever recorded" look the same.
    ``status == 'posted'`` matches ``_period_bounds`` below: a draft is not yet a record.
    """
    e = t.entries
    row = conn.execute(sa.select(e.c.id).where(e.c.entity_id == entity_id, e.c.status == "posted",
                                               e.c.dt <= date).limit(1)).first()
    return row is not None


def _statement_inputs(conn, entity_id: str, start: str | None, end: str | None, comparative: bool):
    """Compute the reports a statement document is built from, over the period asked for, cut to today.

    A period that has not begun is refused here, once for both the JSON and the PDF: an empty statement of a
    quarter that does not exist yet is not a fact about the books.

    A comparative column the books cannot supply is dropped rather than drawn empty (see
    ``_anything_recorded_through`` for what "cannot supply" means).  It is dropped here, before either output is
    built, so a single-column statement is single-column on screen, in the JSON and in the PDF alike.  ``prior``
    being ``None`` is the one signal the shape layer reads for this.  A first year is the ordinary case for a set
    of books, not an edge case.
    """
    now = dates.today()
    period_start, period_end = _period_bounds(conn, entity_id, *scoped_range(conn, entity_id, start=start, end=end))
    asked = periods.classify(period_start, period_end)
    if not periods.has_begun(asked, now):
        raise validation(
            f"the period {asked.start}..{asked.end} has not begun; these books can be reported through {now}",
            path="from", reason="in_the_future", value=asked.start, today=now)
    # A period still running is cut off at today, and every heading taken from it then says "to date" rather
    # than claiming a whole quarter it does not have.
    period = periods.through_today(asked, now)
    prior = _comparative_period(period) if comparative else None
    if prior is not None and not _anything_recorded_through(conn, entity_id, prior.end):
        prior = None

    stmt = income_statement(conn, entity_id, start=period.start, end=period.end)
    sheet = trial_balance(conn, entity_id, through=period.end)
    # The result carried inside equity: every income and expense movement from the start of the journal
    # through this date, none of which any entry has posted to equity.  The open start is what makes the two
    # sides meet at any date; a result covering only this period would leave earlier profit in neither equity
    # nor the result row.
    result = income_statement(conn, entity_id, start=None, end=period.end)

    prior_stmt = prior_sheet = prior_result = None
    if prior is not None:
        prior_stmt = income_statement(conn, entity_id, start=prior.start, end=prior.end)
        prior_sheet = trial_balance(conn, entity_id, through=prior.end)
        prior_result = income_statement(conn, entity_id, start=None, end=prior.end)
    return period, prior, stmt, sheet, result, prior_stmt, prior_sheet, prior_result


def _statement_views(stmt, sheet, result, prior_stmt, prior_sheet, prior_result, code: str):
    return (income_statement_view(stmt, prior_stmt, commodity=code),
            balance_sheet_view(sheet, result, prior_sheet, prior_result, commodity=code))


def _commodities(code: str, stmt, sheet, prior_stmt, prior_sheet) -> list[str]:
    """Return every commodity this period holds, the requested one first even when it has no line of its own.

    Both columns are read.  A commodity only the comparative column holds is still shown by the document and
    must be accounted for; a balance that has since netted to zero is exactly what reading only the current
    column would silently drop.
    """
    seen = commodities_of(code, list(stmt.lines), list(sheet.lines),
                          list(prior_stmt.lines) if prior_stmt else [],
                          list(prior_sheet.lines) if prior_sheet else [])
    return [code, *(c for c in seen if c != code)]


def _other_commodities(code: str, stmt, sheet, prior_stmt, prior_sheet) -> tuple[str, ...]:
    """Return what a single-commodity statement leaves out, so the JSON and the PDF name the same omissions.

    Both footnotes are drawn from this.  Computing it twice is how the PDF once omitted a commodity the JSON
    named: a set built from the current column alone misses one the comparative column held.
    """
    return tuple(c for c in _commodities(code, stmt, sheet, prior_stmt, prior_sheet) if c != code)


@router.get("/trial-balance", response_model=schemas.TrialBalance,
            summary="Trial balance of the continuous journal through a date")
def get_trial_balance(er: EntityRequest = Depends(entity_request),
                      as_of: str | None = Query(None, description="inclusive; the latest posting when omitted")) -> dict:
    if as_of is not None:
        not_yet_happened(as_of, path="as_of")
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        tb = trial_balance(conn, er.entity_id, through=as_of)
        seq = high_water(conn, er.entity_id)
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": seq, "report": "trial-balance",
            "through": tb.through, "lines": [views.report_line(ln) for ln in tb.lines],
            "totals": micros_map(net_by_commodity(tb.lines)), "links": _links(er.entity_id, None, as_of)}


@router.get("/balance-sheet", response_model=schemas.BalanceSheet,
            summary="Balance sheet of the continuous journal as at a date")
def get_balance_sheet(er: EntityRequest = Depends(entity_request),
                      as_of: str | None = Query(None, description="inclusive; the latest posting when omitted")) -> dict:
    if as_of is not None:
        not_yet_happened(as_of, path="as_of")
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        tb = trial_balance(conn, er.entity_id, through=as_of)
        # The result carried inside equity, computed with an open start as in ``_statement_inputs``.
        accumulated = income_statement(conn, er.entity_id, start=None, end=tb.through)
        seq = high_water(conn, er.entity_id)
    sections = {kind: [views.report_line(ln) for ln in tb.of_type(kind)] for kind in BALANCE_SHEET_TYPES}
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": seq, "report": "balance-sheet",
            "through": tb.through, "sections": sections,
            "totals": micros_map(net_by_commodity(tb.of_type(*BALANCE_SHEET_TYPES))),
            "result_micros": micros_map(accumulated.net),
            "links": _links(er.entity_id, None, as_of)}


@router.get("/income-statement", response_model=schemas.IncomeStatement,
            summary="Income and expense movements of a range (openings excluded)")
def get_income_statement(er: EntityRequest = Depends(entity_request),
                         from_: str | None = Query(None, alias="from", description="inclusive"),
                         to: str | None = Query(None, description="inclusive")) -> dict:
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        start, end = scoped_range(conn, er.entity_id, start=from_, end=to)
        stmt = income_statement(conn, er.entity_id, start=start, end=end)
        seq = high_water(conn, er.entity_id)
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": seq, "report": "income-statement",
            "start": stmt.start, "end": stmt.end,
            "lines": [views.report_line(ln) for ln in stmt.lines], "net_micros": micros_map(stmt.net),
            "links": _links(er.entity_id, stmt.start, stmt.end)}


@router.get("/composition", response_model=schemas.Composition,
            summary="What a period's figures were built from: posted entries by origin and by source journal")
def get_composition(er: EntityRequest = Depends(entity_request),
                    from_: str | None = Query(None, alias="from", description="inclusive"),
                    to: str | None = Query(None, description="inclusive")) -> dict:
    """Count a period's posted entries per ``origin`` and per source journal.  Counts, not a verdict.

    A reader looking at a nearly empty income statement wants to know whether that is what the books say or only
    what has been loaded so far.  The other reports cannot tell, because an absent invoice looks the same as one
    never issued.  Here, a book loaded from its bank statement alone and a book with nothing to report look
    different, and neither is called wrong.
    """
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        start, end = scoped_range(conn, er.entity_id, start=from_, end=to)
        comp = composition(conn, er.entity_id, start=start, end=end)
        seq = high_water(conn, er.entity_id)
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": seq, "report": "composition",
            "start": comp.start, "end": comp.end,
            "origins": comp.origins, "journals": comp.journals, "entry_count": comp.entry_count,
            "links": _links(er.entity_id, comp.start, comp.end)}


@router.get("/statements", response_model=schemas.StatementsDocument,
            summary="The income statement and balance sheet of a period, arranged and signed by the service")
def get_statements(er: EntityRequest = Depends(entity_request),
                   from_: str | None = Query(None, alias="from", description="inclusive"),
                   to: str | None = Query(None, description="inclusive"),
                   comparative: bool = Query(True, description="show the period before this one beside it, "
                                                               "when these books hold one"),
                   commodity: str | None = Query(None, description="defaults to the entity's base currency"),
                   ) -> dict:
    """Return the statements the PDF is drawn from, as JSON, so a screen and an emailed document cannot disagree.

    This is the canonical shaped view from issue 0008 section B.  Sections, their order, signs, subtotals, the
    result row and whether the two sides of the sheet meet are decided once in ``haddon.statements.shape``.  The
    interface reads that arrangement instead of rebuilding it, and the PDF is drawn from the same view.
    ``difference`` and its per-column ``checks`` are part of the output, which keeps JSON, interface and PDF in
    agreement mid-period when equity carries a result nothing has posted to it.

    Every commodity the period holds comes back shaped, the requested one first.  ``other_commodities`` names the
    rest, so a reader is told what a single-commodity statement leaves out.
    """
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        code = commodity or entity["base_currency"]
        if get_commodity(conn, er.entity_id, code) is None:
            raise not_found("commodity", code)
        period, prior, stmt, sheet, result, prior_stmt, prior_sheet, prior_result = _statement_inputs(
            conn, er.entity_id, from_, to, comparative)
        seq = high_water(conn, er.entity_id)

    codes = _commodities(code, stmt, sheet, prior_stmt, prior_sheet)
    others = _other_commodities(code, stmt, sheet, prior_stmt, prior_sheet)
    statements = []
    for sense in codes:
        income, position = _statement_views(stmt, sheet, result, prior_stmt, prior_sheet, prior_result, sense)
        statements.append({"commodity": sense, "income_statement": views.shaped_income(income),
                           "balance_sheet": views.shaped_balance(position)})
    return {
        **scope_fields(entity, seq),
        "report": "statements",
        "commodity": code,
        "period": {"start": period.start, "end": period.end, "name": period.name, "heading": period.heading},
        "comparative": ({"start": prior.start, "end": prior.end, "name": prior.name} if prior else None),
        "as_of": sheet.through,
        "footnote": sign_footnote(others),
        "statements": statements,
        "other_commodities": list(others),
        "links": _links(er.entity_id, period.start, period.end),
    }


# The shareable document (chunk G5).
def _comparative_period(period: Period) -> Period:
    """Return the period to read this one against.

    The period immediately before this one, or the same span a year earlier when this one is still running.  A
    journal is continuous, so there is no book boundary to cross.
    """
    return periods.comparative(period)


def _period_bounds(conn, entity_id: str, start: str | None, end: str | None) -> tuple[str, str]:
    """Return concrete bounds for a statement, filling an open side from the journal's posted extent.

    A statement is a document with a beginning and an end, so unlike the JSON reports an open range is not left
    open.  It becomes the whole of what has been posted, the only period a statement of these books can mean
    when the caller named none.
    """
    if start is not None and end is not None:
        return start, end
    e = t.entries
    row = conn.execute(sa.select(sa.func.min(e.c.dt), sa.func.max(e.c.dt))
                       .where(e.c.entity_id == entity_id, e.c.status == "posted")).first()
    if row is None or row[0] is None:
        raise validation("these books have no posted entry to report on", path="from", reason="empty")
    return start or row[0], end or row[1]


@router.get("/statements.pdf", summary="Income statement and balance sheet of one period, as a PDF",
            response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
def get_statements_pdf(er: EntityRequest = Depends(entity_request),
                       from_: str | None = Query(None, alias="from", description="inclusive"),
                       to: str | None = Query(None, description="inclusive"),
                       comparative: bool = Query(True, description="show the period before this one beside it, "
                                                                   "when these books hold one"),
                       commodity: str | None = Query(None, description="defaults to the entity's base currency"),
                       ) -> Response:
    """Render the income statement and balance sheet as one PDF, drawn by the service rather than a browser.

    Same journal, same period and same figures as the JSON reports.  The shaping is ``haddon.statements``, the
    one place the sign convention, the grouping and the period result line are decided, so this document and any
    other reader of ``/reports`` cannot disagree about a total.

    The period names itself.  ``from``/``to`` are the only period input, and whether that range is a calendar
    year, quarter or month is worked out from the bounds (``haddon.statements.period``).  A caller who could name
    a granularity could name one its own dates contradict, and a PDF outlives the request that made it.

    A balance sheet is at a date.  It is drawn as at the period's last day and headed that way, and its
    comparative is the last day of the comparative period, never a range.  Equity carries the income and expense
    movements from the start of the journal to that date, which is what makes the two sides meet.  The sheet is
    cumulative, so a result row covering only the requested period would leave earlier profit in neither equity
    nor the result line, and any range not starting at the journal's start would print a difference.
    ``web/src/routes/Reports.tsx`` asks for the same open start.
    """
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        code = commodity or entity["base_currency"]
        unit = get_commodity(conn, er.entity_id, code)
        if unit is None:
            raise not_found("commodity", code)

        period, prior, stmt, sheet, result, prior_stmt, prior_sheet, prior_result = _statement_inputs(
            conn, er.entity_id, from_, to, comparative)
        locked = lock_through(conn)

    income, position = _statement_views(stmt, sheet, result, prior_stmt, prior_sheet, prior_result, code)
    others = _other_commodities(code, stmt, sheet, prior_stmt, prior_sheet)
    ctx = DocumentContext(
        entity_id=entity["entity_id"], legal_name=entity["legal_name"], legal_form=entity["legal_form"],
        country=entity["country"], commodity=unit, period_start=period.start, lock_through=locked,
        generated_at=drawn_words(), other_commodities=others)
    labels = ColumnLabels(period.name, prior.name if prior_stmt is not None else None, period.heading)

    body = to_pdf(document_html(ctx, income, position, labels))
    name = f"{er.entity_id}-{period.name.replace(' ', '-').lower()}-statements.pdf"
    return Response(body, media_type="application/pdf",
                    headers={"Content-Disposition": content_disposition(name, fallback="statements.pdf"),
                             "Cache-Control": "no-store"})
