"""Closing a period, reopening one, and finding out first whether either would be allowed.

``GET /closes/{id}`` reads one close or reopen back, with a link to the snapshot it was recorded against.  A close
returns its snapshot only once, in the 201, so without this route the only way back to the state a past close was
taken against is to walk ``/events`` (issue 0005, acceptance criterion 6).  The lock carries the id of the close
that set it, so from a closed period it is one hop.

``GET /close-checks`` is the read-only face of ``haddon.repository.close_checks``.  An agent or a person can find
out what a close would refuse before attempting one, and gets back, per check, a status, a human-readable reason
and the specific outstanding items, with account, month, commodity, ids and amounts as integer micro strings.

``POST /close`` and ``POST /reopen`` are deliberately small.  A close is a date, a reason, the lock version the
caller expects and, only where a check is blocking, an override naming that check and saying why.  The caller
never supplies a snapshot or state hash.  The verification snapshot is computed server-side in the same
transaction as the lock move and the audit action, because a caller who could hand in a state hash could hand in
a hash of a state that never existed (issue 0005, pitfall 2).

``eligible`` and ``may_close`` answer different questions and are deliberately separate.  ``eligible`` is whether
the month is genuinely complete; ``may_close`` is whether a close would be allowed right now, which an override
can make true while ``eligible`` stays false.  An override never rewrites a check's ``status``, so the finding
stays visible next to the waiver that let the close through.

The read route uses a reader connection and writes nothing.  The two writes go through the ordinary idempotency
boundary, so retrying a close after a timeout replays the first result instead of attempting a second close.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from haddon.api import idempotency, schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.registry import entity_context, require_row
from haddon.api.wire import entity_base, parse_body, scope_fields
from haddon.domain.dates import parse_date
from haddon.repository import tables as t
from haddon.repository.close_checks import Override, close_checks
from haddon.repository.closing import get_lock
from haddon.repository.events import high_water

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["close"], responses=schemas.ERRORS)


def _overrides(submitted) -> list[Override]:
    return [Override(check=o.check, reason=o.reason) for o in submitted]


@router.get("/close-checks", response_model=schemas.CloseChecks,
            summary="Close eligibility through a date: per check, a status, a reason and the outstanding items")
def get_close_checks(er: EntityRequest = Depends(entity_request),
                     through: str = Query(..., description="the close date this would close through (YYYY-MM-DD)"),
                     override: list[str] = Query(default_factory=list,
                                                 description="check keys to preview an override of; the reason "
                                                             "belongs on the close itself, not on a preview")) -> dict:
    parse_date(through, path="through")
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        checks = close_checks(conn, er.entity_id, through=through,
                              overrides=[Override(check=g, reason="preview") for g in override])
        lock = get_lock(conn, er.entity_id)
        seq = high_water(conn, er.entity_id)
    base = entity_base(er.entity_id)
    return {**checks.as_json(), "entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": seq,
            # The lock version a close must send back, here so that an agent asking "would this close?" does not
            # have to fetch the entity listing for it (issue 0006).
            "lock": {"through": lock.through, "version": lock.version, "close_id": lock.close_id},
            "links": {"events": f"{base}/events", "close": f"{base}/close", "reopen": f"{base}/reopen",
                      "trial_balance": f"{base}/reports/trial-balance?as_of={through}"}}


@router.get("/closes/{close_id}", response_model=schemas.CloseRecord,
            summary="One close or reopen, read back, with a link to the snapshot it was recorded against")
def get_close_record(close_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    """Read one close or reopen record, linking to the immutable snapshot behind it (issue 0005, criterion 6).

    A close returns its snapshot link only once, in the 201.  This route is the way back to it afterwards.  The
    lock carries the id of the close that set it (``lock_close_id`` on the entity summary), so from any closed
    period this is one hop.

    Read-only, and shaped like ``/snapshots/{id}`` and ``/exports/{id}``.  A close record is an immutable row,
    never updated or deleted, so it is fetched by id and there is no listing here.  The history of closes belongs
    to the reviewer loop (issue 0007).
    """
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        row = require_row(conn.execute(sa.select(t.close_records).where(
            t.close_records.c.entity_id == er.entity_id, t.close_records.c.id == close_id)).first(),
            "close_record", close_id)
        seq = high_water(conn, er.entity_id)
    return {**views.close_record(row, er.entity_id), "entity_id": entity["entity_id"],
            "legal_name": entity["legal_name"], "base_currency": entity["base_currency"], "snapshot_seq": seq}


@router.post("/close", status_code=201, operation_id="close_period",
             responses={201: {"model": schemas.ClosePeriodResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.ClosePeriodSubmission.model_json_schema()}}}},
             summary="Close the books through a date: check, snapshot, lock and audit, in one transaction")
async def close_period(request: Request, er: EntityRequest = Depends(entity_request)):
    """Close a period on the acting principal's own authority, with the checks applied.

    Everything happens in one transaction.  The checks are evaluated, and a blocking one refuses unless an
    override names it.  The verification snapshot is computed and stored, the lock advances, and one audit action
    records what was evaluated, what was waived and the snapshot's hash.  A refusal leaves nothing behind: no
    snapshot, no partial record, and the idempotency key is not used up.

    There are two distinct refusals.  ``409 close_not_eligible`` names every blocking check and what is
    outstanding in it.  A ``422`` says an override named a check that is not blocking at all.
    """
    raw = await request.body()

    def work(ctx):
        from haddon import commands

        body = parse_body(raw, schemas.ClosePeriodSubmission)
        closed = commands.close_period(ctx, through=body.through, reason=body.reason,
                                       expected_version=body.expected_version,
                                       overrides=_overrides(body.overrides))
        base = entity_base(er.entity_id)
        payload = {**scope_fields(entity_context(ctx.conn, er.entity_id), high_water(ctx.conn, er.entity_id)),
                   **closed.as_json(), "audit_seq": closed.audit.seq,
                   "links": {"self": f"{base}/closes/{closed.close_id}",
                             "snapshot": f"{base}/snapshots/{closed.snapshot.id}",
                             "events": f"{base}/events", "reopen": f"{base}/reopen",
                             "close_checks": f"{base}/close-checks?through={body.through}"}}
        payload.pop("audit", None)
        return 201, payload

    return idempotency.run(er, raw, work)


@router.post("/reopen", status_code=201, operation_id="reopen_period",
             responses={201: {"model": schemas.ReopenPeriodResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.ReopenPeriodSubmission.model_json_schema()}}}},
             summary="Move the period lock deliberately backwards, and record why")
async def reopen_period(request: Request, er: EntityRequest = Depends(entity_request)):
    """Open a closed period again.

    Reopening is how an incomplete month gets fixed, so it does not depend on the checks that refuse a close
    (issue 0005, pitfall 1).  The month that most needs reopening is exactly the one that cannot pass them.
    Nothing is undone: the close record and its snapshot stay as they are, and this appends a second record
    saying the lock moved back.
    """
    raw = await request.body()

    def work(ctx):
        from haddon import commands

        body = parse_body(raw, schemas.ReopenPeriodSubmission)
        reopened = commands.reopen_period(ctx, reason=body.reason, expected_version=body.expected_version,
                                          through=body.through, close_id=body.close_id)
        base = entity_base(er.entity_id)
        payload = {**scope_fields(entity_context(ctx.conn, er.entity_id), high_water(ctx.conn, er.entity_id)),
                   **reopened.as_json(), "audit_seq": reopened.audit.seq,
                   "links": {"self": f"{base}/closes/{reopened.close_id}",
                             "snapshot": f"{base}/snapshots/{reopened.snapshot.id}",
                             "events": f"{base}/events", "close": f"{base}/close"}}
        payload.pop("audit", None)
        return 201, payload

    return idempotency.run(er, raw, work)
