"""Review D7: every connection must have the pragmas the trigger suite relies on, or it is refused."""

from __future__ import annotations

import sqlite3

import pytest
import sqlalchemy as sa

from haddon.db.connection import (
    REQUIRED_PRAGMAS,
    ConnectionGuardError,
    assert_connection_safe,
    inspect_connection,
    make_engine,
    sqlite_connect,
)
from haddon.db.migrate import upgrade
from haddon.repository.transaction import write_transaction


def test_sqlite_connect_applies_mandatory_settings(tmp_path):
    path = tmp_path / "e.db"
    upgrade(path)
    con = sqlite_connect(path)
    try:
        r = inspect_connection(con)
        assert (r.foreign_keys, r.recursive_triggers, r.ignore_check_constraints) == (1, 1, 0)
        assert r.journal_mode == "wal" and r.file_backed
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert con.isolation_level is None  # autocommit; transactions are explicit BEGIN IMMEDIATE
    finally:
        con.close()


def test_memory_connection_is_guarded_without_wal_requirement():
    con = sqlite_connect(":memory:")
    try:
        r = inspect_connection(con)
        assert not r.file_backed and r.journal_mode == "memory"
        assert_connection_safe(con)
    finally:
        con.close()


@pytest.mark.parametrize("pragma,bad", [("foreign_keys", "OFF"), ("recursive_triggers", "OFF"), ("ignore_check_constraints", "ON")])
def test_guard_refuses_each_unsafe_pragma(pragma, bad):
    raw = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for k, v in REQUIRED_PRAGMAS.items():
            raw.execute(f"PRAGMA {k}={'ON' if v else 'OFF'}")
        assert_connection_safe(raw)
        raw.execute(f"PRAGMA {pragma}={bad}")
        with pytest.raises(ConnectionGuardError, match=pragma):
            assert_connection_safe(raw)
    finally:
        raw.close()


def test_guard_refuses_file_db_not_in_wal(tmp_path):
    path = tmp_path / "rollback.db"
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("PRAGMA journal_mode=DELETE")
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute("PRAGMA recursive_triggers=ON")
    with pytest.raises(ConnectionGuardError, match="journal_mode"):
        assert_connection_safe(raw)
    raw.close()


def test_engine_connections_are_configured_and_use_begin_immediate(tmp_path):
    path = tmp_path / "e.db"
    upgrade(path)
    engine = make_engine(path)
    with engine.connect() as conn:
        dbapi = conn.connection.driver_connection
        r = inspect_connection(dbapi)
        assert (r.foreign_keys, r.recursive_triggers, r.ignore_check_constraints, r.journal_mode) == (1, 1, 0, "wal")
        assert dbapi.isolation_level is None
    # BEGIN IMMEDIATE takes the write lock at once: a second writer cannot start.
    with write_transaction(engine) as conn:
        assert conn.connection.driver_connection.in_transaction
        other = sqlite_connect(path, busy_timeout_ms=50)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


def test_tampered_pooled_connection_is_refused_at_next_write(tmp_path):
    path = tmp_path / "e.db"
    upgrade(path)
    engine = make_engine(path, busy_timeout_ms=100)
    with engine.connect() as conn:
        # Through SQLAlchemy every statement runs inside BEGIN IMMEDIATE, where SQLite ignores foreign_keys
        # changes; real tampering has to go through the raw driver connection outside a transaction.
        conn.connection.driver_connection.execute("PRAGMA foreign_keys=OFF")
        assert conn.connection.driver_connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    with pytest.raises(ConnectionGuardError, match="foreign_keys"):
        with write_transaction(engine):
            pass  # pragma: no cover
    engine.dispose()
    # After disposing of the tampered connection the guard passes again.
    with write_transaction(make_engine(path)) as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
