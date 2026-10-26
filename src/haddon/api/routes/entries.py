"""Booked entries: the list, one entry, one entry's history, and the writes that post, reverse or correct one.

``POST /entries`` is a journal entry somebody wrote on purpose: an accrual, a reclassification, an adjustment.
It needs no statement line behind it.  An invoice raised or a payroll accrued is an economic event on a date
with no bank row on that date, and without this write the books could record cash and nothing else.
``POST /entries/{id}/reverse`` and ``POST /entries/{id}/correct`` are the only ways to undo or restate a posted
entry; it is never edited.

``/entries`` is a list, not a balance, so a date range is an optional filter: listing two years' entries adds
nothing up.  Everything that produces a total, such as the register and the reports, takes the same inclusive
``from``/``to`` range, because a journal is continuous (issue 0002).

``/entries/{id}/events`` returns the append-only log for that entry, for the correction rows that reference it
and for the statement line that booked it.  A correction is its own subject in the log, so an entry whose events
stopped at its own id would show a silently reversed posting as if nothing had happened.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response

from haddon.api import idempotency, schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.pagination import ENTRIES, page_limit, paginate
from haddon.api.registry import entity_context, require_row
from haddon.api.wire import entity_base, parse_body, scope_fields
from haddon.domain.dates import parse_date
from haddon.repository import tables as t
# Aliased: this module defines a route called ``get_entry``, which would shadow the import.
from haddon.repository.entries import get_entry as read_entry
from haddon.repository.events import high_water

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["entries"], responses=schemas.ERRORS)


@router.post("/entries", status_code=201, operation_id="post_entry",
             responses={201: {"model": schemas.JournalEntryResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.JournalEntrySubmission.model_json_schema()}}}},
             summary="Write a balanced journal entry: an accrual, a reclassification, an adjustment")
async def create_entry(request: Request, er: EntityRequest = Depends(entity_request)):
    """Post one balanced journal entry that no bank statement line stands behind.

    An invoice raised, a payroll accrued, a quarter's withholding moved from one liability to the next: each is
    an economic event on a date with no cash row behind it on that date.  The other entry writes either hang an
    entry off a statement line or post a converted set as one unit (``POST /batches``).  This one is for a single
    entry, and without it a period's profit could only ever be its cash flow.

    The entry takes the ordinary posting path (drafted and sealed in one transaction), so it meets the same
    balance rule, the same account, currency and precision rules and the same period lock as a line classified
    off a statement.  It takes any number of legs from two upward, as a four-leg payroll accrual needs.  It
    comes back posted and is never edited: ``POST /entries/{id}/correct`` restates it and
    ``POST /entries/{id}/reverse`` withdraws it.
    """
    raw = await request.body()

    def work(ctx):
        from haddon import commands

        body = parse_body(raw, schemas.JournalEntrySubmission)
        written = commands.post_entry(
            ctx, dt=body.dt, narration=body.narration, reason=body.reason,
            legs=[commands.LegSpec(lg.account, lg.commodity, lg.amount, lg.memo, lg.tax_code)
                  for lg in body.legs],
            origin=commands.DEFAULT_ORIGIN if body.origin is None else body.origin,
            code=body.code or "", comment=body.comment or "", tags=body.tags or (), id=body.id)
        paths = views.account_paths(ctx.conn, er.entity_id)
        shaped = views.entries_by_id(ctx.conn, er.entity_id, [written.entry.id], paths)[written.entry.id]
        base = entity_base(er.entity_id)
        payload = {**scope_fields(entity_context(ctx.conn, er.entity_id), high_water(ctx.conn, er.entity_id)),
                   "entry": {k: v for k, v in shaped.items() if k != "reversed_by"},
                   "version": written.entry.version, "audit_seq": written.audit.seq,
                   "links": {"self": f"{base}/entries/{written.entry.id}",
                             "events": f"{base}/entries/{written.entry.id}/events",
                             "reverse": f"{base}/entries/{written.entry.id}/reverse"}}
        return 201, payload

    return idempotency.run(er, raw, work)


@router.get("/entries", response_model=schemas.EntryPage, summary="Entries, filtered")
def list_entries(er: EntityRequest = Depends(entity_request),
                 status: str | None = Query(None, description="draft | posted | void"),
                 origin: str | None = Query(None),
                 from_: str | None = Query(None, alias="from", description="inclusive"),
                 to: str | None = Query(None, description="inclusive"),
                 account: str | None = Query(None, description="only entries with a leg on this account"),
                 text: str | None = Query(None, description="substring of the narration"),
                 limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    where: list[Any] = []
    if status is not None:
        where.append(t.entries.c.status == status)
    if origin is not None:
        where.append(t.entries.c.origin == origin)
    if from_ is not None:
        where.append(t.entries.c.dt >= parse_date(from_, path="from"))
    if to is not None:
        where.append(t.entries.c.dt <= parse_date(to, path="to"))
    if text:
        where.append(t.entries.c.narration.contains(text))
    if account is not None:
        where.append(sa.exists().where(t.postings.c.entry_id == t.entries.c.id,
                                       t.postings.c.entity_id == er.entity_id,
                                       t.postings.c.account_id == account))
    filters = {"status": status, "origin": origin, "from": from_, "to": to, "account": account,
               "text": text}
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        page = paginate(conn, er.entity_id, spec=ENTRIES, where=where, filters=filters, limit=size, cursor=cursor,
                        keys=er.cursor_keys, resync=f"{entity_base(er.entity_id)}/entries", render=lambda r: r)
        paths = views.account_paths(conn, er.entity_id)
        legs = views.postings_by_entry(conn, er.entity_id, [r.id for r in page.items], paths)
        items = [views.entry(r, er.entity_id, legs.get(r.id, [])) for r in page.items]
    return page.with_items(items).body(entity_id=entity["entity_id"], legal_name=entity["legal_name"],
                                       base_currency=entity["base_currency"])


def _entry_row(conn, entity_id: str, entry_id: str):
    row = conn.execute(sa.select(t.entries).where(t.entries.c.entity_id == entity_id,
                                                  t.entries.c.id == entry_id)).first()
    return require_row(row, "entry", entry_id)


@router.get("/entries/{entry_id}", response_model=schemas.EntryResponse,
            summary="One entry: its legs, what it is now, and every correction it is part of")
def get_entry(response: Response, entry_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    """Return one entry with its legs, its current ``state`` and every correction it is part of (issue 0007).

    Reviewing an entry means knowing what it is now: posted, corrected or reversed.  That is not a column.  It
    follows from the correction rows that name the entry and is derived in one place (``views.entry_state``).

    ``corrections`` is the chain from whichever end this entry sits on, so a mirror links back to what it
    reversed rather than reading as a lone negative entry.
    """
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        row = _entry_row(conn, er.entity_id, entry_id)
        paths = views.account_paths(conn, er.entity_id)
        legs = views.postings_by_entry(conn, er.entity_id, [row.id], paths).get(row.id, [])
        corrections = views.corrections_of(conn, er.entity_id, entry_id)
        seq = high_water(conn, er.entity_id)
    response.headers["ETag"] = f'"{row.version}"'
    mine = next((c for c in corrections if c["role"] == "original"), None)
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": seq,
            "entry": views.entry(row, er.entity_id, legs), "version": row.version,
            "reversed_by": mine["mirror_id"] if mine else None,
            "corrections": corrections, "state": views.entry_state(row, corrections)}


def _subject_ids(conn, entity_id: str, entry_id: str) -> list[str]:
    """Return every id under which something that happened to this entry was recorded.

    There are three kinds, and all are needed:

    * the entry itself, for a manual posting and anything filed against it afterwards;
    * every correction row that names it, because a correction is its own subject in the log; and
    * the statement lines its postings explain, because an entry booked off a statement has its creation
      recorded under the line (``line.booked``, subject kind ``statement_line``).

    The last is the ordinary case: nearly every entry in a real set of books arrives through a statement line.
    Without it, an entry's history would be empty although its whole provenance is in the log under a
    neighbouring id.  The browser harness caught exactly that.
    """
    ids = [entry_id]
    c = t.corrections
    rows = conn.execute(sa.select(c.c.id).where(c.c.entity_id == entity_id,
                                                sa.or_(c.c.original_id == entry_id, c.c.mirror_id == entry_id,
                                                       c.c.replacement_id == entry_id)))
    ids.extend(r[0] for r in rows)
    ids.extend(_explaining_lines(conn, entity_id, entry_id))
    return ids


def _explaining_lines(conn, entity_id: str, entry_id: str) -> list[str]:
    """Return the statement lines whose live explanation allocates to this entry.

    This lives in the API layer rather than the core repository on purpose.  ``statement_lines``,
    ``explanations`` and ``allocations`` belong to the reconciliation extension, and the core may not depend on
    it (``test_core_boundaries``).  The API layer is the composition root, where the two may meet.  Superseded
    explanations are excluded by ``live_explanation_clause``: a line re-explained onto a different account has a
    history with this entry but no longer explains it.
    """
    # An allocation names a posting, not an entry, so the entry is reached through its legs, the same join
    # ``read_provenance`` makes from the other end.
    rows = conn.execute(
        sa.select(t.statement_lines.c.id)
        .select_from(t.statement_lines
                     .join(t.explanations, sa.and_(t.explanations.c.entity_id == t.statement_lines.c.entity_id,
                                                   t.explanations.c.line_id == t.statement_lines.c.id))
                     .join(t.allocations, sa.and_(t.allocations.c.entity_id == t.explanations.c.entity_id,
                                                  t.allocations.c.explanation_id == t.explanations.c.id))
                     .join(t.postings, sa.and_(t.postings.c.entity_id == t.allocations.c.entity_id,
                                               t.postings.c.id == t.allocations.c.posting_id)))
        .where(t.statement_lines.c.entity_id == entity_id, t.postings.c.entry_id == entry_id,
               t.live_explanation_clause(entity_id))
        .distinct())
    return [r[0] for r in rows]


@router.get("/entries/{entry_id}/events", response_model=schemas.EntryEvents,
            summary="History of one entry: its posting, its corrections and what was filed against it")
def entry_events(entry_id: str, er: EntityRequest = Depends(entity_request),
                 since: int = Query(0, ge=0), limit: int | None = Query(None)) -> dict:
    size = page_limit(limit, default=er.config.event_default_limit, maximum=er.config.event_max_limit)
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        _entry_row(conn, er.entity_id, entry_id)
        subjects = _subject_ids(conn, er.entity_id, entry_id)
        rows = conn.execute(sa.select(t.events)
                            .where(t.events.c.entity_id == er.entity_id, t.events.c.seq > since,
                                   t.events.c.subject_id.in_(subjects))
                            .order_by(t.events.c.seq).limit(size))
        items = [views.event(e) for e in _as_events(rows)]
        water = high_water(conn, er.entity_id)
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": water, "entry_id": entry_id,
            "items": items, "since": since, "next_since": items[-1]["seq"] if items else since,
            "high_water": water}


def _as_events(rows):
    from haddon.repository.events import Event
    import json
    for r in rows:
        yield Event(r.seq, r.kind, r.subject_kind, r.subject_id, json.loads(r.before) if r.before else None,
                    json.loads(r.after) if r.after else None, r.reason, r.request_id, r.created_at, r.created_by)



@router.post("/entries/{entry_id}/reverse", status_code=201, operation_id="reverse_entry",
             responses={201: {"model": schemas.EntryReversal}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.EntryReversalSubmission.model_json_schema()}}}},
             summary="Reverse a posted entry, recording why somebody disagreed with it")
async def reverse(entry_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """Withdraw a posted entry without restating it; ``POST /entries/{id}/correct`` withdraws and restates.

    Nothing is edited or deleted.  The original stays posted and in the sums, an exact mirror is posted beside
    it, and both carry the actor that wrote them.  A reversal that would land in a closed month is refused;
    reopen the month to fix it.

    To move a statement line onto a different account, use ``POST /lines/{id}/reexplain``, which does this and
    the re-booking together so the line is never momentarily unexplained.
    """
    raw = await request.body()

    def work(ctx):
        from haddon import commands

        body = parse_body(raw, schemas.EntryReversalSubmission)
        original = require_row(read_entry(ctx.conn, er.entity_id, entry_id), "entry", entry_id)
        correction = commands.reverse_entry(ctx, original_id=entry_id, reason=body.reason,
                                            dt=body.dt or original.dt, narration=body.narration)
        entity = entity_context(ctx.conn, er.entity_id)
        paths = views.account_paths(ctx.conn, er.entity_id)
        mirror = views.entries_by_id(ctx.conn, er.entity_id, [correction.correction.mirror_id], paths)[correction.correction.mirror_id]
        base = entity_base(er.entity_id)
        return 201, {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
                     "base_currency": entity["base_currency"], "snapshot_seq": high_water(ctx.conn, er.entity_id),
                     "correction_id": correction.correction.id, "original_id": correction.correction.original_id,
                     "mirror_id": correction.correction.mirror_id,
                     "replacement_id": correction.correction.replacement_id,
                     "audit_seq": correction.audit.seq,
                     "entry": {k: v for k, v in mirror.items() if k != "reversed_by"},
                     "links": {"original": f"{base}/entries/{entry_id}",
                               "mirror": f"{base}/entries/{correction.correction.mirror_id}",
                               "events": f"{base}/entries/{entry_id}/events"}}

    return idempotency.run(er, raw, work)


@router.post("/entries/{entry_id}/correct", status_code=201, operation_id="correct_entry",
             responses={201: {"model": schemas.EntryCorrection}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.EntryCorrectionSubmission.model_json_schema()}}}},
             summary="Reverse a posted entry and post what it should have said, in one transaction")
async def correct(entry_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """Reverse a posted entry and post what it should have said, linked to the original (issue 0006).

    ``reverse`` withdraws a booking; this restates it, which is the ordinary case.  An entry on the wrong
    account, for the wrong amount or on the wrong date usually needs replacing, not just withdrawing.

    Both halves land in one transaction, so there is no moment at which the original is reversed and nothing
    stands in its place, and a concurrent report cannot see the books with the entry missing.  The replacement
    is an ordinary entry (account paths and exact decimal amounts) and goes through the same balance, account,
    precision and period-lock rules as any other posting.  A correction that would land in a closed month is
    refused; reopen the month to fix it.

    For a statement line, ``POST /lines/{id}/reexplain`` does this and the re-explanation together, so the line
    is never momentarily unexplained.
    """
    raw = await request.body()

    def work(ctx):
        from haddon import commands

        body = parse_body(raw, schemas.EntryCorrectionSubmission)
        require_row(read_entry(ctx.conn, er.entity_id, entry_id), "entry", entry_id)
        replacement = body.replacement
        corrected = commands.correct_entry(
            ctx, original_id=entry_id, reason=body.reason, dt=body.dt, narration=body.narration,
            replacement=commands.Replacement(
                dt=replacement.dt, narration=replacement.narration,
                origin=commands.DEFAULT_ORIGIN if replacement.origin is None else replacement.origin,
                legs=[commands.LegSpec(lg.account, lg.commodity, lg.amount, lg.memo, lg.tax_code)
                      for lg in replacement.legs],
                code=replacement.code, comment=replacement.comment,
                tags=tuple(tuple(tag) for tag in (replacement.tags or ()))))
        correction = corrected.correction
        entity = entity_context(ctx.conn, er.entity_id)
        paths = views.account_paths(ctx.conn, er.entity_id)
        shaped = views.entries_by_id(ctx.conn, er.entity_id,
                                     [correction.mirror_id, correction.replacement_id], paths)
        base = entity_base(er.entity_id)
        return 201, {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
                     "base_currency": entity["base_currency"], "snapshot_seq": high_water(ctx.conn, er.entity_id),
                     "correction_id": correction.id, "original_id": correction.original_id,
                     "mirror_id": correction.mirror_id, "replacement_id": correction.replacement_id,
                     "audit_seq": corrected.audit.seq,
                     "entry": {k: v for k, v in shaped[correction.mirror_id].items() if k != "reversed_by"},
                     "replacement": {k: v for k, v in shaped[correction.replacement_id].items()
                                     if k != "reversed_by"},
                     "links": {"original": f"{base}/entries/{entry_id}",
                               "mirror": f"{base}/entries/{correction.mirror_id}",
                               "replacement": f"{base}/entries/{correction.replacement_id}",
                               "events": f"{base}/entries/{entry_id}/events"}}

    return idempotency.run(er, raw, work)
