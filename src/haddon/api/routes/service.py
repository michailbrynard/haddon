"""Service-level routes: liveness, readiness, and the entities this caller can see.

``/healthz`` answers whether the process is up and touches nothing.  ``/readyz`` answers whether it can do its
job, which has four parts:

* Migrations: every configured file is at the Alembic head.  Serving reads off a database one revision behind
  the code is how a missing column becomes a 500 an hour later.
* The writer: one ``BEGIN IMMEDIATE`` transaction opens, passes the connection guard and commits.  This is the
  only check that proves the file is writable, WAL is on and the pragmas the trigger suite needs are set.
* The pinned verifier: the vendored hledger binary is present and is the pinned build, by sha256 and by
  ``--version``.  Export and the oracle are useless without it, and close time is too late to find out.
* The cursor keyring: the key cursors are signed with comes from the configuration.  A service with no
  configured key invents one per process, so every restart silently invalidates every open pagination cursor.

Neither health route requires a credential, since a liveness probe that can be locked out is not one.  They
disclose no entity data.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from haddon import commands
from haddon.api import errors as api_errors
from haddon.api import idempotency, schemas
from haddon.api.config import check_entity_id
from haddon.api.deps import ApiState, current_principal, state_of
from haddon.api.registry import EntityFileMismatch, entity_id_in, entity_summary
from haddon.api.security import Principal
from haddon.api.wire import parse_body
from haddon.db.connection import make_engine
from haddon.db.migrate import current_revision, head_revision, upgrade
from haddon.domain.errors import IMMUTABLE, INTERNAL_ERROR, HaddonError
from haddon.importing.hledger import HledgerBinaryError
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx, new_id
from haddon.repository.requests import check_idempotency, record_request
from haddon.repository.transaction import write_transaction

API_VERSION = "v1"

router = APIRouter(tags=["service"])


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def as_json(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def check_migrations(state: ApiState) -> list[Check]:
    head = head_revision()
    out = []
    for eid, path in sorted(state.config.entities.items()):
        try:
            current = current_revision(path)
        except Exception as exc:  # noqa: BLE001 - readiness reports the reason, it does not raise
            out.append(Check(f"migrations:{eid}", False, f"{type(exc).__name__}: {exc}"))
            continue
        out.append(Check(f"migrations:{eid}", current == head, f"at {current}, head {head}"))
    return out


def check_writer(state: ApiState) -> list[Check]:
    out = []
    for eid in state.registry.configured_ids():
        try:
            handle = state.registry.handle(eid)
            with write_transaction(handle.engine) as conn:
                conn.execute(sa.select(sa.func.count()).select_from(t.entities))
            out.append(Check(f"writer:{eid}", True, "BEGIN IMMEDIATE committed on a guarded connection"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check(f"writer:{eid}", False, f"{type(exc).__name__}: {exc}"))
    return out


def check_cursor_keys(state: ApiState) -> Check:
    keys = state.config.cursor_keys
    if not keys.durable:
        return Check("cursors", False, "no cursor key is configured: this process signs with a key it invented, "
                                       "so restarting it invalidates every open cursor. Set cursor_keys.")
    listed = ", ".join(k.id for k in keys.keys)
    return Check("cursors", True, f"signing with {keys.active.id!r}; verifying {len(keys.keys)}: {listed}")


def check_verifier(state: ApiState) -> Check:
    try:
        binary = state.verifier()
    except HledgerBinaryError as exc:
        return Check("verifier", False, str(exc))
    return Check("verifier", True, f"{binary.version} sha256={binary.sha256[:12]}")


@router.get("/healthz", response_model=schemas.Health, summary="Liveness")
def healthz() -> dict:
    return {"status": "ok", "service": "haddon", "api_version": API_VERSION}


@router.get("/readyz", response_model=schemas.Readiness, responses={503: {"model": schemas.Readiness}},
            summary="Readiness: migrations, the writer, the pinned verifier and the cursor keyring")
def readyz(request: Request) -> JSONResponse:
    state = state_of(request)
    checks = [*check_migrations(state), *check_writer(state), check_verifier(state), check_cursor_keys(state)]
    ready = all(c.ok for c in checks)
    body = {"status": "ready" if ready else "not_ready", "checks": [c.as_json() for c in checks]}
    return JSONResponse(status_code=200 if ready else 503, content=body)


def _summary(state: ApiState, entity_id: str) -> dict | None:
    try:
        handle = state.registry.handle(entity_id)
    except EntityFileMismatch:
        return None  # readyz reports it; the listing leaves a misconfigured file out
    with handle.reader() as conn:
        return entity_summary(conn, verifier_ok=state.verifier_ok())


@router.get("/v1/entities", response_model=schemas.EntityList, responses=schemas.ERRORS,
            summary="The entities this credential can see")
def list_entities(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    state = state_of(request)
    visible = principal.visible(state.registry.configured_ids())
    items = [s for s in (_summary(state, eid) for eid in visible) if s is not None]
    return {"items": items, "next_cursor": None, "snapshot_seq": None}


# Creating an entity.  One database file is one entity, so this means creating a file, migrating it,
# provisioning the books inside it and telling the deployment it exists.  The four steps are useless apart, and
# without this route they needed `bin/haddon-init` plus an operator editing the configuration by hand.
#
# The order makes a failure safe.  The file is created, migrated and provisioned before the entity map is
# touched, and the map is written durably before this process starts serving the entity.  A crash at any point
# leaves the service serving exactly what it served before, plus at worst an unregistered file that the next
# attempt finishes.  Nothing half-registered is ever addressable.

def _created(state: ApiState, entity_id: str, principal: Principal, *, intent_key: str, digest: str,
             request_id: str) -> JSONResponse:
    """Answer a creation for an entity already served: the caller's own earlier reply, or a refusal.

    This is the idempotency check for a creation.  It reads the entity's request log, the only durable record of
    the reply that was sent.  Same caller, key and request: the original 201 replayed.  Same key, different
    request: 409 from ``check_idempotency``.  A different key: the entity exists, and this is a second attempt to
    create it.
    """
    try:
        handle = state.registry.handle(entity_id)
    except EntityFileMismatch as exc:
        # Configuration is wrong, not the request.  Same answer as ``deps.entity_request``, so a misconfigured
        # file does not make this the one route that answers with a traceback.
        raise HaddonError(INTERNAL_ERROR, f"entity {entity_id!r} is misconfigured and cannot be served") from exc
    with handle.reader() as conn:
        existing = check_idempotency(conn, entity_id, caller=principal.caller, intent_key=intent_key,
                                     request_hash=digest)
    if existing is None:
        raise HaddonError(IMMUTABLE, f"entity {entity_id!r} already exists",
                          details=[{"path": "entity_id", "value": entity_id, "reason": "entity_exists"}])
    return idempotency.replay_response(existing.http_status,
                                       idempotency.validate(existing.response, schemas.EntitySummary), request_id)


@router.post("/v1/entities", status_code=201, operation_id="initialize_entity",
             responses={201: {"model": schemas.EntitySummary}, **schemas.ERRORS},
             openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                 "schema": schemas.EntityCreationSubmission.model_json_schema()}}}},
             summary="Create an entity: its database file, its schema, its commodities and its opening chart")
async def create_entity(request: Request, principal: Principal = Depends(current_principal)):
    """Create a new set of books and serve them from this process immediately.

    The reply is one ``EntitySummary``, the same object ``GET /v1/entities`` lists and
    ``GET /v1/entities/{entity_id}`` returns, so the caller can follow ``links.capabilities`` straight into the
    new book.

    Creating an entity that is already served is not an update.  The id is its permanent address and the books
    behind it are somebody's accounts, so a second creation is refused rather than merged.  A retry of the same
    creation (same credential, same ``Idempotency-Key``, same body) replays the original 201, so it is safe to
    retry over a connection that dropped after the books were written.
    """
    state = state_of(request)
    refusal = state.config.provisioning_refusal()
    if refusal is not None:
        raise refusal
    raw = await request.body()
    body = parse_body(raw, schemas.EntityCreationSubmission)
    entity_id = check_entity_id(body.entity_id)
    # A credential may not create an entity it could not address afterwards; otherwise creation would be a way
    # for a scoped credential to reach past its scope.
    principal.require(entity_id)

    intent_key = request.headers.get(idempotency.KEY_HEADER)
    if not intent_key:  # pragma: no cover - the middleware refuses these before a route is reached
        raise idempotency.missing_key()
    digest = idempotency.request_hash(method=request.method, path=request.url.path,
                                      body=idempotency.body_for_hash(raw), caller=principal.caller,
                                      entity_id=entity_id)
    request_id = api_errors.request_id_of(request)
    # One creation at a time, from "is this id taken" through to "this process serves it" (see
    # ``Registry.provisioning``).
    with state.registry.provisioning():
        if state.config.path_for(entity_id) is not None:
            return _created(state, entity_id, principal, intent_key=intent_key, digest=digest,
                            request_id=request_id)
        payload = _provision(state, entity_id, principal, body=body, intent_key=intent_key, digest=digest,
                             path=request.url.path, method=request.method)
    return JSONResponse(status_code=201, content=payload, headers={api_errors.REQUEST_ID_HEADER: request_id})


def _provision(state: ApiState, entity_id: str, principal: Principal, *, body, intent_key: str, digest: str,
               path: str, method: str) -> dict:
    """Make the file, migrate it, write the books, then register it.  Call under ``provisioning()``."""
    db = state.config.new_entity_path(entity_id)
    if db.exists():
        engine = make_engine(db, immediate=False)
        try:
            occupied = entity_id_in(engine) is not None
        finally:
            engine.dispose()
        if occupied:
            # A file with books in it that the configuration does not mention needs an operator to look at it.
            # Provisioning over it would destroy what is there or, worse, add a second entity's rows to it.
            raise HaddonError(IMMUTABLE,
                              f"a database file for {entity_id!r} is already there and already holds an entity; "
                              f"it is not registered with this service, so somebody has to say what it is",
                              details=[{"path": "entity_id", "value": entity_id, "reason": "file_exists"}])

    upgrade(db)
    engine = make_engine(db)
    try:
        durable_id = new_id("rq")
        with write_transaction(engine) as conn:
            commands.initialize_entity(
                conn, actor=principal.actor, id=entity_id, name=body.legal_name, legal_form=body.legal_form,
                country=body.country, base_currency=body.base_currency, base_precision=body.base_precision,
                commodities=tuple((c.code, c.precision) for c in body.commodities),
                chart=commands.STARTER_CHART if body.starter_chart else (),
                tax_regime=body.tax_regime, reason=body.reason, request_id=durable_id)
            # Validated inside the transaction, like every other write: a reply that does not match the
            # published schema rolls the new books back.
            payload = idempotency.validate(entity_summary(conn, verifier_ok=state.verifier_ok()),
                                           schemas.EntitySummary)
            ctx = WriteCtx.open(conn, principal.actor, request_id=durable_id)
            record_request(ctx, caller=principal.caller, intent_key=intent_key, method=method, path=path,
                           request_hash=digest, http_status=201, response=payload, id=durable_id)
    finally:
        engine.dispose()
    state.registry.register(entity_id, db)
    return payload
