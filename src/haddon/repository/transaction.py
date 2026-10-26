"""The one write-transaction shape every repository uses (spec "Accounting and transaction contract").

``write_transaction(engine)`` opens a guarded connection, runs the connection guard (review D7), issues
``BEGIN IMMEDIATE`` and yields a SQLAlchemy Core connection.  It rolls back on any exception, including one raised
after entries were sealed.

It also refuses to commit a transaction in which any database error occurred.  SQLite's RAISE(ABORT) aborts one
statement but leaves the transaction open, so without this a caller could catch the refusal and commit anyway.  The
spec and the review both call this out.

``after_commit`` callbacks (notifications, publishing) run only once the commit has returned.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Connection, Engine

from haddon.db.connection import assert_connection_safe

TAINT_KEY = "haddon_tainted"


class TransactionTainted(RuntimeError):
    """A database error was raised inside the transaction and swallowed; committing is refused."""


class TransactionAlreadyEnded(RuntimeError):
    """User code committed or rolled back the transaction itself; the repository owns that."""


def _dbapi(conn: Connection):
    return conn.connection.driver_connection


@contextmanager
def write_transaction(engine: Engine, *, after_commit: Callable[[], None] | None = None) -> Iterator[Connection]:
    with engine.connect() as conn:
        dbapi = _dbapi(conn)
        assert_connection_safe(dbapi)  # D7: every connection, every write
        conn.info[TAINT_KEY] = False
        trans = conn.begin()  # the engine's `begin` event emits BEGIN IMMEDIATE
        committed = False
        try:
            yield conn
            if not dbapi.in_transaction:
                raise TransactionAlreadyEnded("transaction was ended inside write_transaction")
            if conn.info.get(TAINT_KEY):
                raise TransactionTainted("a database error occurred inside the transaction; refusing to commit")
            trans.commit()  # may itself fail (deferred FKs are checked here)
            committed = True
        finally:
            if not committed:
                # Roll back whatever is left, whatever the reason (including a failed COMMIT).
                try:
                    if trans.is_active:
                        trans.rollback()
                    elif dbapi.in_transaction:
                        dbapi.rollback()
                except Exception:  # pragma: no cover - rollback of a dead connection
                    pass
    if after_commit is not None:
        after_commit()
