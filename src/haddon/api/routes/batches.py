"""``POST /batches``: N entries posted as one unit, applied whole or refused whole.

Every other entry write is one entry at a time.  That suits a reviewer booking what they are looking at, but not a
conversion.  An agent that has turned a journal, spreadsheet or export into a thousand entries and posts them in
a loop of single calls cannot show afterwards that it posted each of them exactly once.  A dropped entry, a retry
that landed twice, or a connection lost two thirds of the way through leaves an append-only ledger that is
internally consistent and simply wrong.

So the batch takes the shape ``POST /statements`` has had since chunk F1.  The ledger never sees what the entries
were converted from, so it cannot work out what they should add up to.  The caller declares it
(``asserted.count`` and ``asserted.totals``) and the ledger refuses a submission that does not match.  No parser,
source format or per-company logic reaches this module.

What it deliberately does not do is recorded in ``docs/design/decisions.md``.  It writes no ``journal_mappings``
row (that is the hledger importer's full-fidelity record, read by the oracle's ``mapping_sha256`` check), and it
reimplements no part of posting.  Every entry is written by ``commands.post_entry``, so the balance rule, the
account, commodity and precision rules and the period lock behave exactly as they do for a single entry.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from haddon.api import idempotency, schemas
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.registry import entity_context
from haddon.api.wire import entity_base, parse_body, scope_fields
from haddon.repository.events import high_water

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["entries"], responses=schemas.ERRORS)


@router.post("/batches", status_code=201, operation_id="post_batch",
             responses={201: {"model": schemas.BatchResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.BatchSubmission.model_json_schema()}}}},
             summary="Post many entries as one unit, checked against the count and totals the caller asserts")
async def post_batch(request: Request, er: EntityRequest = Depends(entity_request)):
    """Post a converted set of entries, and learn before anything commits whether it is the set you sent.

    The caller asserts how many entries the batch contains and what each commodity's debits and credits come to
    in micros.  Both sides are asked for even though a balanced batch makes them equal.  The caller computes them
    from its own source, so a figure transposed on one side disagrees with the other instead of cancelling out.

    A refusal is whole and leaves nothing behind, whether the cause is a broken assertion, an unknown account in
    the nine-hundredth entry or a date in a closed period.  ``422 unbalanced`` names every commodity whose asserted
    totals disagree with what was posted, with both numbers.  ``422 validation`` covers the count and the maximum
    batch size (published as ``limits.max_batch_entries`` in ``/capabilities``).  A refusal about one entry names
    that entry: ``batch_index``, its date and narration, its ``entry_id`` when the caller supplied one, and a
    ``path`` such as ``entries[900].legs[0].account``.  A retry under the same ``Idempotency-Key`` replays the
    original reply instead of posting the batch twice.

    The whole batch is recorded as one ``operations`` row (intent: the assertion; result: the entries written),
    alongside the ordinary audit action each entry keeps.
    """
    raw = await request.body()

    def work(ctx):
        from haddon import commands

        body = parse_body(raw, schemas.BatchSubmission)
        batch = commands.post_batch(
            ctx, reason=body.reason, asserted_count=body.asserted.count,
            asserted_totals=[commands.BatchTotalSpec(t.commodity, t.debits_micros, t.credits_micros)
                             for t in body.asserted.totals],
            entries=[commands.BatchEntrySpec(
                dt=e.dt, narration=e.narration,
                legs=[commands.LegSpec(lg.account, lg.commodity, lg.amount, lg.memo, lg.tax_code)
                      for lg in e.legs],
                reason=e.reason, origin=e.origin, code=e.code, comment=e.comment,
                tags=tuple(tuple(tag) for tag in (e.tags or ())), id=e.id) for e in body.entries])
        base = entity_base(er.entity_id)
        payload = {**scope_fields(entity_context(ctx.conn, er.entity_id), high_water(ctx.conn, er.entity_id)),
                   "operation_id": batch.operation_id, "count": len(batch.entries),
                   "entry_ids": batch.entry_ids, "totals": commands.totals_json(batch.totals),
                   "audit_seq": batch.audit.seq,
                   "links": {"entries": f"{base}/entries", "events": f"{base}/events"}}
        return 201, payload

    return idempotency.run(er, raw, work)
