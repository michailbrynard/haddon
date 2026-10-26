"""Bookings awaiting a document, and the question they answer: what in these books is provisional?

Booking something provisionally while an invoice is still to come is ordinary, but the store had no way to hold
that state.  The list of open items then lives in somebody's notes, and only as long as somebody remembers it.
When nobody does, a line whose gross cost should split into a smaller net cost plus recoverable tax once the
invoice turns up stays at the gross figure.

Two writes and a read:

* ``POST /entries/{id}/awaiting-document`` records that this booking is provisional and names what would settle
  it.  Called again for the same entry it restates the expectation instead of opening a second one.
* ``POST /entries/{id}/awaiting-document/withdraw`` records the other ending: no document is coming.
* ``GET /provisional`` lists every booking still awaiting a document, with the entry beside it.

There is no "mark it resolved".  Filing the awaited document against the entry (``POST /entries/{id}/documents``)
settles the expectation and names the evidence link that did it.  A separate tick box would be one more thing to
remember, and forgetting is the failure this exists to prevent.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from haddon.api import idempotency, schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.pagination import DOCUMENT_EXPECTATIONS, page_limit, paginate
from haddon.api.registry import entity_context
from haddon.api.wire import entity_base, parse_body, scope_fields
from haddon.domain.dates import parse_date
from haddon.repository import expectations as ex
from haddon.repository import tables as t
from haddon.repository.events import high_water

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["provisional"], responses=schemas.ERRORS)


def _links(entity_id: str, entry_id: str) -> dict[str, str]:
    base = entity_base(entity_id)
    return {"entry": f"{base}/entries/{entry_id}", "provisional": f"{base}/provisional",
            "documents": f"{base}/entries/{entry_id}/documents",
            "provenance": f"{base}/provenance?entry_id={entry_id}"}


def _response(ctx_conn, entity_id: str, record) -> dict:
    entity = entity_context(ctx_conn, entity_id)
    history = [e.as_json() for e in ex.history_of_entry(ctx_conn, entity_id, record.entry_id)]
    return {**scope_fields(entity, high_water(ctx_conn, entity_id)),
            "expectation": record.as_json(), "history": history,
            "links": _links(entity_id, record.entry_id)}


@router.post("/entries/{entry_id}/awaiting-document", status_code=201, operation_id="await_document",
             responses={201: {"model": schemas.ExpectationResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.ExpectationSubmission.model_json_schema()}}}},
             summary="Record that this booking is provisional, naming the document that would settle it")
async def expect_document(entry_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """Mark a posted booking as resting on an assumption that an awaited document may overturn.

    The booking stands.  This is not a draft and not a doubt about the amount: the entry stays posted and counts
    in every report, because a cost belongs in the month it happened, at the best figure available.  Calling this
    again for the same entry restates the expectation.  Filing the awaited document against the entry
    (``POST /entries/{id}/documents``) settles it.
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.ExpectationSubmission)
        record = ex.expect_document(ctx, entry_id=entry_id, awaiting=body.awaiting, reason=body.reason)
        return 201, _response(ctx.conn, er.entity_id, record)

    return idempotency.run(er, raw, work)


@router.post("/entries/{entry_id}/awaiting-document/withdraw", status_code=201, operation_id="withdraw_expectation",
             responses={201: {"model": schemas.ExpectationResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.WithdrawalSubmission.model_json_schema()}}}},
             summary="No document is coming: the booking stands as it is, and the reason says why")
async def withdraw(entry_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """Close an expectation because no document is coming; the booking stands as it is.

    This is kept separate from a document arriving.  A simplified receipt the supplier will never reissue settles
    the question, but not the way an invoice would, and the record keeps the two apart so the reason each item
    left the provisional list stays true.
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.WithdrawalSubmission)
        record = ex.withdraw_expectation(ctx, entry_id=entry_id, reason=body.reason)
        return 201, _response(ctx.conn, er.entity_id, record)

    return idempotency.run(er, raw, work)


@router.get("/provisional", response_model=schemas.ProvisionalPage,
            summary="What in these books is provisional: every booking still awaiting a document")
def list_provisional(er: EntityRequest = Depends(entity_request),
                     from_: str | None = Query(None, alias="from", description="inclusive, on the entry date"),
                     to: str | None = Query(None, description="inclusive, on the entry date"),
                     limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    """List every booking still awaiting a document, with its entry.

    Ordered by when the expectation was recorded, not by entry date, because the list is worked through as open
    items.  The ``from``/``to`` filters apply to the entry date, so a month's page can ask what is provisional in
    that month while everything older and still open remains reachable.
    """
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    where = [ex.awaiting_clause()]
    entry_filters = []
    if from_ is not None:
        entry_filters.append(t.entries.c.dt >= parse_date(from_, path="from"))
    if to is not None:
        entry_filters.append(t.entries.c.dt <= parse_date(to, path="to"))
    if entry_filters:
        where.append(sa.exists().where(t.entries.c.entity_id == er.entity_id,
                                       t.entries.c.id == t.document_expectations.c.entry_id, *entry_filters))
    filters = {"from": from_, "to": to}
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        page = paginate(conn, er.entity_id, spec=DOCUMENT_EXPECTATIONS, where=where, filters=filters,
                        limit=size, cursor=cursor, keys=er.cursor_keys,
                        resync=f"{entity_base(er.entity_id)}/provisional", render=lambda r: r)
        paths = views.account_paths(conn, er.entity_id)
        entries = views.entries_by_id(conn, er.entity_id, [r.entry_id for r in page.items], paths)
        items = []
        for row in page.items:
            shaped = entries.get(row.entry_id)
            if shaped is None:   # an expectation always has an entry: the foreign key says so
                continue
            items.append({"expectation": ex.of_row(row).as_json(),
                          "entry": {k: v for k, v in shaped.items() if k != "reversed_by"},
                          "links": _links(er.entity_id, row.entry_id)})
    return page.with_items(items).body(entity_id=entity["entity_id"], legal_name=entity["legal_name"],
                                       base_currency=entity["base_currency"])
