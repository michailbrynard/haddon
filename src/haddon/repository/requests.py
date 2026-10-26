"""Durable request results (idempotency) and operations (durable jobs).

Requests are unique on ``(entity_id, caller, intent_key)``.  The same key with the same hash replays the stored
result.  The same key with a different hash is ``409 idempotency_conflict``.  The request row commits together with
the mutation and its events.

Operations move ``queued -> running -> succeeded|failed``, and a queued operation may fail without running.
Terminal results are immutable (trigger ``operation_state``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.errors import IDEMPOTENCY_CONFLICT, IMMUTABLE, STALE_VERSION, HaddonError, not_found, validation
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx

OP_STATES = ("queued", "running", "succeeded", "failed")
TERMINAL = frozenset({"succeeded", "failed"})
ALLOWED_TRANSITIONS = {("queued", "running"), ("running", "running"), ("running", "succeeded"), ("running", "failed"),
                       ("queued", "failed")}


@dataclass(frozen=True)
class Request:
    id: str
    caller: str
    intent_key: str
    method: str
    path: str
    request_hash: str
    http_status: int
    response: Any
    operation_id: str | None


@dataclass(frozen=True)
class Operation:
    id: str
    kind: str
    state: str
    intent: Any
    counts: Any
    errors: Any
    result: Any
    lease_until: str | None
    attempts: int
    version: int


def find_request(conn: Connection, entity_id: str, *, caller: str, intent_key: str) -> Request | None:
    r = conn.execute(sa.select(t.requests).where(t.requests.c.entity_id == entity_id, t.requests.c.caller == caller,
                                                 t.requests.c.intent_key == intent_key)).first()
    return None if r is None else Request(r.id, r.caller, r.intent_key, r.method, r.path, r.request_hash, r.http_status,
                                          json.loads(r.response), r.operation_id)


def check_idempotency(conn: Connection, entity_id: str, *, caller: str, intent_key: str, request_hash: str) -> Request | None:
    """Return None for a new intent and the stored request for a replay.  Raise 409 for a reused key with a new hash."""
    existing = find_request(conn, entity_id, caller=caller, intent_key=intent_key)
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise HaddonError(IDEMPOTENCY_CONFLICT, "Idempotency-Key reused with a different request",
                          details=[{"intent_key": intent_key, "request_id": existing.id}])
    return existing


def record_request(ctx: WriteCtx, *, caller: str, intent_key: str, method: str, path: str, request_hash: str,
                   http_status: int, response: Any, operation_id: str | None = None, id: str | None = None) -> Request:
    for p, v in (("caller", caller), ("intent_key", intent_key), ("method", method), ("path", path), ("request_hash", request_hash)):
        if not isinstance(v, str) or not v:
            raise validation(f"{p} is required", path=p, reason="required")
    if not isinstance(http_status, int) or not 100 <= http_status <= 599:
        raise validation("http_status must be an HTTP status", path="http_status", reason="range")
    if find_request(ctx.conn, ctx.entity_id, caller=caller, intent_key=intent_key) is not None:
        raise HaddonError(IDEMPOTENCY_CONFLICT, "a result for this intent key is already recorded",
                          details=[{"caller": caller, "intent_key": intent_key}])
    if operation_id is not None and get_operation(ctx.conn, ctx.entity_id, operation_id) is None:
        raise not_found("operation", operation_id)  # pre-validated so an unknown id is a 404, not a tainting FK failure
    rid = ctx.insert(t.requests, id, "rq", caller=caller, intent_key=intent_key, method=method, path=path, request_hash=request_hash,
                     http_status=http_status, response=json.dumps(response, sort_keys=True), operation_id=operation_id)
    return find_request(ctx.conn, ctx.entity_id, caller=caller, intent_key=intent_key) or Request(rid, caller, intent_key, method, path, request_hash, http_status, response, operation_id)


def _op(r) -> Operation:
    return Operation(r.id, r.kind, r.state, json.loads(r.intent), json.loads(r.counts), json.loads(r.errors),
                     None if r.result is None else json.loads(r.result), r.lease_until, r.attempts, r.version)


def get_operation(conn: Connection, entity_id: str, op_id: str) -> Operation | None:
    r = conn.execute(sa.select(t.operations).where(t.operations.c.entity_id == entity_id, t.operations.c.id == op_id)).first()
    return None if r is None else _op(r)


def create_operation(ctx: WriteCtx, *, kind: str, intent: Any, id: str | None = None) -> Operation:
    if not isinstance(kind, str) or not kind:
        raise validation("kind is required", path="kind", reason="required")
    oid = ctx.insert(t.operations, id, "op", kind=kind, state="queued", intent=json.dumps(intent, sort_keys=True), counts="{}",
                     errors="[]", result=None, lease_until=None, attempts=0, version=1)
    return get_operation(ctx.conn, ctx.entity_id, oid)  # type: ignore[return-value]


def transition_operation(ctx: WriteCtx, op_id: str, *, expected_version: int, state: str, counts: Any = None, errors: Any = None,
                         result: Any = None, lease_until: str | None = None) -> Operation:
    op = get_operation(ctx.conn, ctx.entity_id, op_id)
    if op is None:
        raise not_found("operation", op_id)
    if state not in OP_STATES:
        raise validation(f"state {state!r} invalid", path="state", reason="enum")
    if op.state in TERMINAL:
        raise HaddonError(IMMUTABLE, f"operation {op_id!r} is {op.state}; terminal results are immutable", details=[{"id": op_id, "state": op.state}])
    if op.version != expected_version:
        raise HaddonError(STALE_VERSION, "operation changed", details=[{"id": op_id, "current_version": op.version}])
    if (op.state, state) not in ALLOWED_TRANSITIONS:
        raise HaddonError(IMMUTABLE, f"operation cannot go {op.state} -> {state}", details=[{"id": op_id, "from": op.state, "to": state}])
    values: dict[str, Any] = {"state": state, "version": op.version + 1, "lease_until": lease_until}
    if state == "running" and op.state == "queued":
        values["attempts"] = op.attempts + 1
    if counts is not None:
        values["counts"] = json.dumps(counts, sort_keys=True)
    if errors is not None:
        values["errors"] = json.dumps(errors, sort_keys=True)
    if result is not None:
        values["result"] = json.dumps(result, sort_keys=True)
    ctx.execute(sa.update(t.operations).where(t.operations.c.id == op_id, t.operations.c.version == op.version).values(**values))
    return get_operation(ctx.conn, ctx.entity_id, op_id)  # type: ignore[return-value]
