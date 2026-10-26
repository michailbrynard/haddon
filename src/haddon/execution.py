"""Run one durable command intent, independent of its transport.

HTTP, an agent-tool adapter, and a local automation entry point all need the same write transaction,
idempotency check, request-linked audit context, and durable result.  This module owns that application boundary;
``repository.requests`` remains the persistence implementation beneath it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from haddon.domain.errors import validation
from haddon.repository.ctx import WriteCtx, new_id
from haddon.repository.requests import check_idempotency, record_request
from haddon.repository.transaction import write_transaction


@dataclass(frozen=True)
class Execution:
    status: int
    response: Any
    request_id: str
    replayed: bool


def execute_once(engine, *, entity_id: str, caller: str, actor: str, intent_key: str, method: str, path: str,
                 request_hash: str, work: Callable[[WriteCtx], tuple[int, Any]]) -> Execution:
    """Run ``work`` and durably record its result exactly once for this caller and intent key.

    The preallocated request id is placed on the write context, so every audit event written by the command links
    to the request row that commits beside it.  The foreign key is deferred specifically to permit the event to
    be written before the response is known.
    """
    if not isinstance(intent_key, str) or not intent_key:
        raise validation("intent_key is required", path="intent_key", reason="required")
    durable_id = new_id("rq")
    with write_transaction(engine) as conn:
        existing = check_idempotency(conn, entity_id, caller=caller, intent_key=intent_key,
                                     request_hash=request_hash)
        if existing is not None:
            return Execution(existing.http_status, existing.response, existing.id, True)
        ctx = WriteCtx.open(conn, actor, request_id=durable_id)
        if ctx.entity_id != entity_id:
            raise validation("request entity does not match the database entity", path="entity_id",
                             reason="entity_mismatch", value=entity_id)
        status, response = work(ctx)
        record_request(ctx, caller=caller, intent_key=intent_key, method=method, path=path,
                       request_hash=request_hash, http_status=status, response=response, id=durable_id)
    return Execution(status, response, durable_id, False)
