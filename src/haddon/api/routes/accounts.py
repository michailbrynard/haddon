"""The chart and the register.

The register needs care.  ``GET /accounts/{id}/register`` answers "what happened on this account, and what was
the balance after each movement", and both halves have a trap:

* The running balance must include pre-page history.  A page two whose running column restarts at the top looks
  like an account balance and is not one.  So the report is computed for the whole journal up to ``to`` and then
  sliced, and every line's running balance is accumulated from the journal's first day, whatever page it lands
  on.  The opening balance reported alongside is the running total per commodity immediately before the first
  line in range, the same number the caller would get by summing everything the page does not show.
* The range is any valid date range.  A journal is continuous (issue 0002), so a register may cross a
  December/January boundary like any other report; there is no carry row into a following period.

Ordering is ``(dt, entry_id, posting_seq)``.  The repository breaks ties within a date by entry ``created_at``
before id, a stable order that is not lexicographic in the key.  So the cursor records the last key emitted and
the page resumes at that key's position in the ordered report, never at "everything greater than it".

The three writes are the chart's whole lifecycle: ``POST /accounts`` adds an account,
``POST /accounts/{id}/archive`` retires one and ``POST /accounts/{id}/restore`` puts a retired one back.  All
three are provisioning, not booking: they touch no entry, posting or balance.  They go through ``provisioning``
beneath ``haddon.commands``, the same functions that wrote every existing account, so an account added while
reviewing a statement is indistinguishable afterwards from one written at setup, and the export declares it like
any other.  The period lock does not apply, because a lock is about dated movement and an account has no date.

Archiving is not deletion and does not reach backwards.  Everything posted to the account stays posted and keeps
appearing in every report covering its date; only a new leg on the account is refused.  That is why archiving
needs a reason and creation does not: retiring an account is a decision about the books, adding one is
structure.

Because it is a decision, it can be taken back.  ``restore`` is to archive what ``reopen`` is to ``close``: a
reason, an ``expected_version``, one audit action, and no change to anything already posted.  Without it, an
account archived while it still carried a balance, a posted entry or an unbooked statement line was stuck: the
reversal, the correction, the booking and the close were all refused ``account_archived`` for good
(``decisions.md``, "Nothing a command can reach is a place commands cannot leave").
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response

from haddon import commands
from haddon.api import idempotency, schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.pagination import ACCOUNTS, page_limit, paginate, paginate_sequence
from haddon.api.registry import entity_context, require_row
from haddon.api.scoping import scoped_range
from haddon.api.wire import entity_base, micros_map, parse_body, scope_fields
from haddon.domain.micros import Micros
from haddon.repository import tables as t
from haddon.repository.events import high_water
from haddon.reports.register import register as register_report

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["accounts"], responses=schemas.ERRORS)


def _account_row(conn, entity_id: str, account_id: str):
    row = conn.execute(sa.select(t.accounts).where(t.accounts.c.entity_id == entity_id,
                                                   t.accounts.c.id == account_id)).first()
    return require_row(row, "account", account_id)


@router.get("/accounts", response_model=schemas.AccountPage, summary="The chart of accounts")
def list_accounts(er: EntityRequest = Depends(entity_request),
                  type: str | None = Query(None, description="asset | liability | equity | income | expense"),
                  archived: bool | None = Query(None),
                  text: str | None = Query(None, description="substring of the account path or code"),
                  limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    where: list[Any] = []
    if type is not None:
        where.append(t.accounts.c.type == type)
    if archived is not None:
        where.append(t.accounts.c.archived == int(archived))
    if text:
        where.append(sa.or_(t.accounts.c.path.contains(text), t.accounts.c.code.contains(text)))
    filters = {"type": type, "archived": archived, "text": text}
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        page = paginate(conn, er.entity_id, spec=ACCOUNTS, where=where, filters=filters, limit=size, cursor=cursor,
                        keys=er.cursor_keys, resync=f"{entity_base(er.entity_id)}/accounts", render=lambda r: r)
        commodities = views.commodities_by_account(conn, er.entity_id, [r.id for r in page.items])
        items = [views.account(r, er.entity_id, commodities.get(r.id, [])) for r in page.items]
    return page.with_items(items).body(entity_id=entity["entity_id"], legal_name=entity["legal_name"],
                                       base_currency=entity["base_currency"])


def _chart_change(ctx, er, change) -> dict:
    """Build the reply every account command shares: the account as it now stands, and the action that wrote it."""
    account = change.account
    commodities = views.commodities_by_account(ctx.conn, er.entity_id, [account.id]).get(account.id, [])
    base = entity_base(er.entity_id)
    return {**scope_fields(entity_context(ctx.conn, er.entity_id), high_water(ctx.conn, er.entity_id)),
            "account": views.account(account, er.entity_id, commodities), "version": account.version,
            "audit_seq": change.audit.seq,
            # Offer only the lifecycle command that would succeed from here (archive or restore), so a caller
            # following links is never pointed at a 409.
            "links": {"self": f"{base}/accounts/{account.id}", "accounts": f"{base}/accounts",
                      "register": f"{base}/accounts/{account.id}/register",
                      ("restore" if account.archived else "archive"):
                          f"{base}/accounts/{account.id}/{'restore' if account.archived else 'archive'}",
                      "events": f"{base}/events"}}


@router.post("/accounts", status_code=201, operation_id="create_account",
             responses={201: {"model": schemas.ChartChangeResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.AccountCreationSubmission.model_json_schema()}}}},
             summary="Add an account to the chart")
async def create_account(request: Request, er: EntityRequest = Depends(entity_request)):
    """Write one new account and return it as it now stands, so the caller can book to it without re-reading.

    A path already in the chart is a 409, not a second account with the same name.  Two accounts that print
    identically in every report is not a useful state, and the caller who hit it wanted the account it already
    has.

    Writes an ``account.created`` audit action recording who extended the chart and why (issue 0006).
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.AccountCreationSubmission)
        change = commands.create_account(ctx, path=body.path, type=body.type, commodities=body.commodities,
                                         is_cash=body.is_cash, code=body.code, tax_code=body.tax_code,
                                         id=body.id, reason=body.reason)
        return 201, _chart_change(ctx, er, change)

    return idempotency.run(er, raw, work)


@router.post("/accounts/{account_id}/archive", status_code=201, operation_id="archive_account",
             responses={201: {"model": schemas.ChartChangeResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.AccountArchiveSubmission.model_json_schema()}}}},
             summary="Retire an account: nothing may be posted to it again, and everything posted to it stays")
async def archive_account(account_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """Archive an account.  This is the only way an account leaves the chart.

    Nothing is deleted and no history moves: every entry already posted to the account stays where it is and
    keeps appearing in every report that covers its date.  From now on, a leg on the account is refused at
    posting time (``account_archived``).  ``restore`` undoes this.

    ``account_id`` accepts the account's path as well as its id, like everywhere an account is named in the
    command surface.  A reason is required, and ``expected_version`` is the version the caller read.
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.AccountArchiveSubmission)
        change = commands.archive_account(ctx, account=account_id, reason=body.reason,
                                          expected_version=body.expected_version)
        return 201, _chart_change(ctx, er, change)

    return idempotency.run(er, raw, work)


@router.post("/accounts/{account_id}/restore", status_code=201, operation_id="restore_account",
             responses={201: {"model": schemas.ChartChangeResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.AccountRestoreSubmission.model_json_schema()}}}},
             summary="Put a retired account back in use, so what it still carries can be finished")
async def restore_account(account_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """Undo an archive.  This is to ``archive_account`` what ``reopen_period`` is to ``close_period``.

    While an account is archived, reversing against it, correcting, booking to it and closing over it are all
    refused ``account_archived``.  Restoring is how an account archived while it still held a balance, a posted
    entry or an unbooked statement line gets finished.

    History does not move in either direction; a leg on this account is simply accepted again.  A reason is
    required, as for archiving, and ``expected_version`` is the version the caller read.
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.AccountRestoreSubmission)
        change = commands.restore_account(ctx, account=account_id, reason=body.reason,
                                          expected_version=body.expected_version)
        return 201, _chart_change(ctx, er, change)

    return idempotency.run(er, raw, work)


@router.get("/accounts/{account_id}", response_model=schemas.AccountResponse, summary="One account")
def get_account(response: Response, account_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        row = _account_row(conn, er.entity_id, account_id)
        commodities = views.commodities_by_account(conn, er.entity_id, [row.id]).get(row.id, [])
        seq = high_water(conn, er.entity_id)
    response.headers["ETag"] = f'"{row.version}"'
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": seq,
            "account": views.account(row, er.entity_id, commodities), "version": row.version}


def _opening(lines, start: str | None) -> dict[str, str]:
    """Return the per-commodity running balance immediately before ``start``, i.e. everything the page omits."""
    if start is None:
        return {}
    opening: dict[str, Micros] = {}
    for ln in lines:
        if ln.dt >= start:
            continue
        if ln.running is not None:
            opening[ln.commodity] = ln.running
    return micros_map(opening)


@router.get("/accounts/{account_id}/register", response_model=schemas.Register,
            summary="Movements and running balances of one account over a date range")
def account_register(account_id: str, er: EntityRequest = Depends(entity_request),
                     from_: str | None = Query(None, alias="from", description="inclusive"),
                     to: str | None = Query(None, description="inclusive"),
                     commodity: str | None = Query(None),
                     limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        row = _account_row(conn, er.entity_id, account_id)
        start, end = scoped_range(conn, er.entity_id, start=from_, end=to)
        # The whole journal up to `end`, so the running balances already carry everything before the window.
        full = register_report(conn, er.entity_id, account_id=account_id, start=None, end=end)
        seq = high_water(conn, er.entity_id)
    lines = [ln for ln in full.lines if commodity is None or ln.commodity == commodity]
    opening = _opening(lines, start)
    in_range = [ln for ln in lines if start is None or ln.dt >= start]
    base = entity_base(er.entity_id)
    query = "&".join(f"{name}={value}" for name, value in
                     (("from", start), ("to", end)) if value is not None)
    resync = f"{base}/accounts/{account_id}/register" + (f"?{query}" if query else "")
    page = paginate_sequence(in_range, entity_id=er.entity_id, kind="register",
                             filters={"from": start, "to": end, "commodity": commodity,
                                      "account_id": account_id},
                             limit=size, cursor=cursor, keys=er.cursor_keys, resync=resync, snapshot_seq=seq,
                             key_of=lambda ln: [ln.dt, ln.entry_id, ln.seq], render=views.register_line)
    return {"entity_id": entity["entity_id"], "legal_name": entity["legal_name"],
            "base_currency": entity["base_currency"], "snapshot_seq": page.snapshot_seq,
            "account_id": row.id, "account_path": row.path, "start": start, "end": end,
            "opening_micros": opening, "items": page.items, "next_cursor": page.next_cursor,
            "links": {"account": f"{base}/accounts/{row.id}", "self": resync,
                      "trial_balance": f"{base}/reports/trial-balance" + (f"?as_of={end}" if end else "")}}
