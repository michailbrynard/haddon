"""Recoverable tax that was not recovered: the record, its two endings, and the question it exists to answer.

A recoverable tax is sometimes deliberately not claimed, for a documented reason.  Without this record only the
arithmetic survives (a larger number in an expense or an asset), so the books cannot say what tax is not being
reclaimed and why, and an amount sitting inside a fixed asset has nothing explaining it.

* ``POST /entries/{id}/unrecovered-tax`` records the amount, the commodity and the reason, and optionally which
  leg it is sitting inside, which tax it is, and the last date it could still be recovered.
* ``POST /unrecovered-tax/{id}/resolve`` closes it: ``recovered``, naming the entry that recovered it, or
  ``forgone``, whose reason has to do the work on its own.
* ``GET /unrecovered-tax`` is the query, with a total per commodity.

Nothing here knows a country or a tax.  The deadline is a date the caller supplies, not a rule the ledger
applies, because only the caller knows the regime.  ``tax_code`` is the entity's own code, the same column name
the chart and the postings already use.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from haddon.api import idempotency, schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.pagination import UNRECOVERED_TAX, page_limit, paginate
from haddon.api.registry import entity_context, require_row
from haddon.api.wire import entity_base, parse_body, scope_fields
from haddon.domain.dates import parse_date
from haddon.repository import tables as t
from haddon.repository import unrecovered as ur
from haddon.repository.events import high_water

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["unrecovered tax"], responses=schemas.ERRORS)


def _links(entity_id: str, record: ur.Unrecovered) -> dict[str, str]:
    base = entity_base(entity_id)
    links = {"entry": f"{base}/entries/{record.entry_id}", "unrecovered_tax": f"{base}/unrecovered-tax",
             "resolve": f"{base}/unrecovered-tax/{record.id}/resolve"}
    if record.recovered_entry_id is not None:
        links["recovered_by"] = f"{base}/entries/{record.recovered_entry_id}"
    return links


def _response(conn, entity_id: str, record: ur.Unrecovered) -> dict:
    entity = entity_context(conn, entity_id)
    return {**scope_fields(entity, high_water(conn, entity_id)),
            "record": record.as_json(),
            "history": [r.as_json() for r in ur.history_of(conn, entity_id, record.entry_id)],
            "links": _links(entity_id, record)}


@router.post("/entries/{entry_id}/unrecovered-tax", status_code=201, operation_id="record_unrecovered_tax",
             responses={201: {"model": schemas.UnrecoveredResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.UnrecoveredSubmission.model_json_schema()}}}},
             summary="Record recoverable tax that was not recovered on this entry, and why")
async def record(entry_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """Record the decision to book a cost gross of a recoverable tax, and the reason for it.

    Such a decision has a reason: the document does not support a claim, or there is no document.  The gross
    figure records only the consequence, which is how an amount ends up inside a fixed asset with nobody able to
    say why.  Nothing is posted here; the arithmetic already happened.
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.UnrecoveredSubmission)
        written = ur.record_unrecovered(ctx, entry_id=entry_id, commodity=body.commodity,
                                        amount_micros=body.amount_micros, reason=body.reason,
                                        posting_id=body.posting_id, tax_code=body.tax_code,
                                        recoverable_until=body.recoverable_until)
        return 201, _response(ctx.conn, er.entity_id, written)

    return idempotency.run(er, raw, work)


@router.post("/unrecovered-tax/{record_id}/resolve", status_code=201, operation_id="resolve_unrecovered_tax",
             responses={201: {"model": schemas.UnrecoveredResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.UnrecoveredResolution.model_json_schema()}}}},
             summary="Close it: recovered after all, naming the entry that did it, or deliberately forgone")
async def resolve(record_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """Close a record as ``recovered`` (naming the recovering entry) or ``forgone`` (the reason stands alone).

    Without ``forgone`` the list would only grow.  When a claim window closes, or an amount is not worth the
    paperwork, the record would otherwise go on saying money is recoverable when it is not.
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.UnrecoveredResolution)
        written = ur.resolve(ctx, record_id=record_id, outcome=body.outcome, reason=body.reason,
                             recovered_entry_id=body.recovered_entry_id)
        return 201, _response(ctx.conn, er.entity_id, written)

    return idempotency.run(er, raw, work)


@router.get("/unrecovered-tax", response_model=schemas.UnrecoveredPage,
            summary="What tax are we not reclaiming, and why: with a total per commodity")
def list_unrecovered(er: EntityRequest = Depends(entity_request),
                     state: str = Query("unrecovered", description="unrecovered | recovered | forgone"),
                     from_: str | None = Query(None, alias="from", description="inclusive, on the entry date"),
                     to: str | None = Query(None, description="inclusive, on the entry date"),
                     limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    """List unrecovered-tax records in one state, with a total per commodity.  Defaults to ``unrecovered``.

    ``totals`` is per commodity and never summed across commodities: two currencies are two answers.  The
    ``from``/``to`` filters apply to the entry date.
    """
    if state not in (ur.UNRECOVERED, ur.RECOVERED, ur.FORGONE):
        from haddon.domain.errors import validation

        raise validation("state must be unrecovered, recovered or forgone", path="state", reason="enum",
                         value=state)
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    where = [ur.state_clause(state)]
    entry_filters = []
    if from_ is not None:
        entry_filters.append(t.entries.c.dt >= parse_date(from_, path="from"))
    if to is not None:
        entry_filters.append(t.entries.c.dt <= parse_date(to, path="to"))
    if entry_filters:
        where.append(sa.exists().where(t.entries.c.entity_id == er.entity_id,
                                       t.entries.c.id == t.unrecovered_tax.c.entry_id, *entry_filters))
    filters = {"state": state, "from": from_, "to": to}
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        page = paginate(conn, er.entity_id, spec=UNRECOVERED_TAX, where=where, filters=filters, limit=size,
                        cursor=cursor, keys=er.cursor_keys,
                        resync=f"{entity_base(er.entity_id)}/unrecovered-tax", render=lambda r: r)
        paths = views.account_paths(conn, er.entity_id)
        entries = views.entries_by_id(conn, er.entity_id, [r.entry_id for r in page.items], paths)
        items = []
        for row in page.items:
            shaped = entries.get(row.entry_id)
            if shaped is None:   # the foreign key says an entry is always there
                continue
            record = ur.of_row(row)
            items.append({"record": record.as_json(),
                          "entry": {k: v for k, v in shaped.items() if k != "reversed_by"},
                          "links": _links(er.entity_id, record)})
        # Total everything matching the filters, not just this page: a total that changed as you paged
        # would be worse than none.
        summed = ur.totals(conn, er.entity_id, where=where)
    return page.with_items(items).body(entity_id=entity["entity_id"], legal_name=entity["legal_name"],
                                       base_currency=entity["base_currency"], state=state, totals=summed)


@router.get("/unrecovered-tax/{record_id}", response_model=schemas.UnrecoveredResponse,
            summary="One record and the whole history of the entry it is about")
def get_record(record_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    with er.reader() as conn:
        record = require_row(ur.get_record(conn, er.entity_id, record_id), "unrecovered_tax", record_id)
        return _response(conn, er.entity_id, record)
