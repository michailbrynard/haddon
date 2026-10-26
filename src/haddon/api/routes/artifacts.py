"""The export resource: running one, reading the immutable rows it wrote, and fetching the files themselves.

``POST /exports`` renders a range of this book as hledger journal files, checks them with the pinned binary and
publishes them.  ``GET /snapshots/{id}`` and ``GET /exports/{id}`` read back the rows it records, and
``GET /exports/{id}/artifacts/{name}`` hands over a published file.

Neither row is ever updated or deleted (triggers refuse both), so together they are the durable claim that a
given state was reported on and that exactly those bytes were rendered from it and passed
``hledger check --strict`` under the pinned binary.  Both are read from the entity's own file, so an id belonging
to another entity is a plain 404.

Nothing here reimplements the export.  The write calls :func:`haddon.exporting.exporter.export_book`, the same
function ``bin/haddon-export`` calls, so chunk C2a's determinism and byte-identity guarantees hold either way: the
same book exported twice is the same bytes, and re-exporting an unchanged range reuses its snapshot.  This module
adds the transport: the range as a request, the idempotency contract, and where the files go.

Where they go is configuration, not the caller's choice.  ``export_root`` says where this deployment writes
exports, and each entity gets its own subdirectory (``ApiConfig.export_dir_for``).  A deployment that has not set
it is refused rather than having a directory chosen for it.  An output directory in the request would make a
published command a write-anywhere primitive, which is also why ``entity_root`` exists for ``POST /v1/entities``
and ``blob_root`` for a document's bytes.

Chunk D1 described the artifacts but did not serve them ("handing out file bytes is an authenticated download
surface with its own rules").  Those rules are in :mod:`haddon.api.exported`.  They are the rules a document's
bytes get (confinement to a configured root, and the recorded digest re-checked before a byte goes out) because
they prevent the same failure.
"""

from __future__ import annotations

from urllib.parse import quote

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from haddon.api import errors as api_errors
from haddon.api import exported, idempotency, schemas, views
from haddon.api.deps import EntityRequest, entity_request
from haddon.api.registry import entity_context, require_row
from haddon.api.wire import entity_base, parse_body, scope_fields
from haddon.domain.errors import INTERNAL_ERROR, NOT_FOUND, HaddonError, validation
from haddon.exporting.exporter import ArtifactMismatch, ExportBlocked, ExportResult, export_book
from haddon.exporting.render import UnrenderableContent
from haddon.repository import tables as t
from haddon.repository.ctx import new_id
from haddon.repository.events import high_water
from haddon.repository.requests import check_idempotency, record_request

router = APIRouter(prefix="/v1/entities/{entity_id}", tags=["artifacts"], responses=schemas.ERRORS)


@router.get("/snapshots/{snapshot_id}", response_model=schemas.Snapshot,
            summary="One immutable report snapshot: its state hash, manifest, report and checks")
def get_snapshot(snapshot_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        row = require_row(conn.execute(sa.select(t.snapshots).where(
            t.snapshots.c.entity_id == er.entity_id, t.snapshots.c.id == snapshot_id)).first(),
            "snapshot", snapshot_id)
        seq = high_water(conn, er.entity_id)
    return {**views.snapshot(row, er.entity_id), "entity_id": entity["entity_id"],
            "legal_name": entity["legal_name"], "base_currency": entity["base_currency"], "snapshot_seq": seq}


@router.get("/exports/{export_id}", response_model=schemas.Export,
            summary="One immutable export: artifacts, manifest digest and the verifier's verdict")
def get_export(export_id: str, er: EntityRequest = Depends(entity_request)) -> dict:
    with er.reader() as conn:
        entity = entity_context(conn, er.entity_id)
        row = require_row(conn.execute(sa.select(t.exports).where(
            t.exports.c.entity_id == er.entity_id, t.exports.c.id == export_id)).first(), "export", export_id)
        seq = high_water(conn, er.entity_id)
    return {**views.export(row, er.entity_id), "entity_id": entity["entity_id"],
            "legal_name": entity["legal_name"], "base_currency": entity["base_currency"], "snapshot_seq": seq}


def _artifact_href(entity_id: str, export_id: str, name: str) -> str:
    return f"{entity_base(entity_id)}/exports/{export_id}/artifacts/{quote(name, safe='')}"


def _run_view(conn, entity_id: str, result: ExportResult) -> dict:
    """Render one export as its caller reads it: the rows it wrote, the range it covered, and where the files are.

    Built from the :class:`ExportResult` rather than re-read, since it is assembled inside the transaction that
    wrote those rows.  The absolute directory on this machine is not included: ``ledger_dir`` is the relative
    one the manifest carries, and each artifact is addressed by the link beside it.
    """
    base = entity_base(entity_id)
    return {**scope_fields(entity_context(conn, entity_id), high_water(conn, entity_id)),
            "export_id": result.export_id, "snapshot_id": result.snapshot_id,
            "operation_id": result.operation_id, "state_hash": result.state_hash,
            "manifest_sha256": result.manifest_sha256,
            "range_start": result.start, "range_end": result.end,
            "ledger_dir": result.manifest["ledger_dir"],
            "artifacts": [{**a.as_json(), "href": _artifact_href(entity_id, result.export_id, a.path)}
                          for a in result.artifacts],
            "counts": result.counts, "verifier": result.verifier, "event_seq": result.event_seq,
            "links": {"self": f"{base}/exports/{result.export_id}",
                      "snapshot": f"{base}/snapshots/{result.snapshot_id}",
                      "events": f"{base}/events"}}


@router.post("/exports", status_code=201, operation_id="run_export",
             responses={201: {"model": schemas.ExportRun}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.ExportSubmission.model_json_schema()}}}},
             summary="Export a range of this book as hledger journal files, verified by the pinned binary")
async def run_export(request: Request, er: EntityRequest = Depends(entity_request)):
    """Render the books as a journal anybody can read with hledger, and record that it was done.

    This is the same export ``bin/haddon-export`` runs on the machine.  It reads the store, renders
    deterministically, stages the files, runs ``hledger check --strict`` on them under the pinned binary,
    publishes them only if that passes, and then writes one ``export`` operation, a snapshot, the immutable
    ``exports`` row and an ``export.completed`` event.  A strict-check failure publishes nothing and records
    nothing.

    The reply is recorded in the same transaction as the export (``export_book(also_record=...)``), so a retry
    under the same ``Idempotency-Key`` replays the original 201 instead of exporting again, and there is no
    window in which the export is durable and its reply is not.  Two exports of one entity are serialised,
    because the directory they publish into is exclusively managed.

    The work runs in a worker thread.  Every other write here is a few SQLite statements; this one renders a
    whole range and waits on a subprocess, which on the event loop would stop the process answering anything,
    ``/healthz`` included, until it finished.  The lock is taken inside that thread for the same reason.

    A range with nothing posted in it is refused (``422``): an empty directory is not an export.
    """
    refusal = er.config.export_refusal()
    if refusal is not None:
        raise refusal
    raw = await request.body()
    body = parse_body(raw, schemas.ExportSubmission)
    if body.year is not None and (body.from_ is not None or body.to is not None):
        raise validation("year is shorthand for a calendar-year range; give it or give from/to, not both",
                         path="year", reason="range_conflict")

    key = request.headers.get(idempotency.KEY_HEADER)
    if not key:  # pragma: no cover - the middleware refuses these before a route is reached
        raise idempotency.missing_key()
    digest = idempotency.request_hash(method=request.method, path=request.url.path,
                                      body=idempotency.body_for_hash(raw), caller=er.principal.caller,
                                      entity_id=er.entity_id)
    request_id = api_errors.request_id_of(request)
    durable_id = new_id("rq")
    written: dict[str, object] = {}

    def also_record(ctx, result: ExportResult) -> None:
        """Build, check and record the reply, inside the export's own write transaction."""
        payload = idempotency.validate(_run_view(ctx.conn, er.entity_id, result), schemas.ExportRun)
        record_request(ctx, caller=er.principal.caller, intent_key=key, method=request.method,
                       path=request.url.path, request_hash=digest, http_status=201, response=payload,
                       operation_id=result.operation_id, id=durable_id)
        written["payload"] = payload

    def work():
        """Run the replay check and the export off the event loop.  Returns a stored reply to replay, or None."""
        with er.state.registry.exporting(er.entity_id):
            # The replay check reads the entity's request log, as creation does (``routes/service.py``).  The
            # export's write transaction belongs to ``export_book``, so the check cannot run inside it the way
            # ``execute_once`` does.  The per-entity export lock stops two identical retries both passing it.
            with er.reader() as conn:
                existing = check_idempotency(conn, er.entity_id, caller=er.principal.caller, intent_key=key,
                                             request_hash=digest)
            if existing is not None:
                return existing
            try:
                export_book(er.handle.path, er.config.export_dir_for(er.entity_id), year=body.year,
                            start=body.from_, end=body.to, overwrite=body.overwrite,
                            actor=er.principal.actor, request_id=durable_id, also_record=also_record)
            except UnrenderableContent as exc:
                # Book content the journal format cannot carry, or an empty range.  Both are about what was
                # asked for, not failures of this service, so they are the caller's 422.
                raise validation(str(exc), path=exc.path or "range", reason="unrenderable") from None
            except ExportBlocked as exc:
                # The service's own output failed its own verifier.  Nothing was published or recorded, and
                # the caller can change nothing about it, so it is a 500.
                raise HaddonError(INTERNAL_ERROR, "the exported journal did not pass hledger check --strict, "
                                                  "so nothing was published or recorded",
                                  details=[{"reason": "export_blocked",
                                            "message": str(exc.error.stderr)[:500]}]) from exc
            except ArtifactMismatch as exc:
                raise HaddonError(INTERNAL_ERROR, "the published files do not match the bytes that were "
                                                  "verified, so nothing was recorded",
                                  details=[{"reason": "artifact_mismatch", "message": str(exc)[:500]}]) from exc
        return None

    existing = await run_in_threadpool(work)
    if existing is not None:
        return idempotency.replay_response(
            existing.http_status, idempotency.validate(existing.response, schemas.ExportRun), request_id)
    return JSONResponse(status_code=201, content=written["payload"],
                        headers={api_errors.REQUEST_ID_HEADER: request_id})


@router.get("/exports/{export_id}/artifacts/{artifact}", response_class=Response,
            responses={200: {"content": {"text/plain": {"schema": {"type": "string", "format": "binary"}}},
                             "description": "one published journal file, re-hashed against what was recorded"},
                       **schemas.ERRORS},
            summary="One file this export published, verified against the digest in the export row")
def export_artifact(export_id: str, artifact: str, er: EntityRequest = Depends(entity_request),
                    disposition: str = Query("attachment", pattern="^(attachment|inline)$",
                                             description="a journal is plain text, so both are honoured")
                    ) -> Response:
    """Return one published journal file, the form of these books other tools can read.

    The name is a key into the export's immutable artifact list, not a path.  A name the export did not record
    is a 404 before any path is built from it, and the file is read only from under this entity's configured
    export root.  It is re-hashed against the ``sha256`` the row recorded before any byte is returned.  That
    digest is the claim that these bytes passed the pinned verifier, so a file that no longer matches is refused
    rather than served with a warning (:mod:`haddon.api.exported`).

    An export published elsewhere, for example by ``bin/haddon-export`` into an operator's own directory, is
    recorded here but its bytes are not served: they are not under the root this service serves from.
    """
    with er.reader() as conn:
        row = require_row(conn.execute(sa.select(t.exports).where(
            t.exports.c.entity_id == er.entity_id, t.exports.c.id == export_id)).first(), "export", export_id)
        recorded = exported.artifact_named(views.export(row, er.entity_id)["artifacts"], artifact)
        if recorded is None:
            raise HaddonError(NOT_FOUND, f"export {export_id!r} recorded no file called {artifact!r}",
                              details=[{"path": "artifact", "value": artifact, "reason": "unknown_artifact"}])
        snapshot = require_row(conn.execute(sa.select(t.snapshots).where(
            t.snapshots.c.entity_id == er.entity_id, t.snapshots.c.id == row.snapshot_id)).first(),
            "snapshot", row.snapshot_id)
    manifest = views.snapshot(snapshot, er.entity_id)["manifest"]
    root = None if er.config.export_root is None else er.config.export_dir_for(er.entity_id)
    content = exported.load(out_root=root, ledger_dir=exported.ledger_dir_of(manifest), artifact=recorded,
                            inline=disposition == "inline")
    return Response(content=content.data, media_type=content.media_type, headers={
        "Content-Disposition": content.disposition,
        "X-Content-Type-Options": "nosniff",
        # The bytes were just verified against a digest on an immutable row, so this address can never
        # legitimately answer different bytes.
        "ETag": f'"{recorded["sha256"]}"',
        "Cache-Control": "private, max-age=3600",
    })
