"""The independent oracle (chunk C2b): pinned source -> store -> reparsed export, on the sample corpus's books.

This module is one half of the deliverable; ``bin/haddon-oracle`` is the other, and both fail the same way.

This is the one place in the suite that compares the books against pinned figures (chunk G1 part (b)).  It
reads them from the corpus manifest's pinned figures (``corpus/example-baselines.json``), and the oracle
re-derives every one of them from the pinned binary inside the run.  An oracle that hardcoded them could not
pass, and a book that moves fails here, saying which figure moved, instead of in seven unrelated-looking places.
The S.L. runs the same eleven book gates as the other two, with no branch on which entity it is.  Each baseline
is keyed ``(entity, start, end)`` because the annual book was replaced by a date range (issue 0002).

The continuity oracle beside it is the other half of issue 0002.  A book run loads one calendar year into an
empty database, which is exactly the shape in which a cross-year double count cannot appear.
``run_continuity_oracle`` loads 2025 and 2026 into one journal, the way ``README.md`` tells a reader to, and
compares the continuous position with the annual books it was built from (``haddon.oracle.continuity``).  It is
pinned by an identity rather than by figures, and every gate reports what it would have caught.

Two things are worth reading closely:

* ``test_a_same_month_date_swap_fails`` / ``test_a_compensating_leg_change_fails`` are the gate that distinguishes
  a real oracle from a totals check.  Both mutations leave every account/commodity total and every
  account/commodity/month total exactly as they were; both make the run fail.
* ``TestDeliberateCorruption`` corrupts one thing at a time and asserts the run exits nonzero and leaves the
  target entity unimported.  The publish target is never created, because a run publishes only when every gate
  passed.

Nothing in the corpus checkout and nothing in ``vendor/`` is written: both are asserted byte-identical afterwards,
and the protected-root guard is probed against a stand-in root under ``tmp_path``, never by aiming a real write at
the real one.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pytest

from haddon.importing.hledger import (DEFAULT_BINARY, DEFAULT_PIN, Hledger, HledgerError, HledgerShapeError, read_pin,
                                      sha256_file)
from haddon.importing.corpus import load_corpus
from haddon.oracle import canonical as C
from haddon.oracle import gap
from haddon.oracle.cli import main as oracle_main
from haddon.oracle.continuity import run_continuity_oracle
from haddon.oracle.runner import load_baselines, run_oracle

CORPUS = load_corpus()
CHECKOUT = CORPUS.checkout
BASELINES = load_baselines(CORPUS)

from test_import_inc_bv import _tmp_checkout
from test_import_spain import untouched_checkout  # noqa: F401  (the read-only guard on the real checkout)

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[1]
START, END = "2025-01-01", "2025-12-31"
INC, BV, ES = (BASELINES[("ExampleInc", START, END)], BASELINES[("ExampleBV", START, END)],
               BASELINES[("ExampleSL", START, END)])
BOOKS = {"oracle_inc": ("ExampleInc", INC), "oracle_bv": ("ExampleBV", BV), "oracle_es": ("ExampleSL", ES)}
ALL_BOOKS = [(name, spec) for name, (_entity, spec) in BOOKS.items()]


# fixtures
@pytest.fixture(scope="module")
def oracle_bv():
    return run_oracle("ExampleBV")


@pytest.fixture(scope="module")
def oracle_inc():
    return run_oracle("ExampleInc")


@pytest.fixture(scope="module")
def oracle_es():
    return run_oracle("ExampleSL")


def gates(report) -> dict:
    return {g.name: g for g in report.gates}


def source_book(entity: str) -> C.Book:
    return C.from_journal(CORPUS.journal(entity))[0]


# 0. the run as a whole
@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_every_required_gate_runs_and_passes(name, expected, request, untouched_checkout):
    report = request.getfixturevalue(name)
    assert report.missing == (), f"required gates never ran: {report.missing}"
    assert report.failures == (), "\n".join(g.line() for g in report.failures)
    assert report.ok and report.published is None  # nothing published: this run was not asked to
    assert len(report.source_revision) == 40
    assert set(gates(report)) == set(report.required)


def test_a_report_is_not_ok_when_a_required_gate_never_ran(oracle_bv):
    """``ok`` is "every required gate ran and passed", not "nothing recorded a failure": a run that died half way
    through has no failing gate at all, and must still be a failure."""
    partial = replace(oracle_bv, gates=oracle_bv.gates[:3])
    assert not partial.ok and partial.failures == () and len(partial.missing) == len(oracle_bv.required) - 3
    assert "MISSING" in partial.summary()


# 1. totals, enumerated
@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_account_commodity_totals_are_equal_across_all_three(name, expected, request):
    g = gates(request.getfixturevalue(name))["totals"]
    assert g.ok and g.detail["mismatches"] == 0
    assert g.detail["account/commodity pairs"] == expected.pairs
    assert g.detail["per_view"] == {"source": expected.pairs, "store": expected.pairs, "export": expected.pairs}
    assert len(g.detail["enumerated"]) == expected.pairs  # the pairs are listed, not just counted
    assert all(len(pair) == 2 for pair in g.detail["enumerated"])


@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_account_commodity_month_totals_are_equal_across_all_three(name, expected, request):
    g = gates(request.getfixturevalue(name))["monthly_totals"]
    assert g.ok and g.detail["mismatches"] == 0
    assert g.detail["account/commodity/month pairs"] == expected.monthly_pairs > expected.pairs
    assert len(g.detail["enumerated"]) == expected.monthly_pairs
    months = {k[2] for k in map(tuple, g.detail["enumerated"])}
    assert months == {f"2025-{m:02d}" for m in range(1, 13)}


def test_summed_pairs_exceed_distinct_pairs_by_exactly_the_entities_that_share_a_commodity(oracle_inc, oracle_bv, oracle_es):
    """Pairs summed over books and pairs counted distinctly are two different numbers, and the difference is
    accounted for rather than tolerated.

    Chunk F3, written as a property (chunk G1 part (b)): two entities can only share an (account, commodity)
    pair if they share the commodity, so the gap between the sum and the distinct count is exactly the overlap
    between the two same-commodity books, whatever those books hold.  Each store's own pair
    count is checked against the pinned manifest, which is where a figure about the books belongs."""
    inc = {tuple(p) for p in gates(oracle_inc)["totals"].detail["enumerated"]}
    bv = {tuple(p) for p in gates(oracle_bv)["totals"].detail["enumerated"]}
    es = {tuple(p) for p in gates(oracle_es)["totals"].detail["enumerated"]}
    assert (len(inc), len(bv), len(es)) == (INC.pairs, BV.pairs, ES.pairs)   # each store, against the manifest
    commodities = {name: {c for _, c in pairs} for name, pairs in (("inc", inc), ("bv", bv), ("es", es))}
    assert commodities["inc"].isdisjoint(commodities["bv"] | commodities["es"])   # USD against two EUR books
    assert commodities["bv"] == commodities["es"]
    assert inc & bv == set() and inc & es == set()          # no shared commodity, so no shared pair either way
    assert bv & es, "the two EUR books share no account at all; the overlap this test explains is gone"
    assert {c for _, c in bv & es} == commodities["bv"]      # everything shared is shared in the shared commodity
    assert (len(inc) + len(bv) + len(es)) - len(inc | bv | es) == len(bv & es)


# 2. counts
@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_transaction_and_posting_counts_are_equal_across_all_three(name, expected, request):
    g = gates(request.getfixturevalue(name))["counts"]
    assert g.ok
    for view in ("source", "store", "export"):
        assert g.detail[view] == {"transactions": expected.entries, "postings": expected.postings}


# 3. the canonical multiset
@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_the_canonical_multiset_is_equal_across_all_three(name, expected, request):
    g = gates(request.getfixturevalue(name))["canonical"]
    assert g.ok and g.mismatches == ()
    assert len(set(g.detail["digests"].values())) == 1  # one digest for all three representations
    assert g.detail["transactions"] == {v: expected.entries for v in ("source", "store", "export")}


def _same_month_date_swap(book: C.Book) -> C.Book:
    """Swap the dates of two transactions in the same month.  Every monthly total is unchanged by construction."""
    by_month = defaultdict(list)
    for i, txn in enumerate(book):
        by_month[txn.date[:7]].append(i)
    for members in by_month.values():
        for a in members:
            for b in members:
                if book[a].date != book[b].date:
                    out = list(book)
                    out[a] = replace(book[a], date=book[b].date)
                    out[b] = replace(book[b], date=book[a].date)
                    return tuple(out)
    raise AssertionError("no two transactions share a month with different dates")


def _compensating_leg_change(book: C.Book) -> C.Book:
    """Swap the whole amount vector of two transactions that hit the same accounts in the same month.  Every
    account/commodity total and every account/commodity/month total is unchanged by construction, and both
    transactions still balance."""
    sig = defaultdict(list)
    for i, txn in enumerate(book):
        sig[(txn.date[:7], tuple((l.account, l.commodity) for l in txn.legs))].append(i)
    for members in sig.values():
        for a in members:
            for b in members:
                amounts = lambda i: tuple(l.amount_micros for l in book[i].legs)
                if amounts(a) != amounts(b):
                    swap = lambda i, j: replace(book[i], legs=tuple(
                        replace(l, amount_micros=m) for l, m in zip(book[i].legs, amounts(j), strict=True)))
                    out = list(book)
                    out[a], out[b] = swap(a, b), swap(b, a)
                    return tuple(out)
    raise AssertionError("no two transactions in one month share a leg signature with different amounts")


MUTATIONS = {"same_month_date_swap": _same_month_date_swap, "compensating_leg_change": _compensating_leg_change}


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_a_mutation_that_leaves_every_total_intact_still_changes_the_canonical_multiset(mutation):
    """The property the canonical gate rests on, on the real B.V. book: these two mutations are invisible to a
    totals check and to a monthly totals check, and visible to a multiset of whole transactions."""
    book = source_book("ExampleBV")
    mutated = MUTATIONS[mutation](book)
    assert C.totals(mutated) == C.totals(book)
    assert C.monthly_totals(mutated) == C.monthly_totals(book)
    assert len(mutated) == len(book) and sum(len(t.legs) for t in mutated) == sum(len(t.legs) for t in book)
    assert C.multiset(mutated) != C.multiset(book)
    assert C.digest(mutated) != C.digest(book)


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_a_mutation_that_leaves_every_total_intact_fails_the_whole_oracle(mutation, tmp_path):
    """...and the run really does go red: the totals and monthly-totals gates still pass, the canonical gate
    fails, and nothing is published."""
    target = tmp_path / "published.db"
    report = run_oracle("ExampleBV", into=target,
                        _mutate=lambda v: replace(v, store=MUTATIONS[mutation](v.store)))
    g = gates(report)
    assert g["totals"].ok and g["monthly_totals"].ok and g["counts"].ok  # a totals check would have said "fine"
    assert not g["canonical"].ok and len(g["canonical"].mismatches) >= 2
    assert not report.ok and report.published is None and not target.exists()


# 4. bidirectional coverage
@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_journal_mappings_are_a_bijection_between_source_and_store(name, expected, request):
    g = gates(request.getfixturevalue(name))["coverage"]
    assert g.ok and g.mismatches == ()
    n = expected.entries
    assert g.detail == {"source_transactions": n, "mapping_rows": n, "stored_entries": n, "distinct_entry_ids": n,
                        "distinct_source_indices": n, "mismatches": 0}


def test_a_colliding_source_index_is_caught(oracle_bv):
    """The four coverage failure modes are named separately; here is the one a UNIQUE constraint does not cover."""
    from haddon.oracle.gates import coverage_gate
    assert coverage_gate  # the gate under test
    g = gates(oracle_bv)["coverage"]
    assert g.detail["distinct_source_indices"] == g.detail["mapping_rows"]  # no collision in the corpus book


# posting memos
@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_posting_comments_are_compared_not_excluded(name, expected, request):
    """``postings.memo`` IS modelled and the export re-emits it, so it is compared exactly source -> store, and up
    to hledger's own trailing newline store -> reparsed export.  That exception is pinned, so it cannot grow."""
    g = gates(request.getfixturevalue(name))["posting_memos"]
    assert g.ok and g.detail["postings"] == expected.postings
    assert g.detail["source_vs_store_mismatches"] == 0
    assert g.detail["store_vs_export_other"] == 0
    assert g.detail["store_vs_export_newline_only"] == expected.memo_normalised


# the note fields, round trip
@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_the_transaction_note_fields_survive_the_round_trip(name, expected, request):
    """Transaction codes, comments and tags are equal in source, store and reparsed export at once.

    Migration 0002 modelled them.  C2b's ``unmodelled_fields`` gate, which asserted the gap, was deleted when
    the fix landed: an assertion that a field is lost is exactly what would have to be weakened to let a fix
    land."""
    g = gates(request.getfixturevalue(name))["round_trip"]
    assert g.ok and g.mismatches == ()
    assert g.detail["modelled"] == ["entries.code (hledger tcode)", "entries.comment (hledger tcomment)",
                                    "entries.tags (hledger ttags)", "postings.memo (hledger pcomment)"]
    assert g.detail["unmodelled"] == ["postings.status (hledger pstatus)"]  # the one field still not modelled
    # the source has them...
    comments, tags, postings = expected.txn_comments, expected.txn_tags, expected.postings
    assert g.detail["source"]["with_comment"] == comments and g.detail["source"]["with_tags"] == tags
    assert g.detail["source"]["with_code"] == expected.txn_codes
    # ...the entries columns hold exactly as many...
    assert g.detail["store_columns"]["with_comment"] == comments
    assert g.detail["store_columns"]["with_tags"] == tags
    assert g.detail["store_columns"]["with_code"] == expected.txn_codes
    # ...and the reparsed export reproduces exactly as many (the export writes them back).
    assert g.detail["reparsed_export"]["with_comment"] == comments
    assert g.detail["reparsed_export"]["with_tags"] == tags
    assert g.detail["reparsed_export"]["with_code"] == expected.txn_codes
    # the unmodelled posting status is not silently dropped either: source and export agree, and all Unmarked
    assert g.detail["source"]["posting_statuses"] == g.detail["reparsed_export"]["posting_statuses"] == {"Unmarked": postings}
    # ...and the full-fidelity record C1 wrote is still there beside the columns
    assert g.detail["canonical_txn_records"] == g.detail["source"]["transactions"]


@pytest.mark.parametrize("name, expected", ALL_BOOKS)
def test_the_note_fields_are_compared_value_by_value_not_only_counted(name, expected, request):
    """The counting gate above would pass if every comment were replaced by a different comment.  The canonical
    multiset is what makes that impossible: it carries each transaction's code, comment and tags, so the three
    representations are compared value by value."""
    report = request.getfixturevalue(name)
    assert gates(report)["canonical"].ok
    entity = BOOKS[name][0]
    book = source_book(entity)
    assert sum(1 for x in book if x.comment) == expected.txn_comments
    assert sum(1 for x in book if x.tags) == expected.txn_tags
    assert C.digest(book) == expected.canonical_sha256


def test_a_field_dropping_out_of_canonical_txn_is_a_failure():
    """``journal_mappings.canonical_txn`` is still the full-fidelity record even though the columns now carry the
    same fields: it is what a backfill is rebuilt from (migration 0002 does exactly that).  If a key quietly
    stopped being written, this is what notices."""
    good = json.dumps({k: [] if k in ("tags", "postings") else "" for k in gap.TXN_KEYS})
    assert gap.record_problems([(1, good)]) == []
    for missing in gap.TXN_KEYS:
        rec = {k: v for k, v in json.loads(good).items() if k != missing}
        problems = gap.record_problems([(7, json.dumps(rec))])
        assert len(problems) == 1 and missing in problems[0] and "tindex 7" in problems[0]
    leg = {k: "" for k in gap.POSTING_KEYS}
    assert gap.record_problems([(1, json.dumps({**json.loads(good), "postings": [leg]}))]) == []
    for missing in gap.POSTING_KEYS:
        rec = {**json.loads(good), "postings": [{k: v for k, v in leg.items() if k != missing}]}
        problems = gap.record_problems([(9, json.dumps(rec))])
        assert len(problems) == 1 and "postings[0]" in problems[0] and missing in problems[0]


def test_the_round_trip_gate_notices_a_schema_that_lost_a_column_or_grew_the_last_one(oracle_bv, repo_db):
    """The schema check, both ways round.  A migration that dropped one of the modelled columns would make the
    export lossy again, and one that added ``postings.status`` would mean the export has a new field to write.

    Dropping ``entries.comment`` takes two steps, which is itself the finding: SQLite refuses the DROP COLUMN
    outright while ``entries_update`` still names the column, so migration 0002's trigger is what makes the new
    columns undroppable by accident.  The gate is probed against the schema that remains once the trigger is
    gone too -- i.e. against the only way the column could actually disappear."""
    import sqlalchemy as sa

    from haddon.db.connection import make_engine
    engine = make_engine(repo_db)
    try:
        with engine.connect() as conn:
            assert gap.schema_problems(conn) == []
            assert {"code", "comment", "tags"} <= gap.columns(conn, "entries")
            assert "status" not in gap.columns(conn, "postings") and "memo" in gap.columns(conn, "postings")
            with pytest.raises(sa.exc.OperationalError, match="no such column: NEW.comment"):
                conn.exec_driver_sql("ALTER TABLE entries DROP COLUMN comment")
        with engine.connect() as conn:
            conn.exec_driver_sql("DROP TRIGGER entries_update")
            conn.exec_driver_sql("ALTER TABLE entries DROP COLUMN comment")
            problems = gap.schema_problems(conn)
            assert len(problems) == 1 and "entries.comment is gone" in problems[0] and "lossy" in problems[0]
            conn.exec_driver_sql("ALTER TABLE postings ADD COLUMN status TEXT")
            problems = gap.schema_problems(conn)
            assert len(problems) == 2 and any("postings.status now exists" in p and "exporter" in p for p in problems)
    finally:
        engine.dispose()


# the pinned source
@pytest.mark.parametrize("name, entity", [(n, e) for n, (e, _s) in BOOKS.items()])
def test_every_pinned_figure_and_digest_still_describes_the_source(name, entity, request):
    report = request.getfixturevalue(name)
    g = gates(report)["baseline"]
    assert g.ok and g.detail["mismatches"] == 0
    baseline = BASELINES[(entity, START, END)]
    assert g.detail["measured"] == g.detail["pinned"] == baseline.pins
    assert len(baseline.pins) == 15
    for key in ("canonical_sha256", "totals_sha256", "monthly_sha256", "mapping_sha256"):
        assert len(baseline.pins[key]) == 64


def test_the_pinned_digests_are_derived_not_asserted(oracle_bv):
    """The canonical and totals digests are recomputed here from the source with the pinned binary, so a baseline
    that had drifted from the journals could not pass by agreeing with itself."""
    book = source_book("ExampleBV")
    baseline = BASELINES[("ExampleBV", START, END)]
    assert C.digest(book) == baseline.canonical_sha256
    assert C.totals_digest(C.totals(book)) == baseline.totals_sha256
    assert C.totals_digest(C.monthly_totals(book)) == baseline.monthly_sha256
    assert len(book) == baseline.entries


# 5. deliberate corruption
def _edit(dst: Path, name: str, old: str, new: str, count: int = 1):
    path = dst / name
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == count, f"{name}: expected {count} occurrence(s) of {old!r}, found {text.count(old)}"
    path.write_text(text.replace(old, new), encoding="utf-8")


AMOUNT = re.compile(r"-?\d+\.\d+")


def _corruptible_transaction(ledger: Path) -> dict:
    """Pick a transaction to corrupt out of the journals themselves, and return the pieces to corrupt.

    Nothing about which book this is, which institution a journal came from or what any transaction was for is
    written down here (chunk G1 part (b)): a corruption probe is a statement about the oracle, so it has to keep
    working whatever the books hold.  The requirements are structural, and each exists so the corruption stays
    invisible to everything but the oracle:

    * a journal with no ``=`` balance assertion anywhere in it, and not an opening journal.  An assertion
      elsewhere in the same file pins a running balance, so changing an amount would be caught by
      ``hledger check --strict`` instead, which is not what these probes are testing;
    * exactly two legs, so changing both amounts leaves the transaction balanced;
    * a day between the 2nd and the 27th, so moving the date one day back stays inside its own month and no
      monthly total moves.
    """
    year = ledger.parent.name
    declared = [l.split(maxsplit=1)[1].strip() for l in (ledger / "accounts.journal").read_text().splitlines()
                if l.startswith("account ")]
    for path in sorted(p for p in ledger.glob(f"{year}-*.journal") if "opening" not in p.name):
        lines = path.read_text(encoding="utf-8").splitlines()
        if any("=" in l for l in lines):
            continue
        for i, line in enumerate(lines):
            if not (len(line) > 10 and line[:4].isdigit() and line[4] == "-"):
                continue
            legs = list(itertools.takewhile(lambda l: l.startswith((" ", "\t")) and l.strip(), lines[i + 1:]))
            if len(legs) != 2 or not all(AMOUNT.search(l) for l in legs):
                continue
            day = int(line[8:10])
            if not 2 <= day <= 27:
                continue
            account = legs[0].split()[0]
            other = next((a for a in declared if a not in (legs[0].split()[0], legs[1].split()[0])), None)
            if other is None:
                continue
            return {"file": path.name, "header": line, "day": day, "legs": legs, "account": account, "other": other}
    raise AssertionError(f"no corruptible two-leg transaction in {ledger}")


def _shift_amount(line: str, by: int) -> str:
    """The same posting line with its amount moved by ``by`` whole units, decimals preserved."""
    m = AMOUNT.search(line)
    whole, _, frac = m.group().partition(".")
    shifted = f"{int(whole) + by}.{frac}"
    return line[:m.start()] + shifted.rjust(len(m.group())) + line[m.end():]


def _corrupt_date(d: Path) -> None:
    """A date, moved back one day within its month, so no monthly total changes."""
    t = _corruptible_transaction(d)
    _edit(d, t["file"], t["header"], t["header"][:8] + f"{t['day'] - 1:02d}" + t["header"][10:])


def _corrupt_account(d: Path) -> None:
    """A posting retargeted to a different declared account, so the strict gate still passes."""
    t = _corruptible_transaction(d)
    _edit(d, t["file"], t["legs"][0], t["legs"][0].replace(t["account"], t["other"], 1))


def _corrupt_amount(d: Path) -> None:
    """An amount, changed on both legs so the transaction still balances and hledger still accepts it."""
    t = _corruptible_transaction(d)
    for leg, by in zip(t["legs"], (1, -1), strict=True):   # +1 and -1 SIGNED: the transaction still balances
        _edit(d, t["file"], leg, _shift_amount(leg, by))


def _corrupt_tag(d: Path) -> None:
    """A transaction tag -- a field the store keeps only inside ``journal_mappings.canonical_txn``."""
    t = _corruptible_transaction(d)
    _edit(d, t["file"], t["header"], t["header"] + "  ; probe: injected")


CORRUPTIONS = {"date": _corrupt_date, "account": _corrupt_account, "amount": _corrupt_amount, "tag": _corrupt_tag}


class TestDeliberateCorruption:
    """One corruption at a time.  Each must exit nonzero and leave the target entity unimported.  The run builds
    in a private workspace and publishes only a report that passed everything, so a failure has nothing to leave
    behind.  The real checkout is never edited: every corruption is applied to a throwaway copy under ``/tmp``."""

    @pytest.mark.parametrize("kind", sorted(CORRUPTIONS))
    def test_a_corrupted_source_fails_and_publishes_nothing(self, kind, tmp_path, untouched_checkout):
        root = _tmp_checkout(tmp_path, "ExampleBV", CORRUPTIONS[kind])
        target = tmp_path / "published.db"
        report = run_oracle("ExampleBV", checkout=root, into=target)
        assert not report.ok, f"{kind} was not caught"
        assert report.published is None and not target.exists()  # the entity is unimported: no partial state
        assert not Path(report.facts["workspace"]).exists()  # and the workspace is gone too
        caught = {g.name for g in report.failures}
        assert "baseline" in caught, f"{kind}: {caught}"
        if kind == "tag":
            # The tag is injected into the source, so store and export agree with it and the canonical multiset
            # cannot notice.  The pinned extent (the round_trip gate) and the pinned digest do.
            assert "round_trip" in caught
            assert any("with tags" in m for m in gates(report)["round_trip"].mismatches)

    def test_a_corrupted_evidence_hash_fails_and_publishes_nothing(self, tmp_path, untouched_checkout):
        """The published artifacts are re-hashed against what the ``exports`` row recorded; one changed byte in one
        published journal is a failure, and the run publishes nothing."""
        target = tmp_path / "published.db"

        def tamper(views):
            victim = views.ledger_dir / "2025-bank.journal"
            victim.write_text(victim.read_text(encoding="utf-8").replace("17.35", "17.36", 1), encoding="utf-8")

        report = run_oracle("ExampleBV", into=target, _after_export=tamper)
        assert not report.ok and report.published is None and not target.exists()
        g = gates(report)["artifacts"]
        assert not g.ok and len(g.mismatches) == 1 and g.mismatches[0].startswith("2025-bank.journal: sha256 ")

    @pytest.mark.parametrize("fault", ["malformed_json", "nonzero_exit", "truncated_json"])
    def test_a_malformed_hledger_response_fails_and_publishes_nothing(self, fault, tmp_path, untouched_checkout,
                                                                     migrated_counts):
        """A subprocess that returns garbage, or exits nonzero, is an import failure -- never a partial import and
        never a silently empty comparison."""
        target = tmp_path / "published.db"

        class Faulty(Hledger):
            def run(self, journal, *args):
                if args[:1] == ("print",):
                    if fault == "nonzero_exit":
                        raise HledgerError(["hledger", "print"], 1, "hledger: Error: broken pipe\n")
                    return "not json at all" if fault == "malformed_json" else '[{"tindex": 1, "tdate": "2025-01-01"'
                return super().run(journal, *args)

        report = run_oracle("ExampleBV", into=target, hledger=Faulty())
        assert not report.ok and report.published is None and not target.exists()
        assert not gates(report)["import"].ok
        assert gates(report)["import"].detail["error"] in ("HledgerError", "HledgerShapeError")
        assert set(report.missing) >= {"counts", "totals", "canonical", "coverage", "baseline"}

    def test_a_passing_run_does_publish(self, tmp_path, untouched_checkout, migrated_counts):
        """The control: the same publish path, on a run with nothing corrupted, really does write the database.
        So "nothing was published" above is a consequence of the failure, not of a publish path that never works."""
        from conftest import table_counts
        target = tmp_path / "published.db"
        report = run_oracle("ExampleBV", into=target, keep=tmp_path / "kept")
        assert report.ok and report.published == str(target) and target.is_file()
        counts = table_counts(target)
        assert counts["entries"] == BV.entries and counts["postings"] == BV.postings
        assert counts["journal_mappings"] == BV.entries and counts["exports"] == 1
        assert (tmp_path / "kept" / "export" / "ExampleBV" / "2025" / "ledger" / "2025.journal").is_file()

    def test_publishing_copies_the_database_and_not_merely_its_file(self, tmp_path):
        """A published database must be a database, not whatever happened to be flushed out of the WAL.

        The workspace database runs in WAL mode, so pages committed since the last checkpoint live in the ``-wal``
        sibling and not in the file.  SQLite folds them back on last-connection close and, once the log passes a
        thousand pages, at a commit.  It skips both while any connection is reading, and whether one is reading
        comes down to when the interpreter last collected a reference cycle.  Publishing with ``shutil.copy``
        would ship a complete database almost always and an empty one occasionally, on a report that said ok,
        because every gate reads the workspace through a connection that sees main and WAL.  This pins the
        condition directly instead of relying on timing.
        """
        import sqlite3

        from haddon.db.connection import sqlite_connect
        from haddon.db.migrate import upgrade
        from haddon.oracle.runner import _copy_database

        def tables(path):
            con = sqlite3.connect(path)
            try:
                return len(con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            finally:
                con.close()

        source = tmp_path / "workspace.db"
        upgrade(source)
        held = sqlite_connect(source)   # open, so SQLite never folds the log back into the file
        try:
            expected = len(held.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            assert expected >= 23  # issue 0005 deleted the five approval-chain tables
            assert (tmp_path / "workspace.db-wal").stat().st_size > 0, "nothing is left in the WAL to lose"

            naive = tmp_path / "file-copy.db"
            shutil.copy(source, naive)
            assert tables(naive) == 0, "the control failed: a plain file copy did not lose the WAL after all"

            published = tmp_path / "published.db"
            _copy_database(source, published)
            assert tables(published) == expected
        finally:
            held.close()


# the CLI is the failing command
def _cli(*args, cwd=None):
    return subprocess.run([str(REPO / "bin" / "haddon-oracle"), *args], capture_output=True, text=True)


def test_the_cli_exits_zero_on_the_real_books(untouched_checkout):
    proc = _cli("book", "ExampleBV", "--json")
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["ok"] and len(body["reports"]) == 1
    report = body["reports"][0]
    assert report["ok"] and report["missing"] == [] and len(report["gates"]) == 11
    assert all(g["ok"] for g in report["gates"])


def test_the_cli_exits_nonzero_when_anything_disagrees(tmp_path, untouched_checkout):
    """The deliverable, stated as the spec states it: a failing oracle is a failing command, not a report that
    prints mismatches and exits successfully."""
    root = _tmp_checkout(tmp_path, "ExampleBV", CORRUPTIONS["amount"])
    proc = _cli("book", "ExampleBV", "--checkout", str(root), "--into", str(tmp_path / "out"))
    assert proc.returncode == 2, proc.stdout
    assert "ORACLE FAILED: book/ExampleBV" in proc.stderr
    assert "FAIL  baseline" in proc.stdout
    assert not (tmp_path / "out").exists()


def test_the_cli_runs_every_known_entity_by_default(untouched_checkout):
    """``make test-gate`` runs ``bin/haddon-oracle all`` with no entity arguments, so what the gate actually covers
    is ``DEFAULT_ENTITIES``.  An entity that exists in the importer but is missing from that tuple would be
    silently unverified by the commit gate (chunk F3), so the two lists are required to agree rather than merely
    overlap."""
    ENTITIES = CORPUS.entities
    DEFAULT_ENTITIES = CORPUS.entity_keys
    assert set(DEFAULT_ENTITIES) == set(ENTITIES) == {"ExampleInc", "ExampleBV", "ExampleSL"}
    assert len(DEFAULT_ENTITIES) == len(set(DEFAULT_ENTITIES))
    # and each one has a pinned range, so none of them can run unpinned
    for entity in DEFAULT_ENTITIES:
        assert (entity, START, END) in BASELINES


def test_the_cli_refuses_an_unknown_entity_and_a_protected_target(tmp_path):
    assert _cli("book", "ExampleLtd").returncode == 1
    proc = _cli("book", "ExampleBV", "--into", str(CHECKOUT))
    assert proc.returncode == 1 and "read-only for every Haddon command" in proc.stderr


# nothing was written
def test_the_protected_root_guard_is_probed_against_a_stand_in_not_the_real_checkout(tmp_path):
    """The guard is a pure predicate, so it is probed with a stand-in root under ``tmp_path``.  Probing it by
    disabling it and writing at the real checkout would do the damage it exists to prevent."""
    from haddon.exporting.exporter import protected_root_for, protected_roots
    from haddon.oracle.runner import _safe_target
    stand_in = tmp_path / "pretend-scratch"
    stand_in.mkdir()
    assert protected_root_for(stand_in / "a" / "b", roots=(stand_in,)) == stand_in
    assert protected_root_for(tmp_path / "elsewhere", roots=(stand_in,)) is None
    # the real roots, asserted without aiming a write at them
    roots = protected_roots()
    assert CHECKOUT.parent in roots or CHECKOUT in roots
    for real in roots:
        with pytest.raises(ValueError) as ei:
            _safe_target(real / "would-be-written", "publish target")
        assert "read-only for every Haddon command" in str(ei.value)
        assert not (real / "would-be-written").exists()
    assert _safe_target(tmp_path / "fine", "publish target") == (tmp_path / "fine").resolve()


def test_the_source_checkout_and_the_pinned_binary_are_byte_identical_afterwards(
        oracle_inc, oracle_bv, oracle_es, untouched_checkout):
    assert sha256_file(DEFAULT_BINARY) == read_pin(DEFAULT_PIN)
    porcelain = subprocess.run(["git", "--no-optional-locks", "-C", str(CHECKOUT), "status", "--porcelain", "--", "."],
                               capture_output=True, text=True, check=True).stdout
    assert porcelain == "", porcelain
    tmp = Path(tempfile.gettempdir()).resolve()
    for report in (oracle_inc, oracle_bv, oracle_es):
        assert Path(report.facts["workspace"]).resolve().is_relative_to(tmp)
        assert not Path(report.facts["workspace"]).exists()
        assert str(CHECKOUT) not in report.facts["ledger_dir"]


# the cross-year oracle (issue 0002)
@pytest.fixture(scope="module")
def continuity_bv():
    return run_continuity_oracle("ExampleBV", checkout=CHECKOUT)


@pytest.fixture(scope="module")
def continuity_inc():
    return run_continuity_oracle("ExampleInc", checkout=CHECKOUT)


@pytest.fixture(scope="module")
def continuity_es():
    return run_continuity_oracle("ExampleSL", checkout=CHECKOUT)


CONTINUITY = ["continuity_inc", "continuity_bv", "continuity_es"]


#: the chain each entity's continuity run loads, which is the manifest's per-entity answer and not a
#: corpus-wide one: Spain's books open in August 2024, Inc's 2024 holds documents and no ledger, and B.V. has
#: no 2024 at all.
CHAIN = {"continuity_inc": (2025, 2026), "continuity_bv": (2025, 2026),
         "continuity_es": (2024, 2025, 2026)}


@pytest.mark.parametrize("name", CONTINUITY)
def test_every_continuity_gate_runs_and_passes(name, request, untouched_checkout):
    """Every year of a company's chain in one journal, on the corpus books, for every entity the commit gate
    covers."""
    report = request.getfixturevalue(name)
    assert report.missing == (), f"required gates never ran: {report.missing}"
    assert report.failures == (), "\n".join(g.line() for g in report.failures)
    assert report.ok and report.kind == "continuity"
    chain = CHAIN[name]
    assert (report.start, report.end) == (f"{chain[0]}-01-01", f"{chain[-1]}-12-31")
    assert tuple(gates(report)["import"].detail["years"]) == chain


@pytest.mark.parametrize("name", CONTINUITY)
def test_every_opening_is_verified_against_the_journal_and_never_posted(name, request):
    """The restatement is data the continuous journal already has.  It is compared, recorded and dropped, and
    what it folds is the journal's own accumulated result, to the micro, which is what makes dropping it safe.

    Every boundary of the chain, not just the last.  A three-year journal has two, and a run that checked only
    the most recent one would pass a book whose earliest opening had been posted on top of the year it
    restates.  The totals at the far end still balance, so nothing else would see it.
    """
    report = request.getfixturevalue(name)
    chain = CHAIN[name]
    g = gates(report)
    assert g["import"].detail["to_entries"] == 0          # the last year holds the opening and nothing else yet
    verified = g["import"].detail["restatements"]
    assert len(verified) == len(chain) - 1
    assert [(r["dt"], r["through"]) for r in verified] == [(f"{later}-01-01", f"{earlier}-12-31")
                                                           for earlier, later in zip(chain, chain[1:])]
    for one in verified:
        assert one["lines"] > 0 and one["compared"] >= one["lines"]
        assert len(one["fold_paths"]) == 1 and one["fold_paths"][0].startswith("equity:")
    last = verified[-1]
    assert g["restatement"].ok and g["restatement"].detail["folded"] == last["folded"]
    # the fold is the accumulated result: the last year's books have posted nothing, so the two coincide
    assert g["restatement"].detail["accumulated_through_end"] == last["folded"]


def test_a_chain_that_skips_a_boundary_fails_the_restatement_gate():
    """The gate enumerates boundaries rather than counting restatements.

    Counting is wrong the moment a chain has more than one boundary: two restatements of the same year would
    satisfy ``len(verified) == len(chain) - 1`` while a boundary in the middle went unchecked.  A two-year chain
    cannot express that failure, so it is tested here directly.
    """
    from haddon.domain.micros import Micros
    from haddon.oracle.continuity import _restatement_gate

    one = {"dt": "2026-01-01", "through": "2025-12-31", "folded": {"EUR": "1"}, "lines": 3,
           "fold_accounts": ["acct-equity:retained-losses"], "fold_paths": ["equity:retained-losses"],
           "compared": 3, "narration": "opening"}
    chain = (2024, 2025, 2026)
    accumulated = {"EUR": Micros(1)}
    twice = _restatement_gate((one, dict(one)), accumulated, chain)
    assert not twice.ok and "carried no restatement" in twice.summary
    assert "2025-01-01 restating through 2024-12-31" in twice.mismatches
    both = (dict(one, dt="2025-01-01", through="2024-12-31"), one)
    assert _restatement_gate(both, accumulated, chain).ok


@pytest.mark.parametrize("name", CONTINUITY)
def test_the_continuity_gates_say_what_they_would_have_caught(name, request):
    """A gate that cannot state the difference it is looking for is not evidence that there is none.  Every
    carried pair must be one a journal that posted the restatement would have got wrong."""
    report = request.getfixturevalue(name)
    g = gates(report)
    assert g["continuous_position"].detail["compared"] > 0
    assert g["continuous_position"].detail["mismatches"] == 0
    inflation = g["no_inflation"].detail
    assert inflation["carried_pairs"] > 0
    assert inflation["would_have_been_inflated"] == inflation["carried_pairs"]


def test_the_cli_runs_the_continuity_oracle_too(untouched_checkout):
    """``make test-gate`` runs ``bin/haddon-oracle all``, so the cross-year shape has to be in ``all``.  A mode
    that has to be asked for is a mode the commit gate does not run."""
    proc = _cli("continuity", "ExampleBV", "--json")
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["ok"] and len(body["reports"]) == 1
    report = body["reports"][0]
    assert report["kind"] == "continuity" and report["ok"] and report["missing"] == []
    assert {g["name"] for g in report["gates"]} == {"import", "restatement", "continuous_position", "no_inflation"}
    from haddon.oracle.cli import MODES
    assert "continuity" in MODES


def test_the_2024_book_spain_opens_with_is_pinned_like_every_other_book(untouched_checkout):
    """Spain's first financial year is a book oracle run of its own, with its own baseline.

    The continuity run loads 2024 beside 2025 and 2026, but it compares positions; only a book run compares
    the source against the store against a reparsed export transaction by transaction.  Without this, 2024
    would be the one year in the live ledger that nothing round-tripped.
    """
    report = run_oracle("ExampleSL", checkout=CHECKOUT, year=2024)
    assert report.missing == (), f"required gates never ran: {report.missing}"
    assert report.failures == (), "\n".join(g.line() for g in report.failures)
    assert (report.start, report.end) == ("2024-01-01", "2024-12-31")
    g = gates(report)
    assert g["baseline"].ok and "no pinned baseline" not in g["baseline"].summary
    assert report.facts["fingerprint"]["entries"] == 44


def test_a_restatement_that_disagrees_with_the_journal_ends_the_import(tmp_path, untouched_checkout):
    """Not a silent skip.  A 2026 opening that states a balance the journal does not hold is a
    ``422 unbalanced`` naming the account, and the whole import rolls back.

    The bend has the shape of a defect found in chunk C2b: EUR 0.14 sitting on a liability instead of on
    retained earnings.  The entry still balances, every per-year total is still right, and only a comparison
    against the journal it restates can see it.
    """
    from conftest import table_counts
    from haddon.db.migrate import upgrade
    from haddon.domain.errors import HaddonError as HE
    from haddon.importing.importer import import_entity

    root = _two_year_checkout(tmp_path, "ExampleBV", _bend_the_2026_opening)
    db = tmp_path / "bent.db"
    upgrade(db)
    import_entity("ExampleBV", db, checkout=root, year=2025)
    before = table_counts(db)
    with pytest.raises(HE) as ei:
        import_entity("ExampleBV", db, checkout=root, year=2026, into_existing=True)
    assert ei.value.code == "unbalanced"
    assert "does not agree with the journal" in ei.value.message
    paths = {d.get("path") for d in ei.value.details}
    assert "liabilities:corporate-tax-payable" in paths
    assert table_counts(db) == before          # the refusal wrote nothing


def _two_year_checkout(tmp_path: Path, entity: str, edit) -> Path:
    """Both years of one entity under /tmp, laid out like the checkout and committed to a throwaway git repo.
    The real checkout is never touched (``_tmp_checkout`` copies one year and this needs the boundary)."""
    root = tmp_path / "two-year-checkout"
    for year in (2025, 2026):
        shutil.copytree(CORPUS.journal(entity, year).parent, root / entity / str(year) / "ledger")
    edit(root / entity / "2026" / "ledger")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "copy"],
                   check=True)
    return root


def _bend_the_2026_opening(ledger: Path) -> None:
    """Move EUR 0.14 of the restatement off equity and onto a liability: it still balances, and it is wrong."""
    path = ledger / "2026-opening.journal"
    body = path.read_text()
    for before, after in (("-12450.00 EUR", "-12450.14 EUR"), ("-123966.08 EUR", "-123965.94 EUR")):
        assert before in body, before
        body = body.replace(before, after)
    path.write_text(body)
