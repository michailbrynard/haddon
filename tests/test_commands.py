"""The command contract: the journal loop through the typed commands, each writing its own audit action.

These run against a migrated, unprovisioned database file (``repo_engine``) so ``initialize_entity`` is the first
real use: a generic entity with no source checkout, no statement configuration and no calendar book named by the
caller.  Everything goes through ``haddon.commands``, as the HTTP transport does.
"""

from __future__ import annotations

import sqlite3

import pytest

from haddon import commands
from haddon.domain.errors import HaddonError
from haddon.repository import entries as entries_repo
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction

ACTOR = "agent:phase-one"


def _init(engine):
    with write_transaction(engine) as conn:
        return commands.initialize_entity(conn, actor=ACTOR, id="acme-ltd", name="Acme Ltd", legal_form="Ltd",
                                          country="US", base_currency="USD", base_precision=2)


def _ctx(engine, actor=ACTOR):
    """A committed write ctx is awkward across tests; each call opens a fresh transaction the caller commits."""
    return write_transaction(engine)


def _count(engine, table: str) -> int:
    con = sqlite3.connect(str(engine.url.database))
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _tables(engine) -> set[str]:
    con = sqlite3.connect(str(engine.url.database))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_initialize_entity_provisions_a_generic_entity_and_audits_it(repo_engine):
    result = _init(repo_engine)
    assert result.entity.id == "acme-ltd" and result.entity.base_currency == "USD"
    assert result.commodities["USD"].precision == 2
    # The starter chart, including the one equity account every entity carries.
    assert {"assets:cash", "expenses:general", "income:sales", "equity:retained-earnings"} <= set(result.accounts)
    assert result.audit.kind == "entity.initialized"
    assert _count(repo_engine, "events") == 1


def test_initialize_entity_never_guesses_the_base_currency_precision(repo_engine):
    with write_transaction(repo_engine) as conn:
        result = commands.initialize_entity(conn, actor=ACTOR, id="tokyo-co", name="Tokyo Co",
                                            legal_form="K.K.", country="JP", base_currency="JPY",
                                            base_precision=0)
    assert result.commodities["JPY"].precision == 0


def test_post_entry_is_direct_audited_and_creates_no_approval_chain(repo_engine):
    _init(repo_engine)
    with write_transaction(repo_engine) as conn:
        ctx = WriteCtx.open(conn, ACTOR)
        posted = commands.post_entry(ctx, dt="2025-02-10", narration="Sale — invoice 1", reason="goods delivered",
                                     legs=[commands.LegSpec("assets:cash", "USD", "1200.00"),
                                           commands.LegSpec("income:sales", "USD", "-1200.00")])
    assert posted.entry.status == "posted"
    assert posted.audit.kind == "entry.posted" and posted.audit.reason == "goods delivered"
    # There is no approval chain (issues 0003 and 0005); the only record of authority is the audit action.
    assert _tables(repo_engine).isdisjoint({"proposals", "decisions", "applications"})


def test_reverse_entry_writes_a_mirror_with_no_decision(repo_engine):
    _init(repo_engine)
    with write_transaction(repo_engine) as conn:
        posted = commands.post_entry(WriteCtx.open(conn, ACTOR), dt="2025-02-10", narration="Sale",
                                     reason="goods delivered",
                                     legs=[commands.LegSpec("assets:cash", "USD", "1200.00"),
                                           commands.LegSpec("income:sales", "USD", "-1200.00")]).entry
    with write_transaction(repo_engine) as conn:
        correction = commands.reverse_entry(WriteCtx.open(conn, ACTOR), original_id=posted.id,
                                            reason="the invoice was cancelled")
    assert correction.audit.kind == "entry.reversed"
    assert _count(repo_engine, "events") == 3  # initialize, post, reverse: one action each
    assert correction.correction.replacement_id is None
    assert not hasattr(correction.correction, "decision_id")
    con = sqlite3.connect(str(repo_engine.url.database))
    try:
        columns = {r[1] for r in con.execute("PRAGMA table_info(corrections)")}
    finally:
        con.close()
    assert "decision_id" not in columns
    # The original is left standing and the mirror is its exact negative.
    with write_transaction(repo_engine) as conn:
        original = entries_repo.get_entry(conn, "acme-ltd", posted.id)
        mirror = entries_repo.get_entry(conn, "acme-ltd", correction.correction.mirror_id)
    assert original.status == "posted" and mirror.status == "posted"
    assert sorted((p.account_id, p.amount.value) for p in mirror.legs) == \
        sorted((p.account_id, -p.amount.value) for p in original.legs)


def test_correct_entry_replaces_in_one_transaction(repo_engine):
    _init(repo_engine)
    with write_transaction(repo_engine) as conn:
        posted = commands.post_entry(WriteCtx.open(conn, ACTOR), dt="2025-03-05", narration="Supplies",
                                     reason="bought supplies",
                                     legs=[commands.LegSpec("expenses:general", "USD", "300.00"),
                                           commands.LegSpec("assets:cash", "USD", "-300.00")]).entry
    replacement = commands.Replacement(dt="2025-03-05", narration="Supplies (corrected)", origin="manual",
                                               legs=[commands.LegSpec("expenses:general", "USD", "250.00"),
                                                     commands.LegSpec("assets:cash", "USD", "-250.00")])
    with write_transaction(repo_engine) as conn:
        correction = commands.correct_entry(WriteCtx.open(conn, ACTOR), original_id=posted.id,
                                            reason="the receipt says 250, not 300", replacement=replacement)
    assert correction.audit.kind == "entry.corrected"
    assert correction.correction.replacement_id is not None
    with write_transaction(repo_engine) as conn:
        replacement_entry = entries_repo.get_entry(conn, "acme-ltd", correction.correction.replacement_id)
    assert replacement_entry.narration == "Supplies (corrected)"


def test_a_command_that_fails_leaves_no_audit_action(repo_engine):
    _init(repo_engine)
    before = _count(repo_engine, "events")
    with pytest.raises(HaddonError):
        with write_transaction(repo_engine) as conn:
            commands.post_entry(WriteCtx.open(conn, ACTOR), dt="2025-02-10", narration="One-sided",
                                reason="should fail", legs=[commands.LegSpec("assets:cash", "USD", "1.00")])
    assert _count(repo_engine, "events") == before
    assert _count(repo_engine, "entries") == 0


def test_audit_hash_covers_the_complete_entry_input(repo_engine):
    _init(repo_engine)
    hashes = []
    variants = [
        {"code": "", "comment": "", "tags": (), "id": None},
        {"code": "INV-1", "comment": "", "tags": (), "id": None},
        {"code": "", "comment": "source invoice", "tags": (), "id": None},
        {"code": "", "comment": "", "tags": (("invoice", "1"),), "id": None},
        {"code": "", "comment": "", "tags": (), "id": "entry-supplied-by-caller"},
    ]
    for variant in variants:
        with write_transaction(repo_engine) as conn:
            written = commands.post_entry(
                WriteCtx.open(conn, ACTOR), dt="2025-02-10", narration="Sale", reason="goods delivered",
                legs=[commands.LegSpec("assets:cash", "USD", "1200.00"),
                      commands.LegSpec("income:sales", "USD", "-1200.00")], **variant)
            hashes.append(written.audit.input_hash)
    assert len(set(hashes)) == len(variants)


def test_initialize_entity_refuses_a_second_entity(repo_engine):
    _init(repo_engine)
    with pytest.raises(HaddonError):
        with write_transaction(repo_engine) as conn:
            commands.initialize_entity(conn, actor=ACTOR, id="another", name="Another", legal_form="Ltd",
                                       country="US", base_currency="USD", base_precision=2)


def test_the_whole_loop_runs_on_a_fresh_database_with_no_approval_chain(repo_engine):
    """Initialize, post, read a report and inspect the audit action, with no corpus checkout, no caller-named
    book, no statement configuration and no proposal concept (the Phase 1 exit gate)."""
    from haddon.domain.micros import Micros
    from haddon.reports.balances import trial_balance

    init = _init(repo_engine)
    with write_transaction(repo_engine) as conn:
        posted = commands.post_entry(WriteCtx.open(conn, ACTOR), dt="2025-06-01", narration="Consulting income",
                                     reason="June invoice raised",
                                     legs=[commands.LegSpec("assets:receivables", "USD", "800.00"),
                                           commands.LegSpec("income:sales", "USD", "-800.00")])
    with repo_engine.connect() as conn:
        tb = trial_balance(conn, "acme-ltd", through="2025-12-31")
    assert tb.get(init.accounts["assets:receivables"].id, "USD") == Micros(800_000_000)
    assert tb.get(init.accounts["income:sales"].id, "USD") == Micros(-800_000_000)

    events = repo_engine.connect()
    try:
        rows = events.exec_driver_sql(
            "SELECT kind, reason, after FROM events WHERE subject_id=? ORDER BY seq", (posted.entry.id,)).fetchall()
    finally:
        events.close()
    assert len(rows) == 1
    kind, reason, after = rows[0]
    assert kind == "entry.posted" and reason == "June invoice raised"
    assert posted.entry.id in after
    assert _tables(repo_engine).isdisjoint({"proposals", "decisions", "applications"})
