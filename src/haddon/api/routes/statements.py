"""Statements: load one, replace one loaded wrongly, read them back, and ask where a line came from.

``POST /statements`` is the generic statement input: an account, a period, an opening and a closing balance, a
list of normalised lines and the original document.  It is the ledger's only input surface for bank data.  No
format is named anywhere below.  Parsing whatever the bank produced happens outside the ledger and what arrives
here is already normalised, which is why the ledger validates it strictly and stores a reference back to the file
it came from.

``POST /statements/{id}/replace`` is the other write.  A statement loaded from the wrong page, the wrong account
or a truncated download is replaced, not edited.  The predecessor stays as it was and stays readable, the
replacement points at it, and the database allows at most one successor.  Replacing is refused while the period
is locked or while a line of the predecessor is still booked; undo those first, because a replacement must not
orphan an entry it caused.

``GET /statements``, ``GET /statements/{id}`` and ``GET /statements/{id}/lines`` read the record back.
``GET /provenance`` answers the question the whole chain exists for: given a line, a posting or an entry, which
document did this come from?  It is a separate route rather than a field on the others because it is asked from
either end, forwards from a line and backwards from a posting through the allocations that explain it.

Lines are paginated rather than embedded, since a year of daily transactions is an ordinary statement.  They page
in document order (``line_no``), the order a person reading the original file expects, not insertion order.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from haddon.api import idempotency, schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.pagination import STATEMENT_LINES, STATEMENTS, page_limit, paginate
from haddon.api.registry import entity_context, require_row
from haddon.api.wire import entity_base, parse_body, scope_fields
from haddon.domain.dates import parse_date
from haddon import reconciliation_commands as rc
from haddon.repository import statements as sm
from haddon.repository import tables as t
from haddon.repository.events import high_water

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["statements"], responses=schemas.ERRORS)


def _document(body) -> sm.Document:
    return sm.Document(sha256=body.document.sha256, filename=body.document.filename,
                       media_type=body.document.media_type, size_bytes=body.document.size_bytes,
                       blob_path=body.document.blob_path, external_ref=body.document.external_ref)


def _load(ctx, er, body, *, replaces_id: str | None):
    """Run either statement write through its published command and shape the reply."""
    submitted = dict(
        account=body.account, commodity=body.commodity, period_start=body.period_start,
        period_end=body.period_end, opening=body.opening, closing=body.closing,
        lines=[rc.LineSpec(dt=ln.dt, description=ln.description, amount=ln.amount, currency=ln.currency,
                           reference=ln.reference, raw=ln.raw, source_key=ln.source_key) for ln in body.lines],
        document=_document(body), locator=body.locator, page=body.page, reason=body.reason, id=body.id)
    loaded = (rc.load_statement(ctx, **submitted) if replaces_id is None
              else rc.replace_statement(ctx, replaces_id=replaces_id, **submitted))
    entity = entity_context(ctx.conn, er.entity_id)
    paths = views.account_paths(ctx.conn, er.entity_id)
    return 201, {**scope_fields(entity, high_water(ctx.conn, er.entity_id)),
                 "statement": views.statement(loaded.statement, er.entity_id, paths),
                 "audit_seq": loaded.audit.seq}


@router.post("/statements", status_code=201, operation_id="load_statement",
             responses={201: {"model": schemas.StatementWriteResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.StatementSubmission.model_json_schema()}}}},
             summary="Load one statement: an account, a period, an opening and closing balance, lines, and the document")
async def load_statement(request: Request, er: EntityRequest = Depends(entity_request)):
    raw = await request.body()

    def work(ctx):
        return _load(ctx, er, parse_body(raw, schemas.StatementSubmission), replaces_id=None)

    return idempotency.run(er, raw, work)


@router.post("/statements/{statement_id}/replace", status_code=201, operation_id="replace_statement",
             responses={201: {"model": schemas.StatementWriteResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.StatementReplacementSubmission.model_json_schema()}}}},
             summary="Replace a statement loaded wrongly; the predecessor is kept and visibly linked")
async def replace_statement(statement_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    raw = await request.body()

    def work(ctx):
        return _load(ctx, er, parse_body(raw, schemas.StatementReplacementSubmission), replaces_id=statement_id)

    return idempotency.run(er, raw, work)


@router.get("/statements", response_model=schemas.StatementPage,
            summary="Loaded statements, newest listing order, filtered by account, commodity or date range")
def list_statements(er: EntityRequest = Depends(entity_request),
                    account_id: str | None = Query(None), commodity: str | None = Query(None),
                    from_: str | None = Query(None, alias="from", description="inclusive, on the statement period"),
                    to: str | None = Query(None, description="inclusive, on the statement period"),
                    live: bool | None = Query(None, description="keep only statements nothing has replaced"),
                    limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    where = []
    if account_id is not None:
        where.append(t.statements.c.account_id == account_id)
    if commodity is not None:
        where.append(t.statements.c.commodity == commodity)
    if from_ is not None:
        where.append(t.statements.c.period_end >= parse_date(from_, path="from"))
    if to is not None:
        where.append(t.statements.c.period_start <= parse_date(to, path="to"))
    if live is not None:
        # Same definition of "live" as the SQL view (``tables.live_statement_clause``).
        where.append(t.live_statement_clause() if live else ~t.live_statement_clause())
    filters = {"account_id": account_id, "commodity": commodity, "from": from_, "to": to, "live": live}
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        page = paginate(conn, er.entity_id, spec=STATEMENTS, where=where, filters=filters, limit=size,
                        cursor=cursor, keys=er.cursor_keys,
                        resync=f"{entity_base(er.entity_id)}/statements", render=lambda r: r)
        paths = views.account_paths(conn, er.entity_id)
        items = [views.statement(sm.get_statement(conn, er.entity_id, row.id), er.entity_id, paths)
                 for row in page.items]
    return page.with_items(items).body(entity_id=entity["entity_id"], legal_name=entity["legal_name"],
                                       base_currency=entity["base_currency"])


@router.get("/statements/{statement_id}", response_model=schemas.StatementResponse,
            summary="One loaded statement: its period, its balances, its document and its replacement chain")
def get_statement(statement_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        statement = require_row(sm.get_statement(conn, er.entity_id, statement_id), "statement", statement_id)
        paths = views.account_paths(conn, er.entity_id)
        seq = high_water(conn, er.entity_id)
    return {**scope_fields(entity, seq), "statement": views.statement(statement, er.entity_id, paths)}


@router.get("/statements/{statement_id}/lines", response_model=schemas.StatementLinePage,
            summary="The lines of one statement, in document order")
def list_statement_lines(statement_id: str, er: EntityRequest = Depends(entity_request),
                         limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    base = entity_base(er.entity_id)
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        require_row(sm.get_statement(conn, er.entity_id, statement_id), "statement", statement_id)
        page = paginate(conn, er.entity_id, spec=STATEMENT_LINES,
                        where=[t.statement_lines.c.statement_id == statement_id],
                        filters={"statement_id": statement_id}, limit=size, cursor=cursor, keys=er.cursor_keys,
                        resync=f"{base}/statements/{statement_id}/lines",
                        render=lambda r: views.statement_line(sm.line_of_row(r), er.entity_id))
    return page.body(entity_id=entity["entity_id"], legal_name=entity["legal_name"],
                     base_currency=entity["base_currency"], statement_id=statement_id,
                     links={"statement": f"{base}/statements/{statement_id}"})


@router.get("/provenance", response_model=schemas.Provenance,
            summary="Where did this come from? Trace a statement line, a posting or an entry back to its document")
def get_provenance(er: EntityRequest = Depends(entity_request),
                   line_id: str | None = Query(None, description="a statement line"),
                   posting_id: str | None = Query(None, description="one leg of an entry"),
                   entry_id: str | None = Query(None, description="a whole entry")) -> dict:
    base = entity_base(er.entity_id)
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        prov = sm.read_provenance(conn, er.entity_id, line_id=line_id, posting_id=posting_id, entry_id=entry_id)
        seq = high_water(conn, er.entity_id)
    body = prov.as_json()
    return {**scope_fields(entity, seq), "subject": body["subject"], "line_count": body["line_count"],
            "lines": [{**ln, "links": {"statement": f"{base}/statements/{ln['statement_id']}",
                                       "provenance": f"{base}/provenance?line_id={ln['line_id']}"}}
                      for ln in body["lines"]],
            "evidence": body["evidence"], "links": {"statements": f"{base}/statements"}}
