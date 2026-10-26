"""The deterministic export, on a small provisioned entity (chunk C2a).

Everything the export promises that does not need a full corpus book is proved here: the ordering and
rendering rules, the refusals, the ``posted``-only filter, the range scope, the strict gate on the export's own
output, the artifact digests and the immutable ``exports`` row.  ``tests/test_export_corpus_books.py`` then proves
the same guarantees against the corpus Inc and B.V. journals.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from haddon.domain.errors import HaddonError
from haddon.domain.micros import Commodity, Micros
from haddon.exporting import render as R
from haddon.exporting.exporter import (ArtifactMismatch, ExportBlocked, export_book, is_managed, read_book_state,
                                       verify_artifacts)
from haddon.importing.hledger import DEFAULT_BINARY, DEFAULT_PIN, Hledger, HledgerError, read_pin, sha256_file
from haddon.repository import corrections as co
from haddon.repository import entries as en
from haddon.repository import provisioning as pv

from repo_helpers import AGENT, COMPANY, ENTITY_ID, m, pair, post

REPO = Path(__file__).resolve().parents[1]


# Helpers
@pytest.fixture
def book(fx):
    """A small 2025 book: four posted entries, one draft, one void, and one 2026 entry outside the range."""
    post(fx, "2025-03-02", "125000000", narration="march rent")
    post(fx, "2025-01-15", "20000000", narration="january stationery", memo="box of pens")
    post(fx, "2025-01-15", "31000000", narration="january coffee")
    post(fx, "2025-07-09", "4500000", narration="july envelopes")
    post(fx, "2026-01-05", "999900000", narration="next year, another book")
    with fx.tx(AGENT) as ctx:  # a committed draft: it exists in the file and must never be exported
        en.create_draft(ctx, dt="2025-04-01", narration="draft never posted", origin="manual", legs=pair(fx, "77000000"))
    with fx.tx(AGENT) as ctx:
        d = en.create_draft(ctx, dt="2025-04-02", narration="voided before posting", origin="manual", legs=pair(fx, "88000000"))
        en.void(ctx, d.id, expected_version=d.version)
    return fx


def export(fx, tmp: Path, **kw):
    return export_book(fx.path, tmp, year=kw.pop("year", 2025), **kw)


def ledger_of(result) -> Path:
    return Path(result.ledger_dir)


def read(result, name: str) -> str:
    return (ledger_of(result) / name).read_text(encoding="utf-8")


def state_of(fx) -> R.BookState:
    with fx.reader() as conn:
        return read_book_state(conn, start="2025-01-01", end="2025-12-31")


# 1. determinism
@pytest.mark.slow
def test_two_exports_of_the_same_book_are_byte_identical(book, tmp_path):
    a = export(book, tmp_path / "a")
    b = export(book, tmp_path / "b")
    assert a.state_hash == b.state_hash and a.manifest_sha256 == b.manifest_sha256
    names = [x.path for x in a.artifacts]
    assert names == [x.path for x in b.artifacts] and names == sorted(names)
    for name in names:
        assert (Path(a.ledger_dir) / name).read_bytes() == (Path(b.ledger_dir) / name).read_bytes(), name
    assert [x.sha256 for x in a.artifacts] == [x.sha256 for x in b.artifacts]


@pytest.mark.slow
def test_the_same_bytes_come_out_of_a_fresh_process(book, tmp_path):
    """Determinism across a process restart: no hash seed, clock, uuid or set iteration order leaks into a file."""
    mine = export(book, tmp_path / "a")
    proc = subprocess.run([str(REPO / "bin" / "haddon-export"), str(book.path), str(tmp_path / "b"), "--year", "2025"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    for artifact in mine.artifacts:
        assert (tmp_path / "b" / ENTITY_ID / "2025" / "ledger" / artifact.path).read_bytes() == \
               (Path(mine.ledger_dir) / artifact.path).read_bytes(), artifact.path
    assert mine.state_hash in proc.stdout or mine.state_hash[:12] in proc.stdout


@pytest.mark.slow
def test_re_export_reuses_the_snapshot_for_an_unchanged_state(book, tmp_path):
    a = export(book, tmp_path / "a")
    b = export(book, tmp_path / "b")
    assert a.snapshot_id == b.snapshot_id and a.snapshot_seq == b.snapshot_seq == 1  # one state is one snapshot
    assert a.export_id != b.export_id and a.operation_id != b.operation_id  # but two distinct export records
    assert book.scalar("SELECT count(*) FROM exports") == 2
    assert book.scalar("SELECT count(*) FROM snapshots") == 1


@pytest.mark.slow
def test_a_changed_book_changes_the_state_hash_and_the_bytes(book, tmp_path):
    before = export(book, tmp_path / "a")
    post(book, "2025-08-08", "10000000", narration="one more posting")
    after = export(book, tmp_path / "b")
    assert after.state_hash != before.state_hash
    assert after.counts["entries"] == before.counts["entries"] + 1
    assert read(after, "2025-haddon.journal") != read(before, "2025-haddon.journal")


# 2. ordering
@pytest.mark.slow
def test_transactions_are_ordered_by_date_then_entry_id(book, tmp_path):
    r = export(book, tmp_path / "a")
    text = read(r, "2025-haddon.journal")
    dates = [line.split(" ", 1)[0] for line in text.splitlines() if line[:1].isdigit()]
    assert dates == sorted(dates)
    state = state_of(book)
    assert [t.sort_key for t in state.transactions] == sorted(t.sort_key for t in state.transactions)
    # The two same-day entries break their tie on the entry id, in both the state and the file.
    same_day = [t for t in state.transactions if t.dt == "2025-01-15"]
    assert len(same_day) == 2 and same_day[0].entry_id < same_day[1].entry_id
    assert text.index(same_day[0].narration) < text.index(same_day[1].narration)


@pytest.mark.slow
def test_accounts_commodities_and_includes_are_byte_ordered(book, tmp_path):
    r = export(book, tmp_path / "a")
    declared = [line.split(" ", 1)[1] for line in read(r, "accounts.journal").splitlines() if line.startswith("account ")]
    assert declared == sorted(declared) and "expenses:office" in declared
    includes = [line.split(" ", 1)[1] for line in read(r, f"2025.journal").splitlines() if line.startswith("include ")]
    assert includes[0] == "accounts.journal"
    assert includes[1:] == sorted(includes[1:])
    commodities = [line for line in read(r, "2025.journal").splitlines() if line.startswith("commodity ")]
    assert commodities == ["commodity EUR 1000.00", "commodity JPY 1000.", "commodity USD 1000.00"]


def test_the_opening_group_is_included_before_the_other_groups():
    state = _synthetic(groups=("zeta", "opening", "alpha"))
    files = R.render_files(state)
    includes = [l.split(" ", 1)[1] for l in files["2025.journal"].splitlines() if l.startswith("include ")]
    assert includes == ["accounts.journal", "2025-opening.journal", "2025-alpha.journal", "2025-zeta.journal"]
    assert sorted(files) == list(files)  # the artifact list itself is byte-ordered


def _synthetic(*, groups=("opening",), narration="synthetic", memo=None, accounts=("assets:cash", "income:sales")) -> R.BookState:
    usd = Commodity("USD", 2)
    txns = tuple(
        R.RTransaction(entry_id=f"e-{i:03d}", dt="2025-01-01", narration=narration, mark="*", origin="manual", group=g,
                       postings=(R.RPosting(accounts[0], "USD", Micros(1000000), memo),
                                 R.RPosting(accounts[1], "USD", Micros(-1000000), None)))
        for i, g in enumerate(groups))
    return R.BookState(entity_id=ENTITY_ID, entity_name=COMPANY, entity_dir=ENTITY_ID, start="2025-01-01",
                       end="2025-12-31", source_revision="rev", commodities=(usd,), accounts=tuple(sorted(accounts)),
                       transactions=txns)


# 3. amounts
def test_amounts_come_from_micros_and_the_declared_precision():
    assert R.render_amount(Micros(1234560000), Commodity("USD", 2)) == "1234.56"
    assert R.render_amount(Micros(-1234560000), Commodity("USD", 2)) == "-1234.56"
    assert R.render_amount(Micros(0), Commodity("USD", 2)) == "0.00"
    assert R.render_amount(Micros(5000000000), Commodity("JPY", 0)) == "5000"
    assert R.render_amount(Micros(1), Commodity("XBT", 6)) == "0.000001"
    with pytest.raises(HaddonError) as ei:  # a value the declared precision cannot carry is refused, never rounded
        R.render_amount(Micros(1234561), Commodity("USD", 2))
    assert ei.value.code == "precision"


@pytest.mark.slow
def test_a_jpy_posting_renders_with_no_decimals(fx, tmp_path):
    with fx.tx(AGENT) as ctx:
        pv.bind_account_commodity(ctx, account_id=fx.acct("expenses:office"), commodity="JPY")
        en.post_entry(ctx, dt="2025-05-05", narration="tokyo taxi", origin="manual", legs=[
            en.Leg(fx.acct("assets:cash-jpy"), "JPY", m("-4000000000")),
            en.Leg(fx.acct("expenses:office"), "JPY", m("4000000000"))])
    r = export(fx, tmp_path / "a")
    text = read(r, "2025-haddon.journal")
    assert "JPY  4000\n" in text and "JPY -4000\n" in text and "4000.00" not in text  # no decimals, ever
    assert "commodity JPY 1000." in read(r, "2025.journal")


@pytest.mark.slow
def test_amount_and_account_columns_are_a_function_of_the_file(book, tmp_path):
    r = export(book, tmp_path / "a")
    posting_lines = [l for l in read(r, "2025-haddon.journal").splitlines() if l.startswith("    ")]
    widths = {l.index(" USD ") for l in posting_lines}
    assert len(widths) == 1  # every posting in the file lines up on the same column
    assert all(l.startswith("    ") for l in posting_lines)


# 4. posted only
@pytest.mark.slow
def test_drafts_and_voids_never_appear(book, tmp_path):
    r = export(book, tmp_path / "a")
    whole = "\n".join(read(r, a.path) for a in r.artifacts)
    assert "draft never posted" not in whole and "voided before posting" not in whole
    assert book.scalar("SELECT count(*) FROM entries WHERE status='draft'") == 1  # they are really in the file
    assert book.scalar("SELECT count(*) FROM entries WHERE status='void'") == 1
    assert r.counts["entries"] == 4 and r.counts["postings"] == 8


@pytest.mark.slow
def test_a_reversed_original_is_exported_beside_its_reversal(book, tmp_path):
    original = post(book, "2025-09-01", "500000000", narration="overstated invoice")
    with book.tx(AGENT) as ctx:
        co.reverse_entry(ctx, original_id=original.id, dt="2025-09-02",
                         reason="the C2a fixture reverses one entry to prove the export renders it")
    r = export(book, tmp_path / "a")
    text = read(r, "2025-haddon.journal")
    assert text.count("overstated invoice") == 2  # the original and its opposite entry, both posted
    assert "USD  500.00" in text and "USD -500.00" in text  # the original and its exact opposite
    assert r.counts["entries"] == 6


# 5. range scope
@pytest.mark.slow
def test_only_the_requested_range_is_exported(book, tmp_path):
    r2025 = export(book, tmp_path / "a", year=2025)
    assert "next year, another book" not in read(r2025, "2025-haddon.journal")
    assert all(a.path.startswith(("2025", "accounts")) for a in r2025.artifacts)
    r2026 = export(book, tmp_path / "a", year=2026)
    assert Path(r2026.ledger_dir).name == "ledger" and Path(r2026.ledger_dir).parent.name == "2026"
    assert "next year, another book" in read(r2026, "2026-haddon.journal")
    assert r2026.counts["entries"] == 1 and r2025.counts["entries"] == 4


# 6. the strict gate
def test_an_undeclared_account_fails_the_strict_check_and_publishes_nothing(book, tmp_path):
    """The export's own output goes through ``hledger check --strict``, which catches a chart missing an account
    it posts to."""
    used = state_of(book).transactions[0].postings[0].account
    drop = lambda s: replace(s, accounts=tuple(a for a in s.accounts if a != used))
    with pytest.raises(ExportBlocked) as ei:
        export(book, tmp_path / "a", _mutate=drop)
    assert used in ei.value.error.stderr and "has not been declared" in ei.value.error.stderr
    assert isinstance(ei.value.error, HledgerError) and ei.value.error.returncode != 0
    assert not (tmp_path / "a").exists() or list((tmp_path / "a").rglob("*.journal")) == []
    assert book.scalar("SELECT count(*) FROM exports") == 0  # and nothing was recorded
    assert book.scalar("SELECT count(*) FROM snapshots") == 0


@pytest.mark.slow
def test_a_missing_commodity_directive_fails_the_strict_check(book, tmp_path):
    """Probe on the artifact itself: strip the ``commodity`` directive the export emits and the pinned binary
    refuses the journal, so emitting it is load-bearing."""
    r = export(book, tmp_path / "a")
    main = ledger_of(r) / "2025.journal"
    Hledger().check_strict(main)  # as exported: clean
    main.write_text("\n".join(l for l in main.read_text().splitlines() if not l.startswith("commodity ")) + "\n")
    with pytest.raises(HledgerError) as ei:
        Hledger().check_strict(main)
    assert "has not been declared" in ei.value.stderr


@pytest.mark.slow
def test_the_strict_check_uses_the_pinned_binary(book, tmp_path):
    r = export(book, tmp_path / "a")
    assert r.verifier["version"].startswith("hledger 1.52.1") and r.verifier["result"] == "pass"
    assert r.verifier["binary_sha256"] == read_pin(DEFAULT_PIN) == sha256_file(DEFAULT_BINARY)
    assert r.verifier["command"] == ["check", "--strict"] and r.verifier["journal"] == "2025.journal"
    assert Path(DEFAULT_BINARY) == REPO / "vendor" / "hledger"


# 7. artifacts
@pytest.mark.slow
def test_the_manifest_hashes_the_published_bytes(book, tmp_path):
    import hashlib

    from haddon.domain.canonical import canonical_json, sha256_hex
    r = export(book, tmp_path / "a")
    for artifact in r.artifacts:
        raw = (ledger_of(r) / artifact.path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == artifact.sha256 and len(raw) == artifact.bytes
    assert verify_artifacts(ledger_of(r), r.artifacts) == []
    assert r.manifest_sha256 == sha256_hex(canonical_json(r.manifest))
    assert r.manifest["snapshot"] == {"id": r.snapshot_id, "seq": r.snapshot_seq, "state_hash": r.state_hash}
    assert r.manifest["counts"] == r.counts and r.manifest["verifier"] == r.verifier
    # A manually posted book has no import revision; a sourced one carries it (test_export_corpus_books).
    assert r.manifest["source_revision"] == "" and r.manifest["ledger_dir"] == f"{ENTITY_ID}/2025/ledger"


@pytest.mark.slow
def test_a_tampered_artifact_is_named_by_the_manifest_check(book, tmp_path):
    r = export(book, tmp_path / "a")
    victim = ledger_of(r) / "2025-haddon.journal"
    victim.write_text(victim.read_text().replace("march rent", "march RENT"))
    problems = verify_artifacts(ledger_of(r), [a.as_json() for a in r.artifacts])
    assert len(problems) == 1 and problems[0].startswith("2025-haddon.journal: sha256 ")
    victim.unlink()
    assert verify_artifacts(ledger_of(r), r.artifacts) == ["2025-haddon.journal: missing"]
    (ledger_of(r) / "2025-haddon.journal").write_text("x")
    (ledger_of(r) / "2025-extra.journal").write_text("stray\n")
    problems = verify_artifacts(ledger_of(r), r.artifacts)
    assert any("2025-extra.journal: present on disk but not in the manifest" == p for p in problems)


@pytest.mark.slow
def test_a_publish_that_loses_bytes_is_an_artifact_mismatch(book, tmp_path, monkeypatch):
    """If publishing ever wrote something other than what was verified, the export fails before recording a row."""
    from haddon.exporting import exporter as X

    real_publish = X._publish

    def corrupting_publish(staging, ledger_dir, artifacts, *, label):
        real_publish(staging, ledger_dir, artifacts, label=label)
        (ledger_dir / artifacts[0].path).write_text("tampered after verification\n")

    monkeypatch.setattr(X, "_publish", corrupting_publish)
    with pytest.raises(ArtifactMismatch) as ei:
        export(book, tmp_path / "a")
    assert artifact_name(ei) in str(ei.value) and "sha256" in str(ei.value)
    assert book.scalar("SELECT count(*) FROM exports") == 0


def artifact_name(ei) -> str:
    return str(ei.value).splitlines()[1].split(":")[0]


# 8. destination
@pytest.mark.slow
def test_an_existing_export_is_not_overwritten_without_saying_so(book, tmp_path):
    r = export(book, tmp_path / "a")
    with pytest.raises(HaddonError) as ei:
        export(book, tmp_path / "a")
    assert ei.value.details[0]["reason"] == "exists"
    again = export(book, tmp_path / "a", overwrite=True)
    assert verify_artifacts(ledger_of(again), again.artifacts) == [] and again.state_hash == r.state_hash


@pytest.mark.slow
def test_a_file_the_export_does_not_manage_stops_it(book, tmp_path):
    r = export(book, tmp_path / "a")
    (ledger_of(r) / "notes.md").write_text("hand-written\n")
    with pytest.raises(HaddonError) as ei:
        export(book, tmp_path / "a", overwrite=True)
    assert ei.value.details[0]["reason"] == "unmanaged_files" and "notes.md" in str(ei.value)
    assert (ledger_of(r) / "notes.md").exists()  # and it is still there, untouched


def test_the_read_only_checkout_and_the_pinned_binary_are_protected_roots(book, tmp_path, monkeypatch):
    """The predicate is checked against the real paths (purely: no write is ever aimed at them, not even by a
    mutation probe), and the refusal it drives is exercised against a stand-in root under tmp_path.

    The source checkout is not a literal in the exporter (issue 0006): the corpus declares which trees are
    read-only, so this repository's manifest is what puts ``corpus/example`` on the list.  The vendored binary
    stays unconditional, because it is this repository's own.
    """
    from haddon.exporting import exporter as X
    from haddon.importing.corpus import load_corpus

    roots = X.protected_roots()
    assert X.ALWAYS_PROTECTED == (REPO / "vendor",)
    assert set(load_corpus().read_only_roots) <= set(roots)
    sample = REPO / "corpus" / "example"
    for real in (sample, sample / "ExampleInc", sample / "ExampleInc" / "2025" / "ledger"):
        assert X.protected_root_for(real) == sample, real
    assert X.protected_root_for(REPO / "vendor" / "hledger") == REPO / "vendor"
    assert X.protected_root_for(tmp_path) is None and X.protected_root_for(REPO / "corpus" / "examples") is None
    guarded = tmp_path / "guarded"
    monkeypatch.setattr(X, "protected_roots", lambda: (guarded,))
    with pytest.raises(HaddonError) as ei:
        export(book, guarded)
    assert ei.value.details[0]["reason"] == "protected_root"
    assert not guarded.exists()  # refused before anything is created


def test_the_verifier_stays_protected_when_no_corpus_is_configured(monkeypatch, tmp_path):
    """A broken or absent manifest must make the guard smaller, never absent.

    An export run in a process with no corpus still protects the vendored binary, because that root does not
    come from a manifest.  A manifest that fails to parse falls back to the same answer rather than to an empty
    list, which would be a guard that disappears exactly when configuration is wrong.
    """
    from haddon.exporting import exporter as X
    from haddon.importing.corpus import CORPUS_ENV

    monkeypatch.delenv(CORPUS_ENV, raising=False)
    assert X.protected_roots() == X.ALWAYS_PROTECTED
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(CORPUS_ENV, str(broken))
    assert X.protected_roots() == X.ALWAYS_PROTECTED
    assert X.protected_root_for(REPO / "vendor" / "hledger") == REPO / "vendor"


def test_is_managed_names_exactly_the_files_the_export_owns():
    assert is_managed("2025.journal", label="2025") and is_managed("accounts.journal", label="2025")
    assert is_managed("2025-opening.journal", label="2025") and is_managed("2025-tax-adjustments.journal", label="2025")
    for other in ("2024.journal", "2024-first-bank.journal", "notes.md", "2025-opening.journal.bak", ".hidden"):
        assert not is_managed(other, label="2025"), other


# 9. refusals
def test_content_a_journal_cannot_carry_is_refused_not_mangled():
    for bad in ("has ; a comment", "has \n a newline", " leading space", "trailing space "):
        with pytest.raises(R.UnrenderableContent):
            R.transaction_header(_synthetic(narration=bad).transactions[0])
    star = _synthetic().transactions[0]
    with pytest.raises(R.UnrenderableContent):
        R.transaction_header(replace(star, mark="", narration="* not a mark"))
    assert R.transaction_header(replace(star, mark="", narration="plain")) == "2025-01-01 plain"
    assert R.transaction_header(replace(star, mark="!", narration="pending")) == "2025-01-01 ! pending"
    for bad in ("assets:two  spaces", "assets:tab\there", "assets:semi;colon", " assets:lead"):
        with pytest.raises(R.UnrenderableContent):
            R.check_account(bad)
    for bad in ("US D", "1USD", 'U"D', "US;D"):
        with pytest.raises(R.UnrenderableContent):
            R.render_symbol(bad)
    assert R.render_symbol("USD") == "USD"


def test_a_source_path_that_is_not_a_group_file_is_refused():
    assert R.group_for("ExampleInc/2025/ledger/2025-tax-adjustments.journal") == "tax-adjustments"
    # A source group is `<year>-<group>.journal` with a plain lowercase group.  The year is the source's own and
    # a journal is continuous, so a group file from any year is legitimate and only the shape is refused.
    for bad in ("ExampleInc/2025/ledger/2025.journal", "ExampleInc/2025/ledger/accounts.journal",
                "x/2025-../../etc.journal", "x/2025-FirstBank.journal", "x/2025- spaced.journal"):
        with pytest.raises(R.UnrenderableContent):
            R.group_for(bad)


@pytest.mark.slow
def test_a_multi_line_memo_becomes_indented_comment_lines(fx, tmp_path):
    with fx.tx(AGENT) as ctx:
        en.post_entry(ctx, dt="2025-02-02", narration="with notes", origin="manual",
                      legs=pair(fx, "1000000", memo="first line\nsecond line\n"))
    r = export(fx, tmp_path / "a")
    text = read(r, "2025-haddon.journal")
    assert "  ; first line" in text and "\n        ; second line\n" in text
    assert R.memo_lines("only\n") == ["only"] and R.memo_lines("\nlead\n") == ["", "lead"] and R.memo_lines(None) == []
    Hledger().check_strict(ledger_of(r) / "2025.journal")  # and it still parses


def test_an_empty_book_has_nothing_to_export(fx, tmp_path):
    with pytest.raises(R.UnrenderableContent) as ei:
        export(fx, tmp_path / "a", year=2026)
    assert "no posted entry" in str(ei.value)


# 10. the record
@pytest.mark.slow
def test_the_export_row_is_immutable_and_carries_the_acting_principal(book, tmp_path):
    r = export(book, tmp_path / "a", actor="human:auditor")
    row = book.q("SELECT id, entity_id, created_by, snapshot_id, operation_id, manifest_sha256 FROM exports")
    assert row == [(r.export_id, book.entity_id, "human:auditor", r.snapshot_id, r.operation_id, r.manifest_sha256)]
    assert book.q("SELECT created_by FROM snapshots") == [("human:auditor",)]
    con = sqlite3.connect(book.path)
    try:
        for sql in (f"UPDATE exports SET manifest_sha256='{'0' * 64}'", "DELETE FROM exports"):
            with pytest.raises(sqlite3.IntegrityError) as ei:
                con.execute("BEGIN IMMEDIATE")
                con.execute(sql)
            assert "immutable" in str(ei.value)
            con.rollback()
    finally:
        con.close()


@pytest.mark.slow
def test_the_operation_and_the_event_tell_the_same_story(book, tmp_path):
    import json
    r = export(book, tmp_path / "a")
    [(kind, state, counts, result)] = book.q("SELECT kind, state, counts, result FROM operations")
    assert (kind, state) == ("export", "succeeded")
    assert json.loads(counts) == r.counts and json.loads(result)["export_id"] == r.export_id
    [(seq, ev_kind, subject_kind, subject_id, after, reason)] = book.q(
        "SELECT seq, kind, subject_kind, subject_id, after, reason FROM events WHERE kind='export.completed'")
    assert (seq, ev_kind, subject_kind, subject_id) == (r.event_seq, "export.completed", "export", r.export_id)
    assert json.loads(after) == r.manifest and "verified by the pinned hledger binary" in reason
    [(manifest, report, checks)] = book.q("SELECT manifest, report, checks FROM snapshots")
    assert json.loads(manifest)["state_hash"] == r.state_hash and json.loads(checks) == r.verifier
    assert json.loads(report) == r.counts


@pytest.mark.slow
def test_an_export_writes_no_real_anywhere(book, tmp_path):
    from test_no_float_reaches_db import sweep_for_reals
    export(book, tmp_path / "a")
    sweep = sweep_for_reals(book.path)
    assert sweep["offenders"] == [] and {"exports", "snapshots", "operations"} <= set(sweep["nonempty"])


@pytest.mark.slow
def test_the_cli_writes_the_manifest_outside_the_managed_directory(book, tmp_path):
    manifest = tmp_path / "manifest.json"
    proc = subprocess.run([str(REPO / "bin" / "haddon-export"), str(book.path), str(tmp_path / "out"),
                           "--year", "2025", "--manifest", str(manifest)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    import json
    body = json.loads(manifest.read_text())
    ledger = tmp_path / "out" / ENTITY_ID / "2025" / "ledger"
    assert verify_artifacts(ledger, body["artifacts"]) == []  # the manifest describes exactly what is on disk
    assert sorted(p.name for p in ledger.iterdir()) == sorted(a["path"] for a in body["artifacts"])
    assert "manifest.json" not in [p.name for p in ledger.iterdir()]


# An export is a range, not a year (issue 0002)
@pytest.mark.slow
def test_export_book_exports_an_arbitrary_range_and_refuses_a_contradictory_one(tmp_path, imported_bv):
    """``--year`` is shorthand for a calendar-year range and nothing more.

    A quarter, an open-ended range and the whole journal are all exportable, and each names itself.  ``year``
    together with explicit bounds is refused rather than silently resolved: a command that accepted both would
    have to pick one, and the file it wrote could be labelled by the other.
    """
    from haddon.exporting.exporter import export_book

    db = tmp_path / "bv.db"
    shutil.copy(imported_bv.path, db)

    quarter = export_book(db, tmp_path / "q2", start="2025-04-01", end="2025-06-30")
    assert (quarter.start, quarter.end) == ("2025-04-01", "2025-06-30")
    assert (tmp_path / "q2" / "ExampleBV" / "2025-04-01_2025-06-30" / "ledger" / "2025-04-01_2025-06-30.journal").is_file()

    whole = export_book(db, tmp_path / "all")
    assert whole.start <= "2025-01-01" and whole.end >= "2025-12-31"
    assert whole.counts["entries"] >= quarter.counts["entries"] > 0

    open_start = export_book(db, tmp_path / "tail", start="2025-07-01")
    assert open_start.start == "2025-07-01" and open_start.end == whole.end

    with pytest.raises(HaddonError) as ei:
        export_book(db, tmp_path / "both", year=2025, start="2025-04-01")
    assert ei.value.details[0]["reason"] == "range_conflict"


def test_the_export_cli_refuses_a_year_and_a_range_together():
    from haddon.exporting.cli import main as export_main

    with pytest.raises(SystemExit) as ei:
        export_main(["db", "out", "--year", "2025", "--from", "2025-04-01"])
    assert ei.value.code == 2
