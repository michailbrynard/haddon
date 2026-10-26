"""SQLite connections for Haddon: mandatory per-connection settings and the connection guard (review D1, D7).

Every connection the service uses is created here.  ``sqlite_connect`` returns a DBAPI connection in autocommit
mode (the repository issues ``BEGIN IMMEDIATE`` itself) with the spec's per-connection settings applied, and
``assert_connection_safe`` refuses any connection whose pragmas are not what the trigger suite relies on.
``make_engine`` wraps the same rules around a SQLAlchemy engine so Alembic and the Core repositories share them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine

BUSY_TIMEOUT_MS = 5000

# Mandatory per-connection settings (spec "Operations" + review D1 adds recursive_triggers).
# recursive_triggers=ON is what makes DELETE triggers fire for REPLACE conflict resolution; without it the
# whole immutability suite is bypassable (review D1).
REQUIRED_PRAGMAS: dict[str, int] = {
    "foreign_keys": 1,
    "recursive_triggers": 1,
    "ignore_check_constraints": 0,
}


class ConnectionGuardError(RuntimeError):
    """Raised when a connection does not satisfy the settings the integrity suite depends on (review D7)."""


@dataclass(frozen=True)
class GuardReport:
    foreign_keys: int
    recursive_triggers: int
    ignore_check_constraints: int
    journal_mode: str
    file_backed: bool

    def violations(self) -> list[str]:
        out = [f"{k}={getattr(self, k)} (required {v})" for k, v in REQUIRED_PRAGMAS.items() if getattr(self, k) != v]
        if self.file_backed and self.journal_mode.lower() != "wal":
            out.append(f"journal_mode={self.journal_mode} (required wal)")
        return out


def inspect_connection(con: sqlite3.Connection) -> GuardReport:
    def pragma(name: str):
        return con.execute(f"PRAGMA {name}").fetchone()[0]

    main_file = next((row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main"), "")
    return GuardReport(
        foreign_keys=int(pragma("foreign_keys")),
        recursive_triggers=int(pragma("recursive_triggers")),
        ignore_check_constraints=int(pragma("ignore_check_constraints")),
        journal_mode=str(pragma("journal_mode")),
        file_backed=bool(main_file),
    )


def assert_connection_safe(con: sqlite3.Connection) -> None:
    """D7 connection guard: refuse to work on a connection whose pragmas would let the triggers fail open.

    Runs on every connection hand-out and again at the start of every write transaction.
    """
    report = inspect_connection(con)
    bad = report.violations()
    if bad:
        raise ConnectionGuardError("unsafe SQLite connection: " + ", ".join(bad))


def apply_pragmas(con: sqlite3.Connection, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA recursive_triggers=ON")  # review D1
    con.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    # WAL is a property of the file; on :memory: this returns 'memory' and the guard does not require WAL there.
    con.execute("PRAGMA journal_mode=WAL")


def sqlite_connect(path: str | Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a guarded DBAPI connection in autocommit mode (transactions are explicit ``BEGIN IMMEDIATE``)."""
    con = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    try:
        apply_pragmas(con, busy_timeout_ms=busy_timeout_ms)
        assert_connection_safe(con)
    except Exception:
        con.close()
        raise
    return con


def sqlite_url(path: str | Path) -> str:
    p = str(path)
    return "sqlite://" if p == ":memory:" else f"sqlite:///{p}"


def make_engine(path: str | Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS, immediate: bool = True) -> Engine:
    """Build a SQLAlchemy engine whose every pooled connection is configured and guarded.

    With ``immediate`` (the default) every transaction begins ``BEGIN IMMEDIATE``: the spec's write-transaction
    shape, which takes the write lock up front so two writers fail fast instead of deadlocking halfway through.
    ``write_transaction`` requires such an engine.

    Pass ``immediate=False`` for a read-only engine.  ``BEGIN IMMEDIATE`` takes the write lock whether or not a
    write follows, so a read surface built on the writer's engine serialises every concurrent reader behind one
    exclusive lock, and one long-held read connection blocks writes entirely.  A deferred transaction under WAL
    gives a reader a consistent snapshot without excluding anybody.  The pragmas and the connection guard are
    identical either way; only the lock the transaction opens with differs.
    """
    engine = sa.create_engine(sqlite_url(path), future=True)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_con, _record):
        dbapi_con.isolation_level = None  # let us control BEGIN; sqlite3 must not emit its own
        apply_pragmas(dbapi_con, busy_timeout_ms=busy_timeout_ms)
        assert_connection_safe(dbapi_con)

    if immediate:
        @event.listens_for(engine, "begin")
        def _on_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    @event.listens_for(engine, "handle_error")
    def _on_error(context):
        # Any DBAPI error inside a transaction taints it: the repository refuses to commit a tainted
        # transaction, so catch-and-commit after RAISE(ABORT) is impossible (spec warning, review).
        if context.connection is not None:
            context.connection.info["haddon_tainted"] = True

    return engine
