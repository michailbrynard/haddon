"""Documents: how a file that is not a bank statement gets into the books.

``POST /statements`` takes one document, the statement itself.  Before this module there was no other way in, so
the invoices, receipts and contracts that decided how a year was booked survived only as narration text.  This
module is the general entry point:

* ``POST /documents`` registers a file by its ``sha256``.  The same bytes twice is the same row, so a caller
  never has to check first; the response says ``created: false`` when the document was already there.
* ``POST /entries/{id}/documents`` files a registered document against an entry, with the page, the place in
  the document and a caption.
* ``GET /documents`` and ``GET /documents/{id}`` read them back, the second with every record that cites it.

There is no separate attachment table.  Registration writes ``source_files``, already content-addressed by a
UNIQUE ``sha256``, and filing writes ``evidence_links``, which already binds a file, page, locator and caption to
an entry.  Because of that, ``GET /provenance?entry_id=...`` answers "what documents does this entry rest on?"
with no new read.

The bytes are not uploaded.  ``blob_path`` records where the file is kept and registration does not fetch it,
the same as for statements.

They are served, though.  ``GET /documents/{id}/content`` (issue 0007) hands over the file a booking rests on,
read from under a configured evidence root and verified against its registered hash.  A hash and a server-local
path are not evidence a reviewer can use, and the path itself is a disclosure, so ``blob_path`` appears in no
response here.  ``storage`` says whether the bytes are available and ``links.content`` is where they are.
``haddon.api.evidence`` holds the rules and the refusals.
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response

from haddon.api import evidence, idempotency, schemas
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.pagination import DOCUMENTS, page_limit, paginate
from haddon.api.registry import entity_context, require_row
from haddon.api.wire import entity_base, parse_body, scope_fields
from haddon.repository import documents as dc
from haddon.repository import tables as t
from haddon.repository.events import high_water
from haddon.repository.statements import Document

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["documents"], responses=schemas.ERRORS)


def _filing_counts(conn, entity_id: str, file_ids: list[str]) -> dict[str, int]:
    """Count the records citing each of these documents, in one query rather than one per row."""
    if not file_ids:
        return {}
    rows = conn.execute(
        sa.select(t.evidence_links.c.document_id, sa.func.count())
        .where(t.evidence_links.c.entity_id == entity_id,
               t.evidence_links.c.document_id.in_(file_ids))
        .group_by(t.evidence_links.c.document_id))
    return {fid: int(n) for fid, n in rows}


def _document_view(entity_id: str, row, filing_count: int) -> dict:
    """Render one document as a reader sees it.

    ``links.content`` is offered only for a document whose bytes this service holds.  An ``external`` document
    has nothing at that address, and the link would only give the interface a button that always fails.
    """
    base = entity_base(entity_id)
    external = row.external_ref or None
    links = {"self": f"{base}/documents/{row.id}", "documents": f"{base}/documents"}
    if external is None:
        links["content"] = f"{base}/documents/{row.id}/content"
    return {"id": row.id, "sha256": row.sha256, "filename": row.filename, "media_type": row.media_type,
            "size_bytes": int(row.size_bytes), "storage": "external" if external else "stored",
            "external_ref": external, "filing_count": int(filing_count),
            "created_at": row.created_at, "created_by": row.created_by, "links": links}


@router.post("/documents", status_code=201, operation_id="register_document",
             responses={200: {"model": schemas.DocumentResponse},
                        201: {"model": schemas.DocumentResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.DocumentSubmission.model_json_schema()}}}},
             summary="Register a document by its hash: an invoice, a receipt, a contract, anything the books rest on")
async def register_document(request: Request, er: EntityRequest = Depends(entity_request)):
    """Register a document; 201 when this call registered it, 200 when the same bytes were already here.

    A document is identified by its content, so two entries resting on one invoice will register it twice in the
    ordinary course of events.  That is not refused, and the caller does not need to look first.  The one
    contradiction that is refused is a different ``size_bytes`` for the same hash.
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.DocumentSubmission)
        registration = dc.register_document(
            ctx, document=Document(sha256=body.sha256, filename=body.filename, media_type=body.media_type,
                                   size_bytes=body.size_bytes, blob_path=body.blob_path,
                                   external_ref=body.external_ref),
            reason=body.reason)
        row = dc.document_row(ctx.conn, er.entity_id, registration.document_id)
        filings = dc.filings_of_document(ctx.conn, er.entity_id, registration.document_id)
        entity = entity_context(ctx.conn, er.entity_id)
        return (201 if registration.created else 200), {
            **scope_fields(entity, high_water(ctx.conn, er.entity_id)),
            "document": _document_view(er.entity_id, row, len(filings)),
            "created": registration.created, "filings": filings}

    return idempotency.run(er, raw, work)


@router.post("/entries/{entry_id}/documents", status_code=201, operation_id="file_document",
             responses={201: {"model": schemas.EntryFilingResponse}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.DocumentFilingSubmission.model_json_schema()}}}},
             summary="File a document against an entry: what this booking rests on, and where in the file it says so")
async def file_document(entry_id: str, request: Request, er: EntityRequest = Depends(entity_request)):
    """File a registered document against an entry, so "treatment follows the document" can be checked.

    An entry booked gross because no deductible document existed, and one booked net because a full invoice
    did, differ by the document, so the document has to be reachable from the entry.  It is linked through the
    same ``evidence_links`` chain the statement loader uses, and ``GET /provenance?entry_id=`` reads it back.

    A closed period does not refuse this.  A late invoice for a closed month is ordinary, and filing it changes
    nothing that month reports (migration 0004).
    """
    raw = await request.body()

    def work(ctx):
        body = parse_body(raw, schemas.DocumentFilingSubmission)
        filing = dc.file_against_entry(ctx, entry_id=entry_id, document_id=body.document_id,
                                       locator=body.locator, caption=body.caption, page=body.page,
                                       reason=body.reason)
        row = dc.document_row(ctx.conn, er.entity_id, body.document_id)
        base = entity_base(er.entity_id)
        entity = entity_context(ctx.conn, er.entity_id)
        return 201, {**scope_fields(entity, high_water(ctx.conn, er.entity_id)),
                     "filing": {**filing.as_json(), "document": dc.document_of_row(row).as_view()},
                     "links": {"entry": f"{base}/entries/{entry_id}",
                               "document": f"{base}/documents/{body.document_id}",
                               "provenance": f"{base}/provenance?entry_id={entry_id}"}}

    return idempotency.run(er, raw, work)


@router.get("/documents", response_model=schemas.DocumentPage,
            summary="Registered documents, newest listing order, optionally by hash")
def list_documents(er: EntityRequest = Depends(entity_request),
                   sha256: str | None = Query(None, description="exact content hash, to ask whether a file is here"),
                   limit: int | None = Query(None), cursor: str | None = Query(None)) -> dict:
    size = page_limit(limit, default=er.config.default_limit, maximum=er.config.max_limit)
    where = [] if sha256 is None else [t.documents.c.sha256 == sha256]
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        page = paginate(conn, er.entity_id, spec=DOCUMENTS, where=where, filters={"sha256": sha256}, limit=size,
                        cursor=cursor, keys=er.cursor_keys,
                        resync=f"{entity_base(er.entity_id)}/documents", render=lambda r: r)
        counts = _filing_counts(conn, er.entity_id, [r.id for r in page.items])
        items = [_document_view(er.entity_id, r, counts.get(r.id, 0)) for r in page.items]
    return page.with_items(items).body(entity_id=entity["entity_id"], legal_name=entity["legal_name"],
                                       base_currency=entity["base_currency"])


@router.get("/documents/{document_id}", response_model=schemas.DocumentResponse,
            summary="One document and every record that cites it")
def get_document(document_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        row = require_row(dc.document_row(conn, er.entity_id, document_id), "document", document_id)
        filings = dc.filings_of_document(conn, er.entity_id, document_id)
        seq = high_water(conn, er.entity_id)
    return {**scope_fields(entity, seq), "document": _document_view(er.entity_id, row, len(filings)),
            "created": False, "filings": filings}


@router.get("/documents/{document_id}/content", response_class=Response,
            responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string",
                                                                                "format": "binary"}}},
                             "description": "the registered bytes, verified against the registered hash"},
                       **schemas.ERRORS},
            summary="The document itself: the bytes this booking rests on, verified against their hash")
def document_content(document_id: str, er: EntityRequest = Depends(entity_request),
                     disposition: str = Query("attachment", pattern="^(attachment|inline)$",
                                              description="inline is honoured only for media types that "
                                                          "cannot execute; everything else downloads")) -> Response:
    """Return the document's bytes, verified against the registered hash (issue 0007, acceptance criterion 3).

    The bytes come only from under the configured ``blob_root``; a ``blob_path`` is caller-supplied, so without
    that containment it would be a file-read primitive.  They are hashed before any byte is returned, and
    ``inline`` is honoured only for media types that cannot execute.  A refusal never names a filesystem path,
    not even when the file is simply missing.

    There is no finer authorization than the entity scoping every route already has.  Haddon records authority
    and does not enforce it (``decisions.md``, "Authorisation").  A credential reaches this route only for an
    entity it is scoped to, and an id from another entity's file is a plain 404 like any other.
    """
    with er.reader() as conn:
        row = require_row(dc.document_row(conn, er.entity_id, document_id), "document", document_id)
        content = evidence.load(sha256=row.sha256, filename=row.filename, media_type=row.media_type,
                                size_bytes=int(row.size_bytes), blob_path=row.blob_path,
                                external_ref=row.external_ref, blob_root=er.config.blob_root,
                                inline=disposition == "inline")
    return Response(content=content.data, media_type=content.media_type, headers={
        "Content-Disposition": content.disposition,
        "X-Content-Type-Options": "nosniff",
        # Content-addressed and just verified against that address, so the same id can never legitimately
        # answer different bytes.
        "ETag": f'"{row.sha256}"',
        "Cache-Control": "private, max-age=3600",
    })
