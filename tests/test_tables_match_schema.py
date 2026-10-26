"""Anti-drift gate: every ``sa.Table`` in ``haddon.repository.tables`` must match the migrated schema exactly.

``tables.py`` is a second declaration of the schema.  This test compares each declaration against
``PRAGMA table_info`` on a database produced by the real migration: the table must exist and the declared column
names must equal the real ones, in order.  A readable diff is printed on failure.
"""

from __future__ import annotations

import difflib
import sqlite3

import pytest

from haddon.repository import tables as t

DECLARED = sorted(t.metadata.tables)


def _real_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]


@pytest.mark.parametrize("table", DECLARED)
def test_declared_columns_match_migrated_schema(migrated_template, table):
    con = sqlite3.connect(migrated_template)
    try:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        assert exists, f"tables.py declares {table!r} but the migrated schema has no such table"
        real = _real_columns(con, table)
    finally:
        con.close()
    declared = [c.name for c in t.metadata.tables[table].columns]
    if declared != real:
        diff = "\n".join(difflib.unified_diff(real, declared, fromfile=f"schema.sql {table}", tofile=f"tables.py {table}", lineterm=""))
        pytest.fail(f"column drift on {table!r} (schema vs tables.py):\n{diff}")


def test_every_declared_table_is_real_and_declarations_are_not_empty():
    assert len(DECLARED) >= 15, DECLARED
    for table in DECLARED:
        assert list(t.metadata.tables[table].columns), table


def test_primary_keys_match(migrated_template):
    con = sqlite3.connect(migrated_template)
    try:
        for table in DECLARED:
            real_pk = [row[1] for row in con.execute(f'PRAGMA table_info("{table}")') if row[5]]
            declared_pk = [c.name for c in t.metadata.tables[table].primary_key.columns]
            assert declared_pk == real_pk, (table, declared_pk, real_pk)
    finally:
        con.close()


def test_nullability_matches(migrated_template):
    """NOT NULL in tables.py must agree with the schema (a nullable=False on a nullable column, or vice versa, is drift)."""
    con = sqlite3.connect(migrated_template)
    try:
        drift = []
        for table in DECLARED:
            real = {row[1]: bool(row[3]) or bool(row[5]) for row in con.execute(f'PRAGMA table_info("{table}")')}
            for c in t.metadata.tables[table].columns:
                declared_not_null = (not c.nullable) or c.primary_key
                if c.name in real and real[c.name] != declared_not_null:
                    drift.append(f"{table}.{c.name}: schema notnull={real[c.name]} tables.py notnull={declared_not_null}")
        assert not drift, "\n".join(drift)
    finally:
        con.close()
