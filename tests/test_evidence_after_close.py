"""Evidence after a close: the one case that is allowed in, and the one that still is not
(docs/design/decisions.md, "Documents that are not bank statements").

Filing a document against an entry in a closed period is allowed, because an evidence link has no amount, no
account and no date of its own and changes nothing a closed period reports.  Filing one against a statement is
not: a statement says "the closing balance was X, printed here", and its evidence link is where "here" is, so a
citation added after the close restates what the closed month's figures rest on.  That belongs in a reopen.

The rule came in with migration 0004.  It is checked where it is enforced: against ``evidence_insert`` as it
ships, and against the two INSERTs themselves.  The rest of the refusal surface is in
``tests/test_integrity_mutation_matrix.py``; the sibling cases are duplicated here because "exactly one of these
is allowed" is the substance of the decision.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import restore_into_memory

C = "entity_id,created_at,created_by"
CV = "'E','t','who'"


def test_the_shipped_trigger_gates_statements_and_says_nothing_about_entries(migrated_template):
    """Read off the trigger: the lock check reaches for a statement's period and for nothing else.

    An entry's authority is its own audit action, so the entry conjunct 0004 removed must not creep back."""
    con = sqlite3.connect(migrated_template)
    try:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name='evidence_insert'").fetchone()[0]
    finally:
        con.close()
    assert "FROM statements s" in sql and "NEW.statement_id" in sql
    assert "FROM entries" not in sql and "NEW.entry_id" not in sql
    assert sql.count("RAISE(ABORT,'period_locked')") == 1


def test_a_statement_in_a_closed_period_still_refuses_its_evidence(templates):
    """The sibling case 0004 deliberately kept.

    A statement says "the closing balance was X, printed here", and its evidence link is where "here" is, so
    adding a citation to a closed period's statement restates what a closed month's figures rest on.  That
    belongs in a reopen.  An entry's authority is its own audit action, which is why the entry case could go.
    (Migration 0009 moved this guard from the old assertion records onto the statement itself.)
    """
    con = restore_into_memory(templates[True])
    try:
        with pytest.raises(sqlite3.DatabaseError, match="period_locked"):
            con.execute(f"INSERT INTO evidence_links(id,{C},document_id,statement_id,locator,caption) "
                        f"VALUES('x',{CV},'f2','s_jan','l','c')")
    finally:
        con.close()


def test_an_entry_in_a_closed_period_accepts_its_evidence(templates):
    """The positive control: the same INSERT as above, against an entry, is accepted."""
    con = restore_into_memory(templates[True])
    try:
        con.execute(f"INSERT INTO evidence_links(id,{C},document_id,entry_id,locator,caption) "
                    f"VALUES('x',{CV},'f2','e_jan','l','c')")
        assert con.execute("SELECT entry_id FROM evidence_links WHERE id='x'").fetchone()[0] == "e_jan"
    finally:
        con.close()
