"""Append-only event log keyed by ``(entity_id, seq)``.  SQLite AUTOINCREMENT assigns ``seq``; callers never do.

Events inherit the durable request id from ``WriteCtx``, so a command run through the shared execution boundary
cannot forget to link its events to the request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.errors import validation
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    subject_kind: str
    subject_id: str
    before: dict | list | None
    after: dict | list | None
    reason: str | None
    request_id: str | None
    created_at: str
    created_by: str


def append_event(ctx: WriteCtx, *, kind: str, subject_kind: str, subject_id: str, before: Any = None,
                 after: Any = None, reason: str | None = None, request_id: str | None = None) -> int:
    """Append one immutable event and return its ``seq``.

    ``request_id`` may name a request row that is only inserted later in the same transaction.  The foreign key is
    deferred for exactly that case."""
    for path, v in (("kind", kind), ("subject_kind", subject_kind), ("subject_id", subject_id)):
        if not isinstance(v, str) or not v:
            raise validation(f"{path} is required", path=path, reason="required")
    request_id = ctx.request_id if request_id is None else request_id
    res = ctx.execute(sa.insert(t.events).values(
        entity_id=ctx.entity_id, created_at=ctx.now, created_by=ctx.actor, kind=kind, subject_kind=subject_kind,
        subject_id=subject_id, before=None if before is None else json.dumps(before, sort_keys=True),
        after=None if after is None else json.dumps(after, sort_keys=True), reason=reason,
        request_id=request_id))
    return int(res.inserted_primary_key[0])


def read_events(conn: Connection, entity_id: str, *, since: int = 0, limit: int = 100) -> list[Event]:
    if not 1 <= limit <= 500:
        raise validation("limit must be 1..500", path="limit", reason="range", value=limit)
    rows = conn.execute(sa.select(t.events).where(t.events.c.entity_id == entity_id, t.events.c.seq > since)
                        .order_by(t.events.c.seq).limit(limit))
    return [Event(r.seq, r.kind, r.subject_kind, r.subject_id, json.loads(r.before) if r.before else None,
                  json.loads(r.after) if r.after else None, r.reason, r.request_id, r.created_at, r.created_by) for r in rows]


def high_water(conn: Connection, entity_id: str) -> int:
    return conn.execute(sa.select(sa.func.coalesce(sa.func.max(t.events.c.seq), 0)).where(t.events.c.entity_id == entity_id)).scalar_one()
