"""The entity registry: one database file, one entity, one engine.

``entities.singleton`` guarantees a file holds at most one entity row.  The registry makes that the API's
addressing rule: ``/v1/entities/{entity_id}`` resolves to the configured file, and the file is refused at open
time if its ``entities`` row names a different id.  A caller cannot reach entity B's rows through entity A's
path, and every id in a request is resolved within the entity that owns the connection.  Repository reads all
filter on ``(entity_id, id)``, so an id that exists in another file is simply absent.

Each entity gets two engines over the same file, created once and reused.  The writer begins
``BEGIN IMMEDIATE``, and ``write_transaction`` runs every write through it.  The reader begins a deferred
transaction.  Under WAL that is a consistent snapshot for the length of a request, and it does not take the
write lock, so concurrent reads do not queue behind each other and a slow report does not block the writer.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from haddon.api.config import ApiConfig, register_entity
from haddon.api.wire import entity_base
from haddon.db.connection import make_engine
from haddon.domain.errors import IMMUTABLE, INTERNAL_ERROR, HaddonError, not_found
from haddon.repository import tables as t
from haddon.repository.closing import get_lock
from haddon.repository.provisioning import all_commodities, read_entity


class EntityFileMismatch(RuntimeError):
    """The database file configured for an entity id holds a different entity (or none at all)."""


@dataclass(frozen=True)
class EntityHandle:
    """One resolved entity: its id, its file, the engine that owns writes and the one reads go through."""

    entity_id: str
    path: Path
    engine: Engine        # BEGIN IMMEDIATE; the only engine write_transaction may be given
    read_engine: Engine   # deferred; never takes the write lock

    @contextmanager
    def reader(self) -> Iterator[Connection]:
        with self.read_engine.connect() as conn:
            yield conn


def entity_id_in(engine: Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(sa.select(t.entities.c.id)).first()
    return None if row is None else row[0]


class Registry:
    """Lazily-opened, process-wide engines for the configured entities."""

    def __init__(self, config: ApiConfig) -> None:
        self.config = config
        self._engines: dict[str, tuple[Engine, Engine]] = {}
        self._lock = threading.Lock()
        #: Held for the whole of one provisioning, which calls ``handle`` and so cannot run under ``_lock``.
        #: Two concurrent creations of the same id are serialised here instead of racing to make the same file.
        self._provisioning = threading.Lock()
        #: One per entity, created on first use under ``_lock``: one export at a time per book.
        self._exporting: dict[str, threading.Lock] = {}

    @contextmanager
    def exporting(self, entity_id: str) -> Iterator[None]:
        """Hold the per-entity export lock for the whole of one export.

        An export publishes into a directory it manages exclusively: it deletes the managed files already there
        and moves its own in.  Two at once would be two writers to one directory, and the loser would re-hash
        the winner's bytes and fail ``ArtifactMismatch``.  The lock is per entity, not global, because two books
        export into different directories.  It also makes the route's idempotency pre-check safe, since that
        read and the write it guards are not one transaction.
        """
        with self._lock:
            lock = self._exporting.setdefault(entity_id, threading.Lock())
        with lock:
            yield

    def configured_ids(self) -> list[str]:
        return sorted(self.config.entities)

    @contextmanager
    def provisioning(self) -> Iterator[None]:
        """Hold the provisioning lock for one entity creation, from "is this id taken" to "this process serves it".

        Locking only the registration is not enough.  The steps in between (creating the file, migrating it,
        writing the books) are what two concurrent creations of the same id would interleave, and the loser
        would find the winner's entity row in what it thought was its own empty file.  Creations are rare and
        take well under a second, so a finer lock is not worth it.
        """
        with self._provisioning:
            yield

    def register(self, entity_id: str, path: Path) -> EntityHandle:
        """Add a newly provisioned entity to the served map and open it, or say why it cannot be added.

        Call inside :meth:`provisioning`.  Registration is deliberately the last step: the file is created,
        migrated and provisioned first, so a failure part way through leaves the service serving exactly what it
        served before, not advertising an entity it cannot open.

        Both maps are updated together, the durable one in the configuration file first and then this process's.
        The file is checked before either, so an entity that would fail ``EntityFileMismatch`` at the first
        request fails here instead, before anything is written down.
        """
        if entity_id in self.config.entities:
            raise HaddonError(IMMUTABLE, f"entity {entity_id!r} is already served",
                              details=[{"path": "entity_id", "value": entity_id, "reason": "entity_exists"}])
        engine = make_engine(path, immediate=False)
        try:
            found = entity_id_in(engine)
        finally:
            engine.dispose()
        if found != entity_id:
            raise EntityFileMismatch(
                f"{path} holds entity {found!r}, not {entity_id!r}: one database file is one entity")
        register_entity(self.config, entity_id, path)
        with self._lock:
            self.config.entities[entity_id] = path
        return self.handle(entity_id)

    def handle(self, entity_id: str) -> EntityHandle:
        """Resolve an entity id to its handle, or raise 404.  Refuses a file that holds a different entity."""
        path = self.config.path_for(entity_id)
        if path is None:
            raise not_found("entity", entity_id)
        with self._lock:
            pair = self._engines.get(entity_id)
            if pair is None:
                writer, reader = make_engine(path), make_engine(path, immediate=False)
                found = entity_id_in(reader)
                if found != entity_id:
                    writer.dispose()
                    reader.dispose()
                    raise EntityFileMismatch(
                        f"{path} holds entity {found!r}, not {entity_id!r}: one database file is one entity")
                pair = self._engines[entity_id] = (writer, reader)
        return EntityHandle(entity_id=entity_id, path=path, engine=pair[0], read_engine=pair[1])

    def open_all(self) -> dict[str, str]:
        """Open and verify every configured entity.  Returns id -> 'ok' or the reason it is unusable."""
        report: dict[str, str] = {}
        for eid in self.configured_ids():
            try:
                self.handle(eid)
                report[eid] = "ok"
            except (EntityFileMismatch, HaddonError, OSError, sa.exc.SQLAlchemyError) as exc:
                report[eid] = f"{type(exc).__name__}: {exc}"
        return report

    def dispose(self) -> None:
        with self._lock:
            for writer, reader in self._engines.values():
                writer.dispose()
                reader.dispose()
            self._engines.clear()


def require_row(row, kind: str, id_: str):
    """Return ``row``, or raise 404 if the entity's own file does not hold it.

    Used for every ``/{id}`` lookup, so an id belonging to another entity is a plain not-found, never a leak."""
    if row is None:
        raise not_found(kind, id_)
    return row


def entity_context(conn: Connection, entity_id: str) -> dict:
    """Return the entity's id, legal name, base currency, legal form and country for a response."""
    row = conn.execute(sa.select(t.entities.c.id, t.entities.c.name, t.entities.c.base_currency,
                                 t.entities.c.legal_form, t.entities.c.country)).first()
    if row is None or row[0] != entity_id:
        raise HaddonError(INTERNAL_ERROR, "the entity file no longer holds the entity it was opened for")
    return {"entity_id": row[0], "legal_name": row[1], "base_currency": row[2],
            "legal_form": row[3], "country": row[4]}


def entity_summary(conn: Connection, *, verifier_ok: bool) -> dict:
    """Render one entity the way the listing, the entity resource and a fresh creation all report it.

    The connection is a parameter so that creation can answer from inside the transaction that made the book
    without a second rendering of the same fields that would drift.  ``verifier_ok`` is passed in, and cached on
    the state, because it is a fact about the process, not about this connection.
    """
    entity = read_entity(conn)
    has_entries = conn.execute(sa.select(t.entries.c.id)
                               .where(t.entries.c.entity_id == entity.id,
                                      t.entries.c.status == "posted").limit(1)).first() is not None
    lock = get_lock(conn, entity.id)
    commodities = sorted(all_commodities(conn, entity.id))
    base = entity_base(entity.id)
    return {"entity_id": entity.id, "legal_name": entity.name, "legal_form": entity.legal_form,
            "country": entity.country, "base_currency": entity.base_currency, "commodities": commodities,
            "lock_through": lock.through, "lock_version": lock.version, "lock_close_id": lock.close_id,
            # A live close needs posted movement to close and a verifier to prove the result; both are checked,
            # not assumed.
            "live_close_eligible": has_entries and verifier_ok,
            "links": {"self": base, "capabilities": f"{base}/capabilities", "events": f"{base}/events"}}
