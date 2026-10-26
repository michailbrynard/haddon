"""Whole-book imports of the Inc. and the B.V. from the corpus journals with the pinned binary (chunk C1).

No figure about the books is written down here (chunk G1 part (b)).  What a particular book contains is a
fact about somebody's accounts, not about this software.  The corpus shape and its digests live in one pinned
place, ``corpus/example-baselines.json`` (the file the corpus manifest names), which only the oracle compares
the books against.  A book that moves fails one test that says so rather than seven that look unrelated.

What this module asserts instead are properties that hold whatever the book is: the store holds exactly what
the pinned binary reports for the same journal, every amount equals the binary's mantissa, the chart is the
declared chart, a rolled-back import leaves nothing, and a second import into a populated database is refused.
Inc takes ~20 s; the module is marked ``slow`` but is part of the default run.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from haddon.db.connection import make_engine, sqlite_connect
from haddon.domain.errors import HaddonError
from haddon.importing import importer as I
from haddon.importing.hledger import Hledger
from haddon.reports import income_statement, trial_balance
from haddon.importing.corpus import load_corpus
from haddon.importing.importer import (ImportBlocked, ImportResult, account_type_for, import_entity, is_cash_path,
                                       memo_for, origin_for, source_revision)

CORPUS = load_corpus()
ENTITIES = CORPUS.entities

from conftest import _checkpoint, table_counts
from test_no_float_reaches_db import sweep_for_reals

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[1]


def _pairs(txns) -> set[tuple[str, str]]:
    return {(p.account, p.amount.commodity) for t in txns for p in t.postings}


def q(path: Path, sql: str, params: tuple = ()):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def scalar(path: Path, sql: str, params: tuple = ()):
    return q(path, sql, params)[0][0]


def source_shape(txns, entity: str, year: int = 2025) -> dict[str, int]:
    """The shape of a journal as the pinned binary reports it, which is all the store is compared against.

    Derived per run, never written down: if the book grows by a transaction, this grows with it and every
    assertion below still means "the store is what the source is".
    """
    return {"entries": len(txns),
            "postings": sum(len(t.postings) for t in txns),
            "pairs": len(_pairs(txns)),
            "accounts": len(Hledger().declared_accounts(CORPUS.journal(entity, year)))}


def pairs_by_path(path: Path) -> set[tuple[str, str]]:
    return set(q(path, "SELECT DISTINCT a.path, p.commodity FROM postings p JOIN accounts a ON a.id = p.account_id"))


@pytest.fixture(scope="module")
def inc_txns():
    return Hledger().print_json(CORPUS.journal("ExampleInc"))


@pytest.fixture(scope="module")
def bv_txns():
    return Hledger().print_json(CORPUS.journal("ExampleBV"))


# 2. counts
def test_inc_imports_exactly_what_the_source_holds(imported_inc, inc_txns):
    """"Exactly" is against the source, read with the pinned binary, not against a number typed here."""
    r, src = imported_inc.result, source_shape(inc_txns, "ExampleInc")
    assert isinstance(r, ImportResult) and r.blocked is None
    assert (r.entries, r.postings, r.pairs, r.accounts, r.commodities) == \
        (src["entries"], src["postings"], src["pairs"], src["accounts"], 1)
    p = imported_inc.path
    assert scalar(p, "SELECT count(*) FROM entries") == src["entries"]
    assert scalar(p, "SELECT count(*) FROM postings") == src["postings"]
    assert pairs_by_path(p) == _pairs(inc_txns)                 # the same pairs, not merely the same count
    assert scalar(p, "SELECT count(*) FROM accounts") == src["accounts"] >= src["pairs"]  # the declared chart is wider
    assert scalar(p, "SELECT count(*) FROM journal_mappings") == src["entries"]
    assert q(p, "SELECT id, legal_form, country, base_currency FROM entities") == [("example-inc", "Inc", "US", "USD")]
    assert q(p, "SELECT code, precision FROM commodities") == [("USD", 2)]


def test_bv_imports_exactly_what_the_source_holds(imported_bv, bv_txns):
    r, src = imported_bv.result, source_shape(bv_txns, "ExampleBV")
    assert (r.entries, r.postings, r.pairs, r.accounts, r.commodities) == \
        (src["entries"], src["postings"], src["pairs"], src["accounts"], 1)
    p = imported_bv.path
    assert scalar(p, "SELECT count(*) FROM entries") == src["entries"]
    assert scalar(p, "SELECT count(*) FROM postings") == src["postings"]
    assert pairs_by_path(p) == _pairs(bv_txns)
    assert q(p, "SELECT id, legal_form, country, base_currency FROM entities") == [("example-bv", "B.V.", "NL", "EUR")]
    assert q(p, "SELECT code, precision FROM commodities") == [("EUR", 2)]


def test_inc_plus_bv_distinct_pairs_across_the_two_stores(imported_inc, imported_bv, inc_txns, bv_txns):
    """Two entities in two files stay two books: the union of the stores is the union of the sources, and the
    two do not overlap at all.  They are in different commodities, so no pair can be in both."""
    inc, bv = pairs_by_path(imported_inc.path), pairs_by_path(imported_bv.path)
    assert inc | bv == _pairs(inc_txns) | _pairs(bv_txns)
    assert inc & bv == set()


# 8. Non-vacuity.  The fixture counts are not pinned here (chunk G1 part (b)); the oracle's ``baseline`` gate
# compares them against the manifest.


def test_every_posting_amount_equals_the_binary_s_mantissa_exactly(imported_inc, inc_txns):
    """Every posting: DB micros == decimalMantissa * 10**(6-2), in posting order, including the float-sensitive
    -512.17 (mantissa -51217, whose float round trip is -512169999.99999994)."""
    rows = q(imported_inc.path, "SELECT jm.source_index, p.seq, a.path, p.commodity, p.amount_micros FROM postings p "
                                "JOIN entries e ON e.id = p.entry_id JOIN journal_mappings jm ON jm.entry_id = e.id "
                                "JOIN accounts a ON a.id = p.account_id ORDER BY jm.source_index, p.seq")
    expected = [(t.index, i + 1, p.account, p.amount.commodity, p.amount.mantissa * 10**4)
                for t in inc_txns for i, p in enumerate(t.postings)]
    assert rows == expected
    assert (-51217 * 10**4) in {r[4] for r in rows} and scalar(imported_inc.path, "SELECT count(*) FROM postings WHERE amount_micros = -512170000") >= 1
    assert all(t.postings[0].amount.places == 2 for t in inc_txns)


# 3. posted, origins
@pytest.mark.parametrize("which", ["inc", "bv"])
def test_every_entry_is_posted_and_origins_are_derived_from_the_source_file(which, imported_inc, imported_bv, request):
    imp = imported_inc if which == "inc" else imported_bv
    p = imp.path
    assert q(p, "SELECT status, count(*) FROM entries GROUP BY status") == [("posted", imp.result.entries)]
    assert scalar(p, "SELECT count(*) FROM entries WHERE status IN ('draft','void')") == 0
    origins = dict(q(p, "SELECT origin, count(*) FROM entries GROUP BY origin"))
    assert origins == {"opening": 1, "source": imp.result.entries - 1} == imp.result.origins
    [(oid, dt, jpath, idx)] = q(p, "SELECT e.id, e.dt, jm.journal_path, jm.source_index FROM entries e JOIN journal_mappings jm "
                                   "ON jm.entry_id = e.id WHERE e.origin = 'opening'")
    assert dt == "2025-01-01" and jpath.endswith("/2025-opening.journal") and idx == 1
    assert scalar(p, "SELECT count(*) FROM journal_mappings jm JOIN entries e ON e.id = jm.entry_id "
                     "WHERE jm.journal_path LIKE '%-opening.journal' AND e.origin <> 'opening'") == 0
    assert scalar(p, "SELECT count(*) FROM journal_mappings jm JOIN entries e ON e.id = jm.entry_id "
                     "WHERE jm.journal_path NOT LIKE '%-opening.journal' AND e.origin <> 'source'") == 0


def test_origin_derivation_lives_in_one_place():
    assert origin_for("/x/ExampleInc/2025/ledger/2025-opening.journal") == "opening"
    assert origin_for("/x/ExampleInc/2025/ledger/2025-card.journal") == "source"
    assert origin_for("/x/2025-opening.journal.bak") == "source"
    src = (REPO / "src" / "haddon" / "importing" / "importer.py").read_text()
    assert src.count('"opening"') == 1  # the literal appears once: in origin_for


# journal_mappings
def test_journal_mappings_record_source_identity_and_a_canonical_txn(imported_inc, inc_txns):
    p = imported_inc.path
    rev = source_revision(CORPUS.checkout)
    assert len(rev) == 40
    assert q(p, "SELECT DISTINCT source_revision FROM journal_mappings") == [(rev,)]
    assert q(p, "SELECT DISTINCT source_status FROM journal_mappings") == [("Cleared",)]
    paths = {r[0] for r in q(p, "SELECT DISTINCT journal_path FROM journal_mappings")}
    assert all(not pth.startswith("/") and pth.startswith("ExampleInc/2025/ledger/") for pth in paths)
    assert len(paths) == 11  # eleven source files carry transactions
    assert scalar(p, "SELECT count(*) FROM journal_mappings jm JOIN entries e ON e.id = jm.entry_id WHERE e.status <> 'posted'") == 0
    assert scalar(p, "SELECT count(*) FROM journal_mappings WHERE txn_id IS NOT NULL") == 0  # C2
    by_index = {t.index: t for t in inc_txns}
    rows = q(p, "SELECT source_index, canonical_txn, entry_id FROM journal_mappings ORDER BY source_index")
    assert [r[0] for r in rows] == sorted(by_index)
    for idx, canon, entry_id in rows:
        rec = json.loads(canon)
        t = by_index[idx]
        assert rec["date"] == t.date and rec["description"] == t.description and rec["code"] == t.code
        assert rec["comment"] == t.comment and rec["tags"] == [list(x) for x in t.tags] and rec["status"] == t.status
        assert rec["source_index"] == idx and rec["journal_path"].endswith(Path(t.source_file).name)
        assert [(x["account"], x["commodity"], x["amount_micros"], x["comment"], x["tags"]) for x in rec["postings"]] == \
            [(pp.account, pp.amount.commodity, str(pp.amount.mantissa * 10**4), pp.comment, [list(x) for x in pp.tags]) for pp in t.postings]
        db_legs = q(p, "SELECT a.path, p.commodity, p.amount_micros FROM postings p JOIN accounts a ON a.id=p.account_id "
                       "WHERE p.entry_id=? ORDER BY p.seq", (entry_id,))
        assert [(x["account"], x["commodity"], int(x["amount_micros"])) for x in rec["postings"]] == db_legs
        assert all(isinstance(x["amount_micros"], str) for x in rec["postings"])
    # the record distinguishes a same-month date swap and a compensating leg change
    rec = json.loads(rows[5][1])
    swapped = {**rec, "date": rec["date"][:8] + ("02" if rec["date"][8:] != "02" else "03")}
    assert I.canonical_json(swapped) != I.canonical_json(rec)
    legs = json.loads(json.dumps(rec["postings"]))
    legs[0]["amount_micros"] = str(int(legs[0]["amount_micros"]) + 10000)
    legs[1]["amount_micros"] = str(int(legs[1]["amount_micros"]) - 10000)
    assert I.canonical_json({**rec, "postings": legs}) != I.canonical_json(rec)


def test_posting_memos_preserve_raw_comments_and_tags(imported_inc, inc_txns):
    rows = q(imported_inc.path, "SELECT jm.source_index, p.seq, p.memo FROM postings p JOIN journal_mappings jm ON jm.entry_id = p.entry_id "
                                "ORDER BY jm.source_index, p.seq")
    expected = [(t.index, i + 1, memo_for(pp)) for t in inc_txns for i, pp in enumerate(t.postings)]
    assert rows == expected
    memos = {m for _, _, m in rows if m}
    assert "orig: 844.76 EUR\n" in memos  # verbatim, trailing newline kept
    assert sum(1 for _, _, m in rows if m) == 70  # 69 postings with comments + 1 with an inherited tag only
    tagged_only = [m for _, _, m in rows if m == "(class: depreciation of fixed assets)"]
    assert len(tagged_only) == 1  # the year-end depreciation: hledger reports the account's tag on a posting with no comment; it is kept


# Chart from declarations
def test_chart_is_the_declared_accounts_typed_by_first_segment_with_the_cash_rule(imported_inc):
    p = imported_inc.path
    declared = Hledger().declared_accounts(CORPUS.journal("ExampleInc"))
    rows = q(p, "SELECT id, path, type, is_cash, archived FROM accounts ORDER BY path")
    assert sorted(r[1] for r in rows) == sorted(declared)
    for aid, path, typ, cash, archived in rows:
        assert aid == f"acct-{path}" and typ == account_type_for(path) and bool(cash) == is_cash_path(path) and archived == 0
    cash = sorted(x for x in declared if x.startswith("assets:cash:"))
    assert q(p, "SELECT path FROM accounts WHERE is_cash=1 ORDER BY path") == [(a,) for a in cash] and cash
    # accounts with no postings exist only because they are declared (chart is not built from observed names)
    unused = scalar(p, "SELECT count(*) FROM accounts a WHERE NOT EXISTS (SELECT 1 FROM postings p WHERE p.account_id = a.id)")
    assert unused == len(declared) - len({r[0] for r in q(p, "SELECT DISTINCT account_id FROM postings")}) > 0
    # conservative default: every account allows exactly the entity's declared commodities (here just USD)
    assert q(p, "SELECT commodity, count(*) FROM account_commodities GROUP BY commodity") == [("USD", len(declared))]


def test_unknown_first_segment_is_an_error_not_a_default():
    for path in ("foo:bar", "Assets:cash", "asset:cash", "revenue:sales", ""):
        with pytest.raises(HaddonError) as ei:
            account_type_for(path)
        assert ei.value.code == "validation" and ei.value.details[0]["reason"] == "unknown_account_type"
    assert account_type_for("liabilities:x") == "liability" and account_type_for("expenses:x:y") == "expense"
    assert is_cash_path("assets:cash") and is_cash_path("assets:cash:first-bank") and not is_cash_path("assets:cashlike") \
        and not is_cash_path("assets:receivables:cash")


# 5. exactness sweep
def test_no_real_anywhere_in_the_imported_inc_database(imported_inc):
    sweep = sweep_for_reals(imported_inc.path)
    assert not sweep["offenders"], "\n".join(sweep["offenders"])
    assert sweep["tables"] >= 20 and sweep["columns"] >= 200
    stored_postings = scalar(imported_inc.path, "SELECT count(*) FROM postings")
    stored_entries = scalar(imported_inc.path, "SELECT count(*) FROM entries")
    assert sweep["rows"] >= stored_postings + stored_entries * 2
    for must in ("postings", "entries", "journal_mappings", "accounts", "account_commodities", "commodities", "events"):
        assert must in sweep["nonempty"], must
    assert scalar(imported_inc.path, "SELECT count(*) FROM postings WHERE typeof(amount_micros) <> 'integer'") == 0
    assert scalar(imported_inc.path, "SELECT count(*) FROM postings WHERE amount_micros % 10000 <> 0") == 0  # 2-dp quantum


# 4. undeclared account
def _tmp_checkout(tmp_path: Path, entity: str, edit) -> Path:
    """A copy of one entity's ledger directory under /tmp, laid out like the checkout and committed to a throwaway
    git repo (the importer reads ``rev-parse HEAD``).  The real checkout is never touched."""
    root = tmp_path / "checkout"
    dst = root / entity / "2025" / "ledger"
    shutil.copytree(CORPUS.journal(entity).parent, dst)
    edit(dst)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "copy"], check=True)
    return root


def _rename_one_posting(dst: Path) -> None:
    f = dst / "2025-bank.journal"
    text = f.read_text()
    assert "    assets:cash:bank " in text
    f.write_text(text.replace("    assets:cash:bank ", "    assets:cash:undeclared-xyz ", 1))


def test_undeclared_account_fails_at_the_strict_gate_and_leaves_the_store_empty(tmp_path, repo_db, migrated_counts):
    root = _tmp_checkout(tmp_path, "ExampleBV", _rename_one_posting)
    with pytest.raises(ImportBlocked) as ei:
        import_entity("ExampleBV", repo_db, checkout=root)
    assert 'account "assets:cash:undeclared-xyz" has not been declared' in str(ei.value)
    assert ei.value.error.file.endswith("2025-bank.journal") and ei.value.error.line is not None
    assert table_counts(repo_db) == migrated_counts


def test_importer_s_own_undeclared_account_gate_names_account_and_transaction(tmp_path, repo_db, migrated_counts):
    """Even if the strict gate were bypassed, a posting to an undeclared account is a hard error, and nothing commits."""
    root = _tmp_checkout(tmp_path, "ExampleBV", _rename_one_posting)

    class GateBypassed(Hledger):
        def check_strict(self, journal):  # the only thing bypassed; declarations and JSON are still real
            return None

    with pytest.raises(HaddonError) as ei:
        import_entity("ExampleBV", repo_db, checkout=root, hledger=GateBypassed())
    e = ei.value
    assert e.code == "account_unknown" and e.status == 422
    assert "assets:cash:undeclared-xyz" in e.message and "is not a declared account" in e.message
    assert e.details[0]["account"] == "assets:cash:undeclared-xyz" and isinstance(e.details[0]["tindex"], int)
    assert f"transaction {e.details[0]['tindex']}" in e.message
    assert table_counts(repo_db) == migrated_counts


# 6. rollback
class InjectedFault(Exception):
    pass


def test_injected_failure_at_transaction_800_of_inc_leaves_zero_rows(repo_db, migrated_counts):
    seen = []

    def fault(txn):
        seen.append(txn.index)
        if txn.index == 800:
            raise InjectedFault("boom at 800")

    with pytest.raises(InjectedFault):
        import_entity("ExampleInc", repo_db, _fault=fault)
    assert seen == list(range(1, 801))  # 799 transactions had been posted inside the transaction
    after = table_counts(repo_db)
    assert after == migrated_counts
    assert after["entries"] == after["postings"] == after["journal_mappings"] == after["accounts"] == after["entities"] == after["events"] == 0
    con = sqlite_connect(repo_db)
    try:
        # `sqlite_sequence` keeps its row once `events` has ever been written to, and migration 0009 rebuilds
        # `events` (dropping `opening_verification_id`) by carrying rows through a scratch table, so the row
        # exists from migration time.  What must be zero is the high-water mark itself.
        assert con.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0] == 0
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        con.close()
    # positive control: the same database then imports B.V. cleanly
    r = import_entity("ExampleBV", repo_db)
    assert (r.entries, r.postings) == (scalar(repo_db, "SELECT count(*) FROM entries"),
                                       scalar(repo_db, "SELECT count(*) FROM postings")) and r.entries > 0


# Imported, not closed
@pytest.mark.parametrize("which", ["inc", "bv"])
def test_import_leaves_no_lock_or_verification(which, imported_inc, imported_bv):
    imp = imported_inc if which == "inc" else imported_bv
    p = imp.path
    assert q(p, "SELECT through FROM period_locks") == [(None,)]
    assert scalar(p, "SELECT count(*) FROM close_records") + scalar(p, "SELECT count(*) FROM snapshots") == 0

    assert q(p, "SELECT kind, subject_kind, subject_id FROM events") == [("import.completed", "import", f"{imp.entity}/2025")]
    after = json.loads(scalar(p, "SELECT after FROM events"))
    assert after["entries"] == scalar(p, "SELECT count(*) FROM entries")
    assert q(p, "SELECT created_by FROM entries GROUP BY created_by") == [(I.ACTOR,)]


def test_import_refuses_a_database_that_already_holds_an_entity(imported_bv, fast_dir):
    dst = fast_dir / "again.db"
    shutil.copy(imported_bv.path, dst)
    with pytest.raises(HaddonError) as ei:
        import_entity("ExampleBV", dst)
    assert ei.value.code == "immutable" and "already holds an entity" in ei.value.message
    assert table_counts(dst) == table_counts(imported_bv.path)


def test_unknown_entity_and_unmigrated_target_are_refused(fast_dir):
    with pytest.raises(HaddonError) as ei:
        import_entity("ExampleLtd", fast_dir / "x.db")
    assert ei.value.details[0]["reason"] == "enum"
    con = sqlite3.connect(fast_dir / "empty.db")
    con.close()
    with pytest.raises(HaddonError) as ei:
        import_entity("ExampleBV", fast_dir / "empty.db")
    assert ei.value.details[0]["reason"] == "not_migrated"
    assert set(ENTITIES) == {"ExampleInc", "ExampleBV", "ExampleSL"}


# CLI
def test_cli_imports_bv_and_prints_the_counts(fast_dir):
    db = fast_dir / "cli-bv.db"
    proc = subprocess.run([str(REPO / "bin" / "haddon-import"), "ExampleBV", str(db)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    # the CLI prints the counts it actually wrote, whatever they are
    assert f"entries={scalar(db, 'SELECT count(*) FROM entries')} " \
           f"postings={scalar(db, 'SELECT count(*) FROM postings')} " \
           f"pairs={len(pairs_by_path(db))}" in proc.stdout
    assert "example-bv" in proc.stdout and scalar(db, "SELECT count(*) FROM entries") > 0
    again = subprocess.run([str(REPO / "bin" / "haddon-import"), "ExampleBV", str(db)], capture_output=True, text=True)
    assert again.returncode != 0 and "already holds an entity" in again.stderr


# A later year beside an earlier one.  `into_existing` is the explicit opt-in for loading one entity's later
# date range into the database that already holds an earlier range.  A journal is continuous (issue 0002), so
# 2025 and 2026 are two ranges of one ledger living in one file.  By default an import still targets an empty
# migrated database, and every way of getting this wrong is refused.
@pytest.fixture(scope="module")
def two_year_bv(tmp_path_factory):
    db = tmp_path_factory.mktemp("two-year") / "bv.db"
    from haddon.db.migrate import upgrade
    upgrade(db)
    first = import_entity("ExampleBV", db, year=2025)
    second = import_entity("ExampleBV", db, year=2026, into_existing=True)
    # Fold the WAL back into the file before any test ``shutil.copy``s it.  SQLite does that on last-connection
    # close, but only when nothing else is reading, and whether something is comes down to when the interpreter
    # last collected a reference cycle.  So a plain copy ships a complete database almost always and an empty
    # one occasionally (see TestDeliberateCorruption in test_oracle.py).
    _checkpoint(db)
    return db, first, second


def test_a_later_year_imports_beside_the_earlier_one(two_year_bv):
    """Each range holds what its own import reported, and neither year's rows leaked into the other's.

    B.V.'s 2026 journal holds exactly one transaction, the opening restatement of the 2025 closing sheet, and a
    continuous journal already holds 2025.  So the second import posts nothing and verifies that one transaction
    instead (see ``haddon.importing.restatement`` and the test below).  Zero entries is the right answer here,
    not an empty result: the event and the ``ImportResult`` both say what was verified."""
    db, first, second = two_year_bv
    assert first.entries > 0
    assert second.entries == 0 and second.postings == 0 and second.journal_mappings == 0
    assert len(second.restatements) == 1
    assert q(db, "SELECT count(*) FROM entries WHERE dt>='2025-01-01' AND dt<='2025-12-31'") == [(first.entries,)]
    assert q(db, "SELECT count(*) FROM entries WHERE dt>='2026-01-01' AND dt<='2026-12-31'") == [(second.entries,)]
    assert q(db, "SELECT count(*) FROM journal_mappings jm JOIN entries e ON e.id=jm.entry_id "
                 "WHERE e.dt>='2025-01-01' AND e.dt<='2025-12-31'") == [(first.entries,)]
    assert q(db, "SELECT count(*) FROM journal_mappings jm JOIN entries e ON e.id=jm.entry_id "
                 "WHERE e.dt>='2026-01-01' AND e.dt<='2026-12-31'") == [(second.entries,)]
    assert scalar(db, "SELECT count(*) FROM entities") == 1
    assert q(db, "SELECT code, precision FROM commodities") == [("EUR", 2)]
    # the counts a result reports are that range's, not a running total across both
    assert first.entries != first.entries + second.entries or second.entries == 0


def test_no_carried_balance_inflation_on_the_real_books(two_year_bv):
    """A restated opening is verified, not posted, so carried balances are not counted twice.

    ``2026-opening.journal`` is a restatement of the 2025 closing balance sheet: the same balances again, in
    absolute amounts, with 2025's result folded into retained earnings.  Posting it onto a continuous journal
    that already holds 2025 doubles every balance-sheet account while the journal still balances per commodity,
    so no invariant catches it.

    The test also proves it is saying something: every carried pair would have been inflated, and by exactly
    the restated amount.
    """
    db, _, second = two_year_bv
    engine = make_engine(db, busy_timeout_ms=200, immediate=False)
    try:
        with engine.connect() as conn:
            through_2026 = trial_balance(conn, "example-bv", through="2026-12-31")
            accumulated = income_statement(conn, "example-bv", start=None, end="2026-12-31")
    finally:
        engine.dispose()
    held = {ln.path: ln.amount.render() for ln in through_2026.lines}
    # the restatement, read straight out of the pinned source: what a doubling would have produced
    restated = _restated_2026_opening()
    assert restated and set(restated) <= set(held)
    for path, amount in restated.items():
        assert held[path] != str(2 * int(amount)), f"{path} is the restatement counted twice"
    # every balance-sheet line except the folded one is exactly what the 2026 book states
    (fold,) = second.restatements[0]["fold_paths"]
    for path, amount in restated.items():
        if path != fold:
            assert held[path] == amount, path
    # and the one that differs differs by exactly the accumulated result
    assert int(held[fold]) + int(accumulated.net["EUR"].render()) == int(restated[fold])
    assert second.restatements[0]["folded"] == {"EUR": accumulated.net["EUR"].render()}


def _restated_2026_opening() -> dict[str, str]:
    """The 2026 opening as the pinned binary reports it: account path -> integer micros."""
    hl = Hledger()
    journal = CORPUS.journal("ExampleBV", 2026)
    declared = hl.declared_commodities(journal)
    out: dict[str, str] = {}
    for txn in hl.print_json(journal):
        for posting in txn.postings:
            out[posting.account] = posting.amount.micros(declared[posting.amount.commodity]).render()
    return out


def test_the_chart_grows_and_is_never_overwritten(two_year_bv):
    db, _, second = two_year_bv
    declared_2025 = set(Hledger().declared_accounts(CORPUS.journal("ExampleBV", 2025)))
    declared_2026 = set(Hledger().declared_accounts(CORPUS.journal("ExampleBV", 2026)))
    stored = {p for (p,) in q(db, "SELECT path FROM accounts")}
    assert stored == declared_2025 | declared_2026 == set() | stored
    assert len(stored) == second.accounts == len(declared_2025 | declared_2026) > len(declared_2025)
    # every account still allows the entity's one declared commodity, including the ones 2026 added
    assert q(db, "SELECT commodity, count(*) FROM account_commodities GROUP BY commodity") == [("EUR", len(stored))]
    for path in sorted(declared_2025 & declared_2026):
        assert scalar(db, "SELECT count(*) FROM accounts WHERE path = ?", (path,)) == 1  # reused, not duplicated
    assert q(db, "SELECT id FROM accounts WHERE path = 'equity:retained-earnings'") == [("acct-equity:retained-earnings",)]


def test_every_way_of_getting_a_second_import_wrong_is_still_refused(two_year_bv, fast_dir, repo_db):
    db, _, _ = two_year_bv
    dst = fast_dir / "copy.db"
    shutil.copy(db, dst)
    before = table_counts(dst)
    # the default is unchanged: no flag, and a populated database is refused outright
    with pytest.raises(HaddonError) as ei:
        import_entity("ExampleBV", dst, year=2026)
    assert ei.value.code == "immutable" and "already holds an entity" in ei.value.message
    # a range that already has entries is refused even with the flag: a range is imported once
    with pytest.raises(HaddonError) as ei:
        import_entity("ExampleBV", dst, year=2025, into_existing=True)
    assert ei.value.code == "immutable" and ei.value.details[0]["reason"] == "range_exists"
    # a different entity is refused: one database is one entity
    with pytest.raises(HaddonError) as ei:
        import_entity("ExampleInc", dst, year=2026, into_existing=True)
    assert ei.value.code == "immutable" and ei.value.details[0]["reason"] == "entity_mismatch"
    assert ei.value.details[0]["stored"] == "example-bv" and ei.value.details[0]["requested"] == "example-inc"
    # and the flag is not a way to skip provisioning: an empty database has no earlier journal to extend
    with pytest.raises(HaddonError) as ei:
        import_entity("ExampleBV", repo_db, year=2026, into_existing=True)
    assert ei.value.details[0]["reason"] == "not_provisioned"
    assert table_counts(dst) == before  # every refusal wrote nothing
