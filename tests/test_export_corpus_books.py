"""The export of whole imported books, end to end (chunk C2a).

These run against the databases chunk C1 imported from the corpus checkout with the pinned binary.  No figure
about those books is written down here (chunk G1 part (b)).  Every count is compared against the store the export
was made from and against the source the store came from, so the assertions read "what came out is what went in"
whatever the books hold.  The corpus shape is pinned in one place only, ``corpus/example-baselines.json`` (the
file the corpus manifest names), which the oracle compares against.

The strongest assertion is the round trip: reparsing the exported journal with the pinned binary yields the same
posting multiset (date, account, commodity and exact amount) as the source journal, and the same balances.  That
is not the C2 oracle, which compares against ``journal_mappings.canonical_txn``; it is the evidence that what
came out is the book that went in.

Nothing in the corpus checkout and nothing in ``vendor/`` is written: both are asserted byte-identical afterwards.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import pytest

from haddon.domain.canonical import canonical_json, sha256_hex
from haddon.exporting.exporter import export_book, protected_root_for, verify_artifacts
from haddon.exporting.render import UnrenderableContent
from haddon.importing.hledger import DEFAULT_BINARY, DEFAULT_PIN, Hledger, read_pin, resolve_binary, sha256_file
from haddon.importing.corpus import load_corpus
from haddon.importing.importer import import_entity

CORPUS = load_corpus()
CHECKOUT = CORPUS.checkout

from conftest import table_counts
from test_import_spain import untouched_checkout  # noqa: F401  (the same read-only guard the import uses)
from test_no_float_reaches_db import sweep_for_reals

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[1]


# Fixtures
class Exported:
    def __init__(self, entity: str, db: Path, root: Path, result) -> None:
        self.entity, self.db, self.root, self.result = entity, db, root, result
        self.ledger = Path(result.ledger_dir)
        self.main = self.ledger / "2025.journal"
        self.source = CORPUS.journal(entity.split("-")[0])

    def file(self, name: str) -> str:
        return (self.ledger / name).read_text(encoding="utf-8")

    def q(self, sql: str):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()


def _export_once(imported, tmp_path_factory, entity: str) -> Exported:
    """A writable copy of the session's imported database, exported once per module."""
    base = tmp_path_factory.mktemp(f"export-{entity}")
    db = base / f"{entity}.db"
    shutil.copy(imported.path, db)
    result = export_book(db, base / "out", year=2025)
    return Exported(entity, db, base / "out", result)


@pytest.fixture(scope="module")
def exported_inc(imported_inc, tmp_path_factory) -> Exported:
    return _export_once(imported_inc, tmp_path_factory, "ExampleInc")


@pytest.fixture(scope="module")
def exported_bv(imported_bv, tmp_path_factory) -> Exported:
    return _export_once(imported_bv, tmp_path_factory, "ExampleBV")


# Helpers
def balances(journal: Path) -> list[tuple[str, str]]:
    """``hledger balance`` through the pinned binary, with the digit-group marks of the display style removed."""
    out = Hledger().run(journal, "balance", "--no-total", "-O", "csv")
    rows = list(csv.reader(io.StringIO(out)))[1:]
    return sorted((r[0], r[1].replace(",", "")) for r in rows)


def posting_multiset(journal: Path) -> Counter:
    """Every posting as (date, account, commodity, exact amount), from ``print -O json``, never a display string."""
    return Counter((t.date, p.account, p.amount.commodity, str(p.amount.decimal()))
                   for t in Hledger().print_json(journal) for p in t.postings)


def source_groups_with_transactions(entity: str) -> set[str]:
    """The source journals that actually hold a transaction, re-derived from the source with the pinned binary."""
    return {Path(t.source_file).name for t in Hledger().print_json(CORPUS.journal(entity))}


# 1. layout
@pytest.mark.parametrize("name", ["exported_inc", "exported_bv"])
def test_the_managed_layout_is_the_source_layout(name, request, untouched_checkout):
    ex = request.getfixturevalue(name)
    on_disk = sorted(p.name for p in ex.ledger.iterdir())
    assert ex.ledger.parent.name == "2025" and ex.ledger.parent.parent.name == ex.entity  # <Entity>/<year>/ledger
    assert "2025.journal" in on_disk and "accounts.journal" in on_disk and "2025-opening.journal" in on_disk
    # One file per source journal that holds a transaction, derived from the source, not from a list here.
    assert set(on_disk) == source_groups_with_transactions(ex.entity) | {"2025.journal", "accounts.journal"}
    assert on_disk == sorted(a.path for a in ex.result.artifacts)
    includes = [l.split(" ", 1)[1] for l in ex.file("2025.journal").splitlines() if l.startswith("include ")]
    assert includes[0] == "accounts.journal" and includes[1] == "2025-opening.journal"
    assert sorted(includes) == sorted(set(on_disk) - {"2025.journal"})
    declared = [l.split(" ", 1)[1] for l in ex.file("accounts.journal").splitlines() if l.startswith("account ")]
    assert len(declared) == ex.result.counts["accounts"] and declared == sorted(declared)
    assert set(declared) == {p for (p,) in ex.q("SELECT path FROM accounts")}  # the chart, from the store


# 2. counts
def _reparsed_shape(main) -> tuple[int, int, set]:
    txns = Hledger().print_json(main)
    return (len(txns), sum(len(t.postings) for t in txns),
            {(p.account, p.amount.commodity) for t in txns for p in t.postings})


@pytest.mark.parametrize("name, currency", [("exported_inc", "USD"), ("exported_bv", "EUR")])
def test_the_export_reparses_to_exactly_what_the_store_holds(name, currency, request):
    """Every posted entry came out, and reading the result back with the pinned binary gives the store again:
    the same number of transactions, the same number of postings, the same (account, commodity) pairs."""
    ex = request.getfixturevalue(name)
    r = ex.result
    stored_entries = ex.q("SELECT count(*) FROM entries WHERE status='posted'")[0][0]
    stored_postings = ex.q("SELECT count(*) FROM postings p JOIN entries e ON e.id=p.entry_id "
                           "WHERE e.status='posted'")[0][0]
    stored_pairs = {(path, com) for path, com in ex.q(
        "SELECT a.path, p.commodity FROM postings p JOIN accounts a ON a.id=p.account_id "
        "JOIN entries e ON e.id=p.entry_id WHERE e.status='posted'")}
    assert (r.counts["entries"], r.counts["postings"]) == (stored_entries, stored_postings)
    assert _reparsed_shape(ex.main) == (stored_entries, stored_postings, stored_pairs)
    assert stored_entries > 0 and stored_pairs
    assert r.counts["commodities"] == 1 and f"commodity {currency} 1000.00" in ex.file("2025.journal")


# 3. round trip
@pytest.mark.parametrize("name", ["exported_inc", "exported_bv"])
def test_the_export_is_the_same_book_as_the_source(name, request, untouched_checkout):
    """Same postings, same balances, through the pinned binary on both sides."""
    ex = request.getfixturevalue(name)
    assert posting_multiset(ex.main) == posting_multiset(ex.source)
    assert balances(ex.main) == balances(ex.source)


@pytest.mark.parametrize("name", ["exported_inc", "exported_bv"])
def test_the_exported_journal_passes_the_strict_check_as_published(name, request):
    ex = request.getfixturevalue(name)
    Hledger().check_strict(ex.main)  # the published bytes, not the staged ones
    assert ex.result.verifier == {"tool": "hledger", "version": resolve_binary().version,
                                  "binary_sha256": read_pin(DEFAULT_PIN), "command": ["check", "--strict"],
                                  "journal": "2025.journal", "result": "pass"}


# 4. determinism
@pytest.mark.parametrize("name", ["exported_inc", "exported_bv"])
def test_a_second_export_of_the_real_book_is_byte_identical(name, request, tmp_path):
    ex = request.getfixturevalue(name)
    again = export_book(ex.db, tmp_path / "again", year=2025)
    assert again.state_hash == ex.result.state_hash and again.manifest_sha256 == ex.result.manifest_sha256
    assert [a.as_json() for a in again.artifacts] == [a.as_json() for a in ex.result.artifacts]
    for artifact in again.artifacts:
        assert (Path(again.ledger_dir) / artifact.path).read_bytes() == (ex.ledger / artifact.path).read_bytes(), artifact.path


def test_a_fresh_process_exports_the_same_bytes(exported_bv, tmp_path):
    proc = subprocess.run([str(REPO / "bin" / "haddon-export"), str(exported_bv.db), str(tmp_path / "cli"), "--year", "2025",
                           "--manifest", str(tmp_path / "m.json")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    ledger = tmp_path / "cli" / "ExampleBV" / "2025" / "ledger"
    for artifact in exported_bv.result.artifacts:
        assert (ledger / artifact.path).read_bytes() == (exported_bv.ledger / artifact.path).read_bytes(), artifact.path
    manifest = json.loads((tmp_path / "m.json").read_text())
    assert manifest["state_hash"] == exported_bv.result.state_hash
    assert sha256_hex(canonical_json(manifest)) == exported_bv.result.manifest_sha256
    assert verify_artifacts(ledger, manifest["artifacts"]) == []


# 5. the record
def test_the_real_export_is_recorded_immutably_and_completely(imported_bv, tmp_path_factory):
    """On its own copy of the imported database, so the row counts are exactly one export's worth."""
    exported_bv = _export_once(imported_bv, tmp_path_factory, "ExampleBV-record")
    r = exported_bv.result
    assert exported_bv.q("SELECT count(*) FROM exports") == [(1,)]
    [(eid, snapshot_id, digest, artifacts, verifier)] = exported_bv.q(
        "SELECT id, snapshot_id, manifest_sha256, artifacts, verifier FROM exports")
    assert (eid, snapshot_id, digest) == (r.export_id, r.snapshot_id, r.manifest_sha256)
    assert json.loads(artifacts) == [a.as_json() for a in r.artifacts] and json.loads(verifier) == r.verifier
    assert verify_artifacts(exported_bv.ledger, json.loads(artifacts)) == []
    [(seq, state_hash, through)] = exported_bv.q("SELECT snapshot_seq, state_hash, through FROM snapshots")
    assert (seq, state_hash, through) == (r.snapshot_seq, r.state_hash, "2025-12-31")
    assert exported_bv.q("SELECT kind, state FROM operations") == [("export", "succeeded")]
    assert [k for (k,) in exported_bv.q("SELECT kind FROM events ORDER BY seq")] == ["import.completed", "export.completed"]
    assert sweep_for_reals(exported_bv.db)["offenders"] == []
    # The export changed nothing about the journal itself.
    assert exported_bv.q("SELECT count(*) FROM entries WHERE status <> 'posted'") == [(0,)]
    assert exported_bv.q("SELECT through FROM period_locks") == [(None,)]


def test_a_tampered_real_artifact_fails_its_recorded_hash(exported_bv, tmp_path):
    r = export_book(exported_bv.db, tmp_path / "t", year=2025)
    recorded = json.loads(exported_bv.q("SELECT artifacts FROM exports")[0][0])
    victim = Path(r.ledger_dir) / "2025-bank.journal"
    victim.write_text(victim.read_text().replace("17.35", "17.36"), encoding="utf-8")  # one cent, one file
    problems = verify_artifacts(Path(r.ledger_dir), recorded)
    assert len(problems) == 1 and problems[0].startswith("2025-bank.journal: sha256 ")
    assert balances(Path(r.ledger_dir) / "2025.journal") != balances(exported_bv.main)  # and the book really changed


# 6. negatives
def test_a_blocked_entity_has_nothing_to_export(repo_db, migrated_counts, tmp_path, untouched_checkout):
    """A blocked import leaves no posted entry, so there is no export.

    The blocked entity is the stand-in ``tests/test_import_spain.blocked_checkout`` builds: Spain's corpus
    journals with their ``commodity EUR`` directives removed, under ``tmp_path``.  Nothing posted, no export."""
    from test_import_spain import blocked_checkout
    import_entity("ExampleSL", repo_db, checkout=blocked_checkout(tmp_path), on_blocked="skip")
    before = table_counts(repo_db)
    with pytest.raises(UnrenderableContent):
        export_book(repo_db, tmp_path / "out", year=2025)
    assert not (tmp_path / "out").exists()
    assert table_counts(repo_db) == before  # nothing recorded, nothing written
    assert before["entries"] == 0 and before["entities"] == 1


def test_the_source_checkout_and_the_pinned_binary_are_untouched(exported_inc, exported_bv, untouched_checkout):
    assert sha256_file(DEFAULT_BINARY) == read_pin(DEFAULT_PIN)
    # Scoped to the corpus directory: the checkout lives inside this repository, whose other files a
    # contributor may have edited.
    assert subprocess.run(["git", "--no-optional-locks", "-C", str(CHECKOUT), "status", "--porcelain", "--", "."],
                          capture_output=True, text=True, check=True).stdout == ""
    # The exports live entirely under their own roots (the system temp dir: `/tmp` on Linux, a /var/folders path
    # on macOS), never under the read-only checkout or vendor/.
    tmp = Path(tempfile.gettempdir()).resolve()
    for root in (exported_inc.root, exported_bv.root):
        assert root not in CHECKOUT.parents and root.resolve().is_relative_to(tmp)
    assert protected_root_for(CHECKOUT) is not None and protected_root_for(REPO / "vendor") is not None
