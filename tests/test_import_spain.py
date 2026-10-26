"""ExampleSL: the third book, and the blocked-import path.

The S.L. imports exactly like the Inc. and the B.V., and what it imports is compared against what the pinned
binary reports for the same journal.  No count about the book is written down here (chunk G1 part (b)); the
corpus shape is pinned once, in ``corpus/example-baselines.json`` (the file the corpus manifest names).

* The S.L. imports, and no account name has a currency code welded on to the end of it.  That is the shape a
  one-space defect between an account and its amount produces when a chart is built from postings instead of
  from declarations.
* The blocked path (raise by default, ``on_blocked='skip'``, CLI exit 3/4) is proven against a stand-in
  checkout under ``tmp_path`` whose ``commodity EUR`` directives are removed, which fails ``hledger check
  --strict`` on an undeclared commodity at ``2025-opening.journal:9``.  The stand-in exists so the corpus
  checkout is never modified, and ``untouched_checkout`` asserts that byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from haddon.domain.errors import HaddonError
from haddon.importing.corpus import load_corpus
from haddon.importing.importer import BlockedResult, ImportBlocked, ImportResult, import_entity

CORPUS = load_corpus()
CHECKOUT = CORPUS.checkout

from conftest import table_counts

SPAIN = "ExampleSL"
LEDGER = CORPUS.journal(SPAIN).parent
OPENING = LEDGER / "2025-opening.journal"
ADJUSTMENTS = LEDGER / "2025-adjustments.journal"
ACCOUNTS = LEDGER / "accounts.journal"
INVOICES = LEDGER / "2025-invoices.journal"
#: How many journals of the book are opening journals: a shape of the managed layout, not of the accounts.
OPENING_JOURNALS = 1


def source_shape() -> dict[str, int]:
    """Spain's 2025 journal as the pinned binary reports it, derived per run and never written down."""
    from haddon.importing.hledger import Hledger

    txns = Hledger().print_json(CORPUS.journal(SPAIN))
    return {"entries": len(txns), "postings": sum(len(t.postings) for t in txns),
            "pairs": len({(p.account, p.amount.commodity) for t in txns for p in t.postings}),
            "accounts": len(Hledger().declared_accounts(CORPUS.journal(SPAIN)))}


def _git_status() -> str:
    # scoped to the checkout's own tree: the sample corpus lives inside this repository, whose other files are
    # somebody's work in progress and none of this guard's business
    return subprocess.run(["git", "--no-optional-locks", "-C", str(CHECKOUT), "status", "--porcelain", "--", "."],
                          capture_output=True, text=True, check=True).stdout


def _tree_digest() -> str:
    """sha256 over every journal byte of the three entity ledger directories (independent of git)."""
    h = hashlib.sha256()
    for f in sorted(CHECKOUT.glob("*/2025/ledger/*")):
        h.update(str(f.relative_to(CHECKOUT)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


@pytest.fixture
def untouched_checkout():
    before_status, before_digest = _git_status(), _tree_digest()
    assert before_status == "", before_status  # precondition: the checkout is clean to begin with
    yield
    assert _git_status() == ""
    assert _tree_digest() == before_digest


def blocked_checkout(tmp_path: Path) -> Path:
    """A stand-in checkout under ``tmp_path`` holding Spain's 2025 ledger with its ``commodity EUR`` directives
    removed: an undeclared commodity, which ``hledger check --strict`` refuses.

    No corpus entity is blocked, so the blocked path needs a stand-in, and it is a copy because the pinned
    checkout is always read-only.  The copy is committed to a throwaway git repo because the importer reads
    ``rev-parse HEAD``.
    """
    root = tmp_path / "blocked-checkout"
    dst = root / SPAIN / "2025" / "ledger"
    shutil.copytree(LEDGER, dst)
    removed = 0
    for f in sorted(dst.glob("*.journal")):
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [ln for ln in lines if not ln.startswith("commodity EUR")]
        removed += len(lines) - len(kept)
        if len(kept) != len(lines):
            f.write_text("".join(kept), encoding="utf-8")
    assert removed == 2, f"expected the two commodity EUR directives, removed {removed}"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "copy"],
                   check=True)
    return root


def q(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# the corrected source imports
@pytest.mark.slow
def test_spain_imports_like_inc_and_bv_now_that_the_source_is_corrected(repo_db, untouched_checkout):
    """Chunk F3: Spain is ``ES``/``EUR``/``S.L.`` per its own chart, and nothing in the importer is aware of which
    entity this is.  The same call, the same code path, the same shape of result as Inc and B.V."""
    r = import_entity(SPAIN, repo_db)
    assert isinstance(r, ImportResult) and r.blocked is None
    src = source_shape()
    assert (r.entries, r.postings, r.pairs) == (src["entries"], src["postings"], src["pairs"])
    assert (r.accounts, r.commodities, r.journal_mappings) == (src["accounts"], 1, src["entries"])
    assert r.origins == {"opening": OPENING_JOURNALS, "source": src["entries"] - OPENING_JOURNALS}
    assert (r.start, r.end) == ("2025-01-01", "2025-12-31") and len(r.source_revision) == 40
    # the entity's own chart decided every one of those facts; no per-company branch produced them
    assert q(repo_db, "SELECT id, country, base_currency, legal_form FROM entities") == [
        ("example-sl", "ES", "EUR", "S.L.")]
    assert q(repo_db, "SELECT code, precision FROM commodities") == [("EUR", 2)]
    src = source_shape()
    assert q(repo_db, "SELECT count(*) FROM entries") == [(src["entries"],)]
    assert q(repo_db, "SELECT count(*) FROM postings") == [(src["postings"],)]
    assert q(repo_db, "SELECT count(*) FROM events WHERE kind = 'import.blocked'") == [(0,)]
    # C1's rule is unchanged: an import neither locks nor verifies anything
    assert q(repo_db, "SELECT through FROM period_locks") == [(None,)]


@pytest.mark.slow
def test_haddon_never_invents_an_account_the_journal_did_not_declare(repo_db, untouched_checkout):
    """The chart Haddon stores is exactly the chart the journal declares: no more, no fewer, no invented names.

    A phantom account such as ``...accumulated-depreciationEUR`` can only enter the store if the chart is derived
    from what appears in postings instead of from what the book declares.  This asserts the behaviour rather than
    facts about one journal's files (chunk G1 part (b)).
    """
    from haddon.importing.hledger import Hledger

    declared = set(Hledger().declared_accounts(CORPUS.journal(SPAIN)))
    import_entity(SPAIN, repo_db)
    stored = {p for (p,) in q(repo_db, "SELECT path FROM accounts")}
    assert stored == declared and stored
    # every account a posting uses is one of those, and no account name carries a commodity code welded on to it
    used = {p for (p,) in q(repo_db, "SELECT DISTINCT a.path FROM postings p JOIN accounts a ON a.id = p.account_id")}
    assert used <= stored and used
    commodities = {c for (c,) in q(repo_db, "SELECT code FROM commodities")}
    assert not [p for p in stored for c in commodities if p.endswith(c)]


def test_every_book_in_the_pinned_checkout_passes_strict_so_none_is_blocked(untouched_checkout):
    """Haddon blocks on ``hledger check --strict`` and on nothing else, and at the pinned fixture no book fails it.

    Findings about a particular journal (for example an ``ordereddates`` failure, which ``--strict`` does not
    include) belong in the oracle's report and in ``docs/design/decisions.md`` (F3), not here (chunk G1 part
    (b)).  This is the precondition the corpus half of the suite rests on: every book in the pinned checkout is
    importable.
    """
    from haddon.importing.hledger import Hledger, HledgerError
    ENTITIES = CORPUS.entity_keys

    hl, blocked = Hledger(), {}
    for entity in ENTITIES:
        try:
            hl.check_strict(CORPUS.journal(entity))
        except HledgerError as exc:                      # the one thing that blocks an import
            blocked[entity] = str(exc)
    assert blocked == {}, blocked
    assert len(ENTITIES) > 1                             # ...and it really did check more than one book


# 7. the blocked path, on a stand-in that is blocked
@pytest.mark.slow
def test_import_entity_raises_by_default_with_the_real_hledger_error(tmp_path, repo_db, migrated_counts,
                                                                     untouched_checkout):
    root = blocked_checkout(tmp_path)
    opening = root / SPAIN / "2025" / "ledger" / "2025-opening.journal"
    with pytest.raises(ImportBlocked) as ei:
        import_entity(SPAIN, repo_db, checkout=root)
    e = ei.value
    msg = str(e)
    assert "2025-opening.journal:9" in msg
    assert 'commodity "EUR" has not been declared' in msg
    assert "Strict commodity checking is enabled" in msg
    assert "nothing imported" in msg
    assert e.entity == SPAIN and e.error.file == str(opening) and e.error.line == 9
    assert e.error.stderr.rstrip() in msg  # verbatim stderr, not a summary
    assert e.error.command[-2:] == ["check", "--strict"]
    # nothing at all was written: not even the entity row
    assert table_counts(repo_db) == migrated_counts


@pytest.mark.slow
def test_the_stand_in_reproduces_the_historical_defect_and_differs_only_in_that(tmp_path, untouched_checkout):
    """The stand-in is only evidence if it is the corpus journals minus the one thing that was corrected.  Every
    other byte is identical, and the real checkout is untouched."""
    root = blocked_checkout(tmp_path)
    dst = root / SPAIN / "2025" / "ledger"
    for real in sorted(LEDGER.glob("*.journal")):
        copy = (dst / real.name).read_text(encoding="utf-8")
        expected = "".join(l for l in real.read_text(encoding="utf-8").splitlines(keepends=True)
                           if not l.startswith("commodity EUR"))
        assert copy == expected, real.name
    assert not any(l.startswith("commodity") for f in dst.glob("*.journal") for l in f.read_text().splitlines())


@pytest.mark.slow
def test_skip_returns_a_structured_blocked_result_and_records_an_event(tmp_path, repo_db, migrated_counts,
                                                                      untouched_checkout):
    root = blocked_checkout(tmp_path)
    opening = root / SPAIN / "2025" / "ledger" / "2025-opening.journal"
    r = import_entity(SPAIN, repo_db, checkout=root, on_blocked="skip")
    assert isinstance(r, BlockedResult)
    assert r.entries == 0 and r.postings == 0 and (r.start, r.end) == ("2025-01-01", "2025-12-31")
    assert r.blocked["file"] == str(opening) and r.blocked["line"] == 9 and r.blocked["returncode"] == 1
    assert 'commodity "EUR" has not been declared' in r.blocked["stderr"]
    assert "BLOCKED" in r.summary() and "2025-opening.journal:9" in r.summary()
    # visible in the store: the entity row and one import.blocked event carrying the real error; no entries
    assert q(repo_db, "SELECT id, country, base_currency, legal_form FROM entities") == [("example-sl", "ES", "EUR", "S.L.")]
    assert q(repo_db, "SELECT count(*) FROM entries") == [(0,)] and q(repo_db, "SELECT count(*) FROM postings") == [(0,)]
    assert q(repo_db, "SELECT count(*) FROM journal_mappings") == [(0,)]
    [(seq, kind, subject_kind, subject_id, after, reason)] = q(repo_db, "SELECT seq, kind, subject_kind, subject_id, after, reason FROM events")
    assert (seq, kind, subject_kind, subject_id) == (r.event_seq, "import.blocked", "import", f"{SPAIN}/2025-01-01..2025-12-31")
    payload = json.loads(after)
    assert (payload["start"], payload["end"]) == ("2025-01-01", "2025-12-31") and payload["error"]["line"] == 9
    assert 'commodity "EUR" has not been declared' in payload["error"]["stderr"]
    assert "skipped by explicit request" in reason
    # nothing about Spain is locked or verified
    assert q(repo_db, "SELECT through FROM period_locks") == [(None,)]
    # the only rows added over an empty database are the entity, its lock row and the event
    diff = {k: v - migrated_counts[k] for k, v in table_counts(repo_db).items() if v != migrated_counts[k]}
    assert diff == {"entities": 1, "period_locks": 1, "events": 1}


@pytest.mark.slow
def test_skip_is_not_a_silent_success_second_run_is_refused(tmp_path, repo_db, untouched_checkout):
    """The refusal must come from the blocked marker (the ``import.blocked`` event and the entity row a skip
    leaves behind), not merely from "this database already holds an entity".  That would refuse a successful
    second import too, and so would prove nothing about the skip path."""
    root = blocked_checkout(tmp_path)
    first = import_entity(SPAIN, repo_db, checkout=root, on_blocked="skip")
    assert isinstance(first, BlockedResult)
    with pytest.raises(HaddonError) as ei:  # the blocked marker occupies the file; re-running cannot masquerade as success
        import_entity(SPAIN, repo_db, checkout=root, on_blocked="skip")
    assert ei.value.code == "immutable"
    assert q(repo_db, "SELECT count(*) FROM entries") == [(0,)]  # still nothing imported
    assert q(repo_db, "SELECT count(*) FROM events") == [(1,)]  # and the second run recorded nothing either


def test_on_blocked_accepts_only_raise_or_skip(repo_db):
    with pytest.raises(HaddonError) as ei:
        import_entity(SPAIN, repo_db, on_blocked="ignore")
    assert ei.value.code == "validation" and ei.value.details[0]["path"] == "on_blocked"


@pytest.mark.slow
def test_cli_exit_codes_for_spain(tmp_path, fast_dir, untouched_checkout):
    """0 on the real, corrected source; 3 and 4 on the stand-in that is still blocked."""
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run([str(repo / "bin" / "haddon-import"), SPAIN, str(fast_dir / "real.db")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    src = source_shape()
    assert f"entries={src['entries']} postings={src['postings']} pairs={src['pairs']}" in proc.stdout
    assert q(fast_dir / "real.db", "SELECT count(*) FROM entries") == [(src["entries"],)]

    root = blocked_checkout(tmp_path)
    db = fast_dir / "es.db"
    proc = subprocess.run([str(repo / "bin" / "haddon-import"), SPAIN, str(db), "--checkout", str(root)],
                          capture_output=True, text=True)
    assert proc.returncode == 3 and "BLOCKED" in proc.stderr and 'commodity "EUR" has not been declared' in proc.stderr
    assert q(db, "SELECT count(*) FROM entities") == [(0,)]
    proc = subprocess.run([str(repo / "bin" / "haddon-import"), SPAIN, str(db), "--checkout", str(root), "--skip-blocked"],
                          capture_output=True, text=True)
    assert proc.returncode == 4 and "BLOCKED at" in proc.stdout and "entries=0 postings=0" in proc.stdout
    assert q(db, "SELECT kind FROM events") == [("import.blocked",)]
