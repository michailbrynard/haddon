"""The write context every repository write function takes.

A ``WriteCtx`` holds the Core connection of one ``write_transaction``, the entity this database file belongs to, the
acting principal and the transaction timestamp.

``WriteCtx.execute`` is the single choke point for statements.  It turns any DBAPI error into a ``HaddonError``
through the trigger-text fallback mapping (D6 fallback).  By then ``make_engine``'s ``handle_error`` hook has
already tainted the transaction, so ``write_transaction`` will not commit it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from haddon.domain.errors import IMMUTABLE, INTERNAL_ERROR, HaddonError, validation
from haddon.repository import tables as t
from haddon.repository.errors import wrap_db_error


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def entity_of(conn: Connection) -> str:
    """The one entity this file holds (``entities.singleton`` guarantees at most one row)."""
    row = conn.execute(sa.select(t.entities.c.id)).first()
    if row is None:
        raise HaddonError(INTERNAL_ERROR, "entity not provisioned: this database has no entities row")
    return row[0]


def check_actor(actor: object, *, path: str = "created_by") -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise validation(f"{path}: an acting principal is required", path=path, reason="actor_required")
    return actor


@dataclass
class WriteCtx:
    conn: Connection
    entity_id: str
    actor: str
    now: str
    request_id: str | None = None

    @classmethod
    def open(cls, conn: Connection, actor: str, *, now: str | None = None,
             request_id: str | None = None) -> "WriteCtx":
        return cls(conn=conn, entity_id=entity_of(conn), actor=check_actor(actor), now=now or utc_now(),
                   request_id=request_id)

    def execute(self, stmt, params=None):
        try:
            return self.conn.execute(stmt, params) if params is not None else self.conn.execute(stmt)
        except DBAPIError as exc:
            raise wrap_db_error(exc) from exc

    def common(self, id_: str) -> dict[str, str]:
        return {"id": id_, "entity_id": self.entity_id, "created_at": self.now, "created_by": self.actor}

    def insert(self, table: sa.Table, id_: str | None, prefix: str, **values) -> str:
        id_ = id_ if id_ is not None else new_id(prefix)
        if not isinstance(id_, str) or not id_:
            raise validation("id must be a non-empty string", path="id", reason="id_invalid")
        # Pre-validate the key (D6).  A caller-supplied id that already exists must be a clean 409, not a primary-key
        # or `exists` trigger refusal that taints the transaction.  The DB constraint stays as the fallback.
        if self.conn.execute(sa.select(table.c.id).where(table.c.id == id_)).first() is not None:
            raise HaddonError(IMMUTABLE, f"{table.name} {id_!r} already exists", details=[{"path": "id", "table": table.name, "id": id_}])
        self.execute(sa.insert(table).values(**self.common(id_), **values))
        return id_
