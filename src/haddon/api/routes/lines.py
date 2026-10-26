"""Statement lines: what a line is, what it explains, and what has become of it.

Two reads and four writes, deliberately no more.  The writes are the four things that can be said about a line,
in the words somebody reviewing a month would use:

* ``POST /lines/{id}/book``: post the entry this line is, and hold the line against it.
* ``POST /lines/{id}/link``: the entry already exists; say which posting(s) this line is, and for how much.
  Several parts is a split.  This posts nothing.
* ``POST /lines/{id}/ignore``: this line will not be booked, and here is why.
* ``POST /lines/{id}/reexplain``: supersede what was said before, correcting or reversing the entry the old
  explanation booked, in the same transaction.

A line is addressed by its own id, not under its statement, because it is reached from either end: from the
statement it was read off and from the posting it explains.  Every response is the line's whole state
afterwards, not a bare ok, so an agent can work through a statement without re-reading it.

All four write bodies are read as raw bytes and checked for wire numbers before the schema runs, so a JSON
number in an amount gets the amount contract's own 422 rather than a pydantic type error, as for
``/statements``.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from haddon import reconciliation_commands as rc
from haddon.api import idempotency, schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.pagination import LINES, page_limit, paginate
from haddon.api.registry import entity_context
from haddon.api.wire import entity_base, link, parse_body, scope_fields
from haddon.commands import LegSpec
from haddon.domain.dates import parse_date
from haddon.domain.errors import validation
from haddon.repository import corrections as corrections_repo
from haddon.repository import lines as lines_repo
from haddon.repository import tables as t
from haddon.repository.events import high_water

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["lines"], responses=schemas.ERRORS)

#: The statuses ``GET /lines?state=`` accepts: exactly the ones ``line_state`` can produce.
LINE_STATES = (lines_repo.UNEXPLAINED, lines_repo.PARTLY, lines_repo.BOOKED_STATUS, lines_repo.LINKED_STATUS,
               lines_repo.IGNORED_STATUS, lines_repo.SUPERSEDED_INPUT)


def _entry_ids(conn, entity_id: str, state: lines_repo.LineState) -> list[str]:
    """Return every entry this line's history touches, oldest first and without repeats.

    If the entry the line booked was later corrected directly, the chain of standing replacements is included,
    so a reader sees what the books hold for the line now and does not offer to book its money a second time.
    Mirrors are left out; a reversal is read from the entry's ``reversed_by``.
    """
    out: list[str] = []
    for explanation in state.history:
        for candidate in [explanation.entry_id, *[a.entry_id for a in explanation.allocations]]:
            if candidate is not None and candidate not in out:
                out.append(candidate)
    live = state.live
    if live is not None and live.entry_id is not None:
        current = live.entry_id
        while (correction := corrections_repo.correction_for(conn, entity_id, current)) is not None \
                and correction.replacement_id is not None and correction.replacement_id not in out:
            current = correction.replacement_id
            out.append(current)
    return out


def line_view(state: lines_repo.LineState, entity_id: str,
              entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    base = entity_base(entity_id)
    return {**state.as_json(), "entries": entries or [],
            "links": {"self": f"{base}/lines/{state.line_id}",
                      "statement": f"{base}/statements/{state.statement_id}",
                      "provenance": f"{base}/provenance?line_id={state.line_id}",
                      "account": link(entity_id, f"/accounts/{state.account_id}")}}


def _body(conn, entity_id: str, line_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
    state = lines_repo.line_state(conn, entity_id, line_id)
    entries = views.entries_for(conn, entity_id, _entry_ids(conn, entity_id, state), views.account_paths(conn, entity_id))
    return {**scope_fields(entity_context(conn, entity_id), high_water(conn, entity_id)),
            "line": line_view(state, entity_id, entries), "result": result}


@router.get("/lines", response_model=schemas.LinePage,
            summary="Statement lines across every statement, by date: what each is and what it was booked to")
def list_lines(er: EntityRequest = Depends(entity_request),
               from_: str | None = Query(None, alias="from", description="inclusive line date (YYYY-MM-DD)"),
               to: str | None = Query(None, description="inclusive line date (YYYY-MM-DD)"),
               account_id: str | None = Query(None),
               statement_id: str | None = Query(None),
               state: list[str] = Query(default_factory=list,
                                        description="keep only these line statuses; repeat for several"),
               limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    """List lines across every statement over a date range, with what became of each and its entries.

    ``GET /statements/{id}/lines`` reads one document in printed order.  This reads by time: every line of every
    statement in the range, ordered by date and then document order, the way somebody reviewing a period reads
    it.

    ``state`` is applied after the page is read, because a line's status is not a column.  It is derived from
    the chain of explanations written for the line, their allocations, the entries behind them and whether the
    line's statement has been replaced.  Doing that in SQL would be a second implementation of
    ``lines_repo.line_state`` that could disagree with the first, which is worse than a short page.

    So a page here can hold fewer items than ``limit``, or none, while ``next_cursor`` is still set.
    ``filtered_after_page`` lists the filters applied after selection; while it is non-empty, a short page is
    not the end of the results.  Read until ``next_cursor`` is null.
    """
    for value, path in ((from_, "from"), (to, "to")):
        if value is not None:
            parse_date(value, path=path)
    unknown = [s for s in state if s not in LINE_STATES]
    if unknown:
        raise validation(f"unknown line status {unknown[0]!r}", path="state", reason="unknown_state",
                         value=unknown[0], expected=list(LINE_STATES))
    where = []
    if from_ is not None:
        where.append(t.statement_lines.c.dt >= from_)
    if to is not None:
        where.append(t.statement_lines.c.dt <= to)
    if statement_id is not None:
        where.append(t.statement_lines.c.statement_id == statement_id)
    if account_id is not None:
        where.append(t.statement_lines.c.statement_id.in_(
            sa.select(t.statements.c.id)
            .where(t.statements.c.entity_id == er.entity_id, t.statements.c.account_id == account_id)))
    # ``state`` binds into the cursor like every other filter, even though it is applied after the query.  A
    # cursor is a position in one listing; replayed under a different ``state`` it would return a page that
    # belongs to neither listing.  Sorted, because the order the query string repeats them in does not change
    # which lines are kept, and a cursor should not be refused over that.
    filters = {"from": from_, "to": to, "account_id": account_id, "statement_id": statement_id,
               "state": sorted(state)}
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    base = entity_base(er.entity_id)
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        page = paginate(conn, er.entity_id, spec=LINES, where=where, filters=filters, limit=size, cursor=cursor,
                        keys=er.cursor_keys, resync=f"{base}/lines", render=lambda r: r)
        states = [lines_repo.line_state(conn, er.entity_id, row.id) for row in page.items]
        if state:
            states = [st for st in states if st.status in state]
        paths = views.account_paths(conn, er.entity_id)
        ids = {st.line_id: _entry_ids(conn, er.entity_id, st) for st in states}
        held = views.entries_by_id(conn, er.entity_id, [i for st in states for i in ids[st.line_id]], paths)
        items = [line_view(st, er.entity_id, [held[i] for i in ids[st.line_id] if i in held]) for st in states]
    return page.with_items(items).body(
        entity_id=entity["entity_id"], legal_name=entity["legal_name"], base_currency=entity["base_currency"],
        filtered_after_page=["state"] if state else [],
        links={"statements": f"{base}/statements", "accounts": f"{base}/accounts"})


@router.get("/lines/{line_id}", response_model=schemas.LineResponse,
            summary="One statement line: its live explanation, its whole history and what of it is unexplained")
def get_line(line_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    with er.reader() as conn:
        return _body(conn, er.entity_id, line_id, None)


def _legs(items) -> list[LegSpec]:
    return [LegSpec(lg.account, lg.commodity, lg.amount, lg.memo, lg.tax_code) for lg in items]


def _parts(items) -> list[rc.PartSpec]:
    return [rc.PartSpec(p.posting_id, p.amount) for p in items]


@router.post("/lines/{line_id}/book", status_code=201, operation_id="book_line",
             responses={201: {"model": schemas.LineResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.LineBookSubmission.model_json_schema()}}}},
             summary="Post the entry this line is, and hold the line against it")
async def book(line_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.LineBookSubmission)
        result = rc.book_line(ctx, line_id=line_id, legs=_legs(body.legs), narration=body.narration,
                              reason=body.reason, dt=body.dt, memo=body.memo, tax_code=body.tax_code,
                              code=body.code, comment=body.comment, entry_id=body.id, origin=body.origin)
        return 201, _body(ctx.conn, er.entity_id, line_id, result.as_json())

    return idempotency.run(er, raw, work)


@router.post("/lines/{line_id}/link", status_code=201, operation_id="link_line",
             responses={201: {"model": schemas.LineResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.LineLinkSubmission.model_json_schema()}}}},
             summary="Say which existing posting(s) this line is, and for how much; posts nothing")
async def link_(line_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.LineLinkSubmission)
        result = rc.link_line(ctx, line_id=line_id, parts=_parts(body.parts), reason=body.reason,
                              explanation_id=body.id)
        return 201, _body(ctx.conn, er.entity_id, line_id, result.as_json())

    return idempotency.run(er, raw, work)


@router.post("/lines/{line_id}/ignore", status_code=201, operation_id="ignore_line",
             responses={201: {"model": schemas.LineResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.LineIgnoreSubmission.model_json_schema()}}}},
             summary="Record that this line will not be booked, and why")
async def ignore(line_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.LineIgnoreSubmission)
        result = rc.ignore_line(ctx, line_id=line_id, reason=body.reason, explanation_id=body.id)
        return 201, _body(ctx.conn, er.entity_id, line_id, result.as_json())

    return idempotency.run(er, raw, work)


@router.post("/lines/{line_id}/reexplain", status_code=201, operation_id="reexplain_line",
             responses={201: {"model": schemas.LineResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.LineReexplainSubmission.model_json_schema()}}}},
             summary="Supersede what was said about this line, correcting or reversing what it booked")
async def reexplain(line_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.LineReexplainSubmission)
        result = rc.reexplain_line(
            ctx, line_id=line_id, kind=body.kind, reason=body.reason,
            legs=None if body.legs is None else _legs(body.legs),
            parts=None if body.parts is None else _parts(body.parts),
            narration=body.narration, dt=body.dt, memo=body.memo, tax_code=body.tax_code, code=body.code,
            comment=body.comment, explanation_id=body.id)
        return 201, _body(ctx.conn, er.entity_id, line_id, result.as_json())

    return idempotency.run(er, raw, work)
