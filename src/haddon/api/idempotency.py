"""``Idempotency-Key`` on every mutating verb, wired to the storage that already exists.

``haddon.repository.requests`` implements the durable side (``UNIQUE(entity_id, caller, intent_key)``, the hash
comparison, the 409), and this module does not reimplement it.  It adds the HTTP contract:

* Every mutating verb under ``/v1`` requires the header.  The check is middleware rather than a per-route
  dependency so that a mutating route added later cannot quietly be exempt.
* The hash binds canonical method, path, body, caller and entity.  The body is canonicalised as JSON, so a retry
  that re-serialises the same intent with different key order or whitespace still replays.  Anything that
  changes the intent changes the hash.
* Same key and same hash: the stored original status and body, with ``Idempotency-Replayed: true``.  Same key,
  different hash: ``409 idempotency_conflict``.  Both decisions are taken under the write lock
  (``BEGIN IMMEDIATE``), so two concurrent retries cannot both pass the check.
* ``record_request`` inserts the request result in the same transaction as the mutation, so either both the
  effect and the record of its reply are durable or neither is.
* The reply is validated against the model its route publishes, on the first pass and on every replay.  A write
  returns a ``JSONResponse`` built inside the transaction, which FastAPI does not check; before issue 0006 only
  the reads had their response model validated on the way out.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from pydantic import ValidationError as PydanticValidationError

from haddon.api import errors as api_errors
from haddon.api.deps import EntityRequest
from haddon.domain.canonical import canonical_json, content_hash
from haddon.domain.errors import INTERNAL_ERROR, HaddonError, validation
from haddon.execution import execute_once

KEY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
V1_PREFIX = "/v1/"


def missing_key() -> HaddonError:
    return validation(f"{KEY_HEADER} is required on every mutating request; persist one UUID per intent and "
                      f"reuse it across retries", path=KEY_HEADER, reason="idempotency_key_required")


def install(app) -> None:
    @app.middleware("http")
    async def _require_key(request: Request, call_next):
        request.state.request_id = f"req-{request.headers.get('X-Request-Id') or _token()}"
        if request.method in MUTATING and request.url.path.startswith(V1_PREFIX) \
                and not request.headers.get(KEY_HEADER):
            return api_errors.error_response(missing_key(), request.state.request_id)
        response = await call_next(request)
        response.headers.setdefault(api_errors.REQUEST_ID_HEADER, request.state.request_id)
        return response


def _token() -> str:
    import uuid
    return uuid.uuid4().hex


def body_for_hash(raw: bytes) -> Any:
    """Return the body in canonical form for hashing.

    Unparseable bytes still hash (as their hex), so the same-key/different-body comparison covers every request
    and a malformed retry cannot slip past the conflict check."""
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw": raw.hex()}


def request_hash(*, method: str, path: str, body: Any, caller: str, entity_id: str) -> str:
    return content_hash(method=method.upper(), path=path, body=json.loads(canonical_json(body)) if body is not None else None,
                        caller=caller, entity_id=entity_id)


def replay_response(status: int, payload: Any, request_id: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload,
                        headers={REPLAYED_HEADER: "true", api_errors.REQUEST_ID_HEADER: request_id})


def validate(payload: Any, model) -> Any:
    """Check a write's reply against the model the route publishes, on the way out.

    FastAPI validates a ``response_model`` for routes that return a value, but a write here returns a
    ``JSONResponse`` built inside the write transaction, which FastAPI does not check (issue 0006).  So it is
    checked here.  A field the schema does not declare, or a declared one that is missing, is a 500 from this
    service rather than an undocumented body a client learns to depend on.

    Replays are validated too.  A stored reply is read back from the request row, so a payload written under an
    older shape of the model surfaces here rather than at the caller.
    """
    if model is None:
        return payload
    try:
        return model.model_validate(payload).model_dump(mode="json")
    except PydanticValidationError as exc:
        raise HaddonError(INTERNAL_ERROR,
                          f"the reply does not match the published {model.__name__} schema",
                          details=[{"path": ".".join(str(part) for part in e.get("loc", ())),
                                    "reason": e.get("type", "validation")} for e in exc.errors()]) from None


def declared_model(request: Request, status: int):
    """Return the model the route publishes for this status, read off the route.

    Read rather than passed in, because a model handed to ``run`` by each write route would be a second
    declaration that drifts from the ``responses=`` the OpenAPI document is built from.  The status is the key
    because ``POST /documents`` answers 200 or 201 for the same request, depending on whether the bytes were
    already here, and declares a model for each.
    """
    route = request.scope.get("route")
    spec = (getattr(route, "responses", None) or {}).get(status)
    return None if not isinstance(spec, dict) else spec.get("model")


def run(er: EntityRequest, raw_body: bytes, work) -> JSONResponse:
    """Execute one mutating request exactly once, and check its reply against what the route publishes.

    ``work(ctx) -> (status, payload)`` runs inside the write transaction, and its result and the request row commit
    together.  A refusal raised by ``work`` propagates and the transaction rolls back, leaving no record, so a
    failed attempt never uses up the caller's intent key.

    The reply is checked against the model the route declares for its status inside the transaction.  A reply
    that does not match the published contract rolls the write back instead of committing an effect whose
    recorded reply is wrong.
    """
    key = er.request.headers.get(KEY_HEADER)
    if not key:  # pragma: no cover - the middleware refuses these before a route is reached
        raise missing_key()
    method = er.request.method
    path = er.request.url.path
    digest = request_hash(method=method, path=path, body=body_for_hash(raw_body), caller=er.principal.caller,
                          entity_id=er.entity_id)
    request_id = api_errors.request_id_of(er.request)
    def checked(ctx):
        status, payload = work(ctx)
        return status, validate(payload, declared_model(er.request, status))

    executed = execute_once(er.handle.engine, entity_id=er.entity_id, caller=er.principal.caller,
                            actor=er.principal.actor, intent_key=key, method=method, path=path,
                            request_hash=digest, work=checked)
    if executed.replayed:
        return replay_response(executed.status,
                               validate(executed.response, declared_model(er.request, executed.status)),
                               request_id)
    return JSONResponse(status_code=executed.status, content=executed.response,
                        headers={api_errors.REQUEST_ID_HEADER: request_id})
