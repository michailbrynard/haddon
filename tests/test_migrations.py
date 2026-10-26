"""The Alembic migration is the only schema mechanism: it must produce exactly the spec's objects, be repeatable,
and its SQL files must be frozen.

Since the squash there is exactly one revision, so "what the migration produces" and "what the schema is" are
the same thing.  The table sets below still name the migrations that dropped things, because that is where the
reasoning is written down.  Those revisions are in git history, not in `versions/`."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from haddon.db.connection import sqlite_connect
from haddon.db.migrate import MIGRATIONS_DIR, current_revision, head_revision, upgrade

# The spec's 36 tables less the four that 0003 dropped, ``books`` (0008, the annual model), the nine 0009
# replaced with the document/statement/line/explanation model (issue 0004) and the approval chain (0010).  The
# dropped names are kept below as a guard: a dropped name coming back is a mistake, whichever file reintroduces it.
TABLES = {
    "entities", "commodities", "accounts", "account_commodities", "entries", "postings",
    "corrections", "evidence_links",
    "journal_mappings", "requests", "operations", "events", "snapshots", "exports", "close_records",
    "period_locks",
    # "booked, awaiting a document", append-only beside the immutable entry it is about
    "document_expectations",
    # recoverable tax that was not recovered, and why
    "unrecovered_tax",
    # issue 0004: the whole reconciliation extension, in five records
    "documents", "statements", "statement_lines", "explanations", "allocations",
}
DROPPED_BY_0003 = {"commodity_styles", "proposal_batches", "batch_items", "settlements"}
#: 0009 dropped nine and replaced ``source_files``/``evidence_links``; none of these names may come back.
DROPPED_BY_0009 = {"source_configs", "source_files", "statement_scopes", "source_rows", "statement_assertions",
                   "opening_assertions", "opening_verifications", "match_sets", "matches", "row_dispositions"}
#: 0010 deleted the approval chain outright (issue 0005): nothing writes a proposal, a decision or an
#: application any more, and the two rule tables existed only to point at one.
DROPPED_BY_0010 = {"proposals", "decisions", "applications", "rule_versions", "rule_authorities"}
assert len(TABLES) == 23 and not TABLES & (DROPPED_BY_0003 | DROPPED_BY_0009 | DROPPED_BY_0010)

REQUIRED_TRIGGERS = {
    # a sample of the spec suite, plus review fixes.  D3's ``settlements_insert`` went with its table in 0003;
    # the match-set and match guards went in 0009, replaced by the guards on ``explanations``/``allocations``.
    "entries_insert", "entries_update", "entries_delete", "postings_insert", "postings_update", "postings_delete",
    "statements_insert", "statement_lines_insert", "explanations_insert", "allocations_insert",
    "allocations_validate_insert", "locks_update",
    "locks_insert",  # D1
    "events_insert",  # D1
}

@pytest.fixture(scope="module")
def migrated(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("mig") / "entity.db"
    upgrade(path)
    return path


def _objects(path: Path, kind: str) -> dict[str, str]:
    con = sqlite3.connect(path)
    try:
        return dict(con.execute("SELECT name, sql FROM sqlite_master WHERE type=?", (kind,)).fetchall())
    finally:
        con.close()


def test_migration_creates_exactly_the_expected_tables_all_strict(migrated):
    tables = _objects(migrated, "table")
    user_tables = {n for n in tables if not n.startswith("sqlite_") and n != "alembic_version"}
    assert user_tables == TABLES
    not_strict = [n for n in user_tables if not tables[n].rstrip().upper().endswith("STRICT")]
    assert not_strict == []


def test_migration_creates_trigger_suite_and_views(migrated):
    triggers = set(_objects(migrated, "trigger"))
    assert REQUIRED_TRIGGERS <= triggers, REQUIRED_TRIGGERS - triggers
    assert not triggers & {"sets_insert", "sets_update", "sets_validate", "matches_insert", "rows_insert",
                           "assertions_insert", "scopes_insert", "dispositions_insert", "files_u", "configs_u"}
    assert not triggers & {"proposals_u", "proposals_d", "decisions_u", "decisions_d", "applications_u",
                           "applications_d", "rules_u", "rules_d", "authorities_u", "authorities_d"}
    assert len(triggers) == 65
    assert set(_objects(migrated, "view")) == {"live_statements", "live_explanations", "live_allocations",
                                               "bad_allocations", "overallocated"}


def test_migration_is_at_head_and_repeatable(migrated):
    """Head is 0001, which since the squash is the whole schema.

    The trigger count is the assertion that survives the squash: ten revisions settled on 65 triggers, and
    building the schema in one step has to land on the same 65."""
    assert current_revision(migrated) == head_revision() == "0001"
    upgrade(migrated)  # second run is a no-op
    assert current_revision(migrated) == "0001"
    assert len(_objects(migrated, "trigger")) == 65


def test_migrated_file_is_wal_and_guard_accepts_it(migrated):
    con = sqlite_connect(migrated)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    finally:
        con.close()


def test_sql_files_are_pinned_by_sha256():
    spec = importlib.util.spec_from_file_location("m0001", MIGRATIONS_DIR / "versions" / "0001_initial_schema.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, pinned in mod.PINNED_SHA256.items():
        actual = hashlib.sha256((MIGRATIONS_DIR / "sql" / "0001" / name).read_bytes()).hexdigest()
        assert actual == pinned, f"{name} changed; write a new migration instead of editing 0001"


def test_review_fixes_are_marked_in_sql():
    """Only the fixes whose objects still exist.

    D2, D3 and D5 and U2, U3 and U4 were written against tables and columns dropped between 0003 and 0010.
    Their reasoning is in docs/design/decisions.md, where a decision about a deleted table belongs; the SQL
    describes only what is there.
    """
    triggers = (MIGRATIONS_DIR / "sql" / "0001" / "triggers.sql").read_text()
    for marker in ("D1", "D4"):
        assert marker in triggers, marker
    schema = (MIGRATIONS_DIR / "sql" / "0001" / "schema.sql").read_text()
    for marker in ("U1", "U6"):
        assert marker in schema, marker
    for gone in ("D2", "D3", "D5"):
        assert gone not in triggers, f"{gone} explains an object the schema no longer has"
