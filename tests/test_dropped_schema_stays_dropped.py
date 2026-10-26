"""What the build dropped stays dropped, and nothing that survived still reaches for it.

Ten migrations' worth of deletions collapsed into one schema file when the chain was squashed.  A deletion is
the one thing a squash can lose quietly: the table is simply not declared, so nothing fails if a trigger body or
a foreign key still names it.  This file checks all the dropped names at once.

``tests/test_migrations.py`` holds the positive half (the 23 tables that must exist, exactly).  This holds the
negative half: 20 names that must not come back, and no surviving object may mention one.
"""

from __future__ import annotations

import re
import sqlite3

#: Every table the build deleted, and the revision that deleted it.  The revisions are history (those files are
#: in git, not in ``versions/``), but the names are the assertion.
DROPPED = {
    # 0003, chunk G1: dead on arrival, nothing ever read them
    "commodity_styles": "0003", "proposal_batches": "0003", "batch_items": "0003", "settlements": "0003",
    # 0008: the annual model gave way to a continuous journal
    "books": "0008",
    # 0009, issue 0004: the reconciliation extension, replaced by documents/statements/lines/explanations
    "source_configs": "0009", "source_files": "0009", "statement_scopes": "0009", "source_rows": "0009",
    "statement_assertions": "0009", "opening_assertions": "0009", "opening_verifications": "0009",
    "match_sets": "0009", "matches": "0009", "row_dispositions": "0009",
    # 0010, issue 0005: the approval chain
    "proposals": "0010", "decisions": "0010", "applications": "0010", "rule_versions": "0010",
    "rule_authorities": "0010",
}

#: Triggers that went with those tables.  A trigger outlives nothing: SQLite drops it with its table, so one
#: of these surviving would mean the table survived too.
DROPPED_TRIGGERS = (
    "styles_u", "styles_d", "items_u", "items_d", "batches_u", "batches_d",
    "settlements_u", "settlements_d", "settlements_insert", "settlements_quantum_insert",
    "books_update", "books_delete",
    "sets_insert", "sets_update", "sets_validate", "sets_delete", "matches_insert", "rows_insert",
    "assertions_insert", "scopes_insert", "dispositions_insert", "files_u", "configs_u",
    "proposals_u", "proposals_d", "decisions_u", "decisions_d", "applications_u", "applications_d",
    "rules_u", "rules_d", "authorities_u", "authorities_d",
)


def _objects(path, kind: str) -> dict[str, str]:
    con = sqlite3.connect(path)
    try:
        return dict(con.execute("SELECT name, sql FROM sqlite_master WHERE type=?", (kind,)).fetchall())
    finally:
        con.close()


def _without_comments(sql: str) -> str:
    """Strip SQL comments.  A comment may name a dropped table on purpose: ``statements`` cites
    docs/design/decisions.md, which is not a reference to the ``decisions`` table.  Only executable text counts."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_no_dropped_table_or_its_triggers_came_back(migrated_template):
    tables, triggers = _objects(migrated_template, "table"), _objects(migrated_template, "trigger")
    assert [t for t in DROPPED if t in tables] == []
    assert [t for t in DROPPED_TRIGGERS if t in triggers] == []


def test_no_surviving_object_mentions_a_dropped_table(migrated_template):
    """Nothing reaches a dropped table, including a trigger body or a view."""
    con = sqlite3.connect(migrated_template)
    try:
        rows = con.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        parents = {r[2] for t in _objects(migrated_template, "table")
                   for r in con.execute(f'PRAGMA foreign_key_list("{t}")')}
    finally:
        con.close()
    assert not parents & set(DROPPED), parents & set(DROPPED)
    mentions = [f"{kind} {name} mentions {t} (dropped in {DROPPED[t]})"
                for kind, name, sql in rows for t in DROPPED
                if re.search(rf"\b{t}\b", _without_comments(sql))]
    assert mentions == []


def test_the_dropped_names_are_not_declared_in_the_python_table_map():
    """``repository/tables.py`` is the schema's second declaration; a squash must not leave a name stranded
    there either.  ``tests/test_tables_match_schema.py`` would catch it too, but only as "no such table"; this
    says which table and when it went."""
    from haddon.repository import tables

    declared = set(tables.metadata.tables)
    assert not declared & set(DROPPED), declared & set(DROPPED)
