"""The transaction-level note fields, and the round trip they close (chunk E1).

``entries.code`` / ``comment`` / ``tags`` let the store hold, and the export write back, hledger's
transaction-level notes.  This module covers four parts:

* The schema and the record.  The columns ship NOT NULL with empty defaults, a posted entry's notes are
  immutable, and the columns are rebuildable from ``journal_mappings.canonical_txn``.  Migration 0002 backfilled
  them from that record; what it guaranteed is asserted here against the schema as it ships, since the squash
  deleted the migration.
* The repository: what may be stored, what is refused, and that the tag order survives.
* The export.  Every rendering claim is checked by reparsing the export with the pinned binary, never by reading
  the manual: a code, a comment on the transaction line, a comment above the legs, an empty comment line, and a
  tag that hledger re-derives from the comment text.
* The corpus books.  Inc and B.V. imported from the sample corpus journals, with the note extents the oracle's
  manifest pins present in the columns and reproduced in the reparsed export.  No figure about those books is
  written down here; the module reads the corpus's pinned figures (chunk G1 part (b)).

The last section is mutation probes: each breaks one load-bearing claim in place and asserts that a specific
named check goes red.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from haddon.db.connection import make_engine, sqlite_connect
from haddon.db.migrate import upgrade
from haddon.domain.canonical import canonical_tags
from haddon.domain.errors import HaddonError
from haddon.exporting import render as R
from haddon.exporting.exporter import export_book, read_book_state
from haddon.importing.hledger import Hledger
from haddon.importing.importer import import_entity
from haddon.oracle import canonical as C
from haddon.oracle import gap
from haddon.oracle.runner import load_baselines

BASELINES = load_baselines()
from haddon.repository import entries as en

from repo_helpers import AGENT, pair

NOTE_COLUMNS = ("code", "comment", "tags")


def note_rows(path: Path) -> dict[str, tuple[str, str, str]]:
    con = sqlite3.connect(path)
    try:
        return {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT id, code, comment, tags FROM entries")}
    finally:
        con.close()


def counted(rows: dict[str, tuple[str, str, str]]) -> dict[str, int]:
    return {"with_code": sum(1 for c, _, _ in rows.values() if c),
            "with_comment": sum(1 for _, m, _ in rows.values() if m),
            "with_tags": sum(1 for _, _, t in rows.values() if t != "[]")}


def reparsed(journal: Path) -> list:
    return Hledger().print_json(journal)


def notes_of(txns) -> list[tuple]:
    return sorted((t.date, t.description, t.code, t.comment, tuple(t.tags)) for t in txns)


# 1. the note columns, as the schema ships them
def test_the_note_columns_are_appended_text_not_null_with_defaults(migrated_template):
    """``code``, ``comment`` and ``tags`` are TEXT NOT NULL with empty defaults.

    The defaults made the column addition (migration 0002) legal over a populated book, and an entry written
    without note fields still has to land on the empty forms rather than on NULL."""
    con = sqlite3.connect(migrated_template)
    try:
        info = {r[1]: r for r in con.execute('PRAGMA table_info("entries")')}
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name='entries'").fetchone()[0]
        triggers = [r[0] for r in con.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'")]
    finally:
        con.close()
    assert [c for c in info][-3:] == list(NOTE_COLUMNS)  # appended, in the order tables.py declares them
    for column, default in (("code", "''"), ("comment", "''"), ("tags", "'[]'")):
        assert info[column][2] == "TEXT" and info[column][3] == 1, column  # TEXT NOT NULL
        assert info[column][4] == default, column                          # with a default, so old rows are legal
    assert sql.rstrip().endswith("STRICT")
    assert triggers == [65]


def test_a_posted_entrys_note_fields_are_immutable_in_the_trigger_text(migrated_template):
    """The immutability rule covers the note fields, not written around them.

    Read off the trigger that ships: all three columns are named in the same immutability conjunction as the
    other immutable fields."""
    con = sqlite3.connect(migrated_template)
    try:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name='entries_update'").fetchone()[0]
    finally:
        con.close()
    for column in NOTE_COLUMNS:
        assert f"NEW.{column} IS OLD.{column}" in sql, column
    assert "-- E1:" in sql  # the reason the conjuncts are there, still attached to them


def test_tags_must_be_json_at_the_database_level(fx):
    """``json_valid`` is a column CHECK, so even a direct write cannot put a non-JSON tag list in the store.  The
    same INSERT without the bad tags is the positive control, so the refusal is the CHECK and not the triggers."""
    con = sqlite_connect(fx.path)
    stmt = ("INSERT INTO entries(id,entity_id,created_at,created_by,dt,narration,origin,tags) "
            f"VALUES(?,'{fx.entity_id}','t','who','2025-01-01','n','manual',?)")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="json_valid"):
            con.execute(stmt, ("x", "not json"))
        con.execute(stmt, ("y", '[["a","b"]]'))
    finally:
        con.close()


# 2. the note columns are rebuildable from the record
# ``journal_mappings.canonical_txn`` is the full-fidelity record the importer writes (chunk C1), and the columns
# are filled from the same import.  Migration 0002 backfilled the columns from it; the two must still say the same
# thing.  These tests state that guarantee against the schema and the importer at head.
def test_a_record_that_cannot_supply_the_note_fields_is_refused():
    """A ``canonical_txn`` the columns cannot be rebuilt from is an error, not three empty columns.

    Empty is the one wrong answer that cannot be spotted afterwards.  ``''`` and ``'[]'`` are legitimate values
    meaning "this transaction has none", so a silently emptied column reads exactly like a transaction that
    never had a comment.  The four shapes are the four ways the record can fail to carry the fields."""
    good = json.dumps({"code": "C", "comment": "c\n", "tags": [["a", "b"]]})
    assert gap.note_fields_of(good, entry_id="e") == {"code": "C", "comment": "c\n", "tags": '[["a","b"]]'}
    for bad, message in ((json.dumps({"code": "C", "comment": ""}), "missing ['tags']"),
                         (json.dumps({"code": 1, "comment": "", "tags": []}), "not strings"),
                         (json.dumps({"code": "", "comment": "", "tags": ["a"]}), "not a list of [name, value]"),
                         ("{not json", "not JSON")):
        with pytest.raises(RuntimeError) as ei:
            gap.note_fields_of(bad, entry_id="e-7")
        assert message in str(ei.value) and "e-7" in str(ei.value)


def test_the_rebuilt_tags_are_byte_identical_to_the_stored_ones_not_merely_equal(fx):
    """Tags are rendered with the importer's own canonical_json, so a rebuilt row and a freshly imported one
    compare equal as bytes.  A rebuild that produced ``[["a", "b"]]`` would be the same tags and a different
    column, and every state hash over the book would move."""
    tags = [("zulu", "1"), ("alpha", "2")]
    with fx.tx(AGENT) as ctx:
        e = en.post_entry(ctx, dt="2025-03-08", narration="rebuilt", origin="manual", legs=pair(fx, "1000000"),
                          comment="zulu: 1, alpha: 2\n", tags=tags)
    stored = fx.scalar("SELECT tags FROM entries WHERE id=?", (e.id,))
    record = json.dumps({"code": "", "comment": "zulu: 1, alpha: 2\n", "tags": [list(p) for p in tags]})
    assert gap.note_fields_of(record, entry_id=e.id)["tags"] == stored == '[["zulu","1"],["alpha","2"]]'


def test_an_entry_with_no_mapping_is_not_something_the_rebuild_claims_about(fx):
    """The rebuild speaks only for entries that came from a journal.  An entry minted in the store has no
    ``journal_mappings`` row and no record to be compared against.  It must not be reported as a disagreement,
    which a join written the other way round would do."""
    with fx.tx(AGENT) as ctx:
        en.post_entry(ctx, dt="2025-03-09", narration="no mapping", origin="manual", legs=pair(fx, "1000000"),
                      code="LOCAL-1", comment="minted here\n")
    with fx.reader() as conn:
        assert gap.rebuild_problems(conn, fx.entity_id) == []


@pytest.mark.slow
@pytest.mark.parametrize("entity", ["ExampleInc", "ExampleBV"])
def test_the_real_books_note_columns_are_exactly_what_the_record_says(round_tripped, entity):
    """The invariant itself, on the corpus books: for every imported entry, the columns the export renders from
    and the record they would be rebuilt from agree, field for field.

    Neither the counting gate nor the oracle makes this check.  ``round_trip`` compares counts of the columns
    against the source and ``canonical_gate`` compares values against the source, each copy separately.
    Neither compares the store's two copies against each other, so a book whose columns and records had both
    drifted the same way, or whose backfill had picked the wrong mapping row, would pass both.  The count line
    below stops this passing on a book with no notes in it; it comes from the manifest, not from this file."""
    baseline = BASELINES[(entity, "2025-01-01", "2025-12-31")]
    db, _ = round_tripped[entity]
    engine = make_engine(db)
    try:
        with engine.connect() as conn:
            entity_id = conn.execute(sa.text("SELECT id FROM entities")).scalar_one()
            assert gap.rebuild_problems(conn, entity_id) == []
    finally:
        engine.dispose()
    assert sum(baseline.note_counts.values()) > 0, "a book with no notes would pass this vacuously"


# 3. the repository: store and refuse
def test_the_note_fields_round_trip_through_the_repository(fx):
    tags = [("period", "01-12-2024"), ("raw", "PAYROLLCO DES:TAX")]
    with fx.tx(AGENT) as ctx:
        e = en.post_entry(ctx, dt="2025-03-02", narration="with notes", origin="manual", legs=pair(fx, "1000000"),
                          code="CHK-1", comment="period: 01-12-2024, raw: PAYROLLCO DES:TAX\n", tags=tags)
    with fx.reader() as conn:
        got = en.get_entry(conn, fx.entity_id, e.id)
    assert got.code == "CHK-1" and got.comment == "period: 01-12-2024, raw: PAYROLLCO DES:TAX\n"
    assert got.tags == tuple(tags)
    assert fx.scalar("SELECT tags FROM entries WHERE id=?", (e.id,)) == '[["period","01-12-2024"],["raw","PAYROLLCO DES:TAX"]]'


def test_an_entry_with_no_note_fields_stores_the_empty_forms(fx):
    """'' and '[]' mean "this transaction has none", hledger's own model.  NOT NULL, so every read totals."""
    with fx.tx(AGENT) as ctx:
        e = en.post_entry(ctx, dt="2025-03-03", narration="plain", origin="manual", legs=pair(fx, "1000000"))
    assert fx.q("SELECT code, comment, tags FROM entries WHERE id=?", (e.id,)) == [("", "", "[]")]
    with fx.reader() as conn:
        assert en.get_entry(conn, fx.entity_id, e.id).tags == ()


def test_tag_order_is_preserved_not_sorted(fx):
    """The order is a deterministic function of the comment and the export has to reproduce it, so it is part of
    the value.  Sorting would make a reparse of the export disagree with the store."""
    reversed_pair = [("zulu", "1"), ("alpha", "2")]
    with fx.tx(AGENT) as ctx:
        e = en.post_entry(ctx, dt="2025-03-04", narration="ordered", origin="manual", legs=pair(fx, "1000000"),
                          comment="zulu: 1, alpha: 2\n", tags=reversed_pair)
    assert fx.scalar("SELECT tags FROM entries WHERE id=?", (e.id,)) == '[["zulu","1"],["alpha","2"]]'
    with fx.reader() as conn:
        assert en.get_entry(conn, fx.entity_id, e.id).tags == tuple(reversed_pair)
    assert canonical_tags(reversed_pair) != canonical_tags(sorted(reversed_pair))


@pytest.mark.parametrize("tags", [[("", "v")], [("a:b", "v")], [("a b", "v")], [("a,b", "v")], [("a", "x\ny")],
                                 [("a",)], ["a: b"], "notalist", [("a", 1)]])
def test_a_tag_a_comment_could_not_carry_is_refused(fx, tags):
    with fx.tx(AGENT) as ctx:
        with pytest.raises(HaddonError) as ei:
            en.post_entry(ctx, dt="2025-03-05", narration="bad tags", origin="manual", legs=pair(fx, "1000000"),
                          tags=tags)
    assert ei.value.code == "validation"


def test_a_non_string_code_or_comment_is_refused(fx):
    for kw in ({"code": 1}, {"comment": ["x"]}):
        with fx.tx(AGENT) as ctx:
            with pytest.raises(HaddonError) as ei:
                en.post_entry(ctx, dt="2025-03-06", narration="bad", origin="manual", legs=pair(fx, "1000000"), **kw)
        assert ei.value.code == "validation" and ei.value.details[0]["reason"] == "string_required"


# 4. the export: every claim checked against the binary
def _reparse_one(fx, tmp_path, **kw):
    """Post one entry with the given note fields, export the book, and reparse it with the pinned binary."""
    with fx.tx(AGENT) as ctx:
        en.post_entry(ctx, dt="2025-05-05", narration=kw.pop("narration", "under test"), origin="manual",
                      legs=pair(fx, "1000000"), **kw)
    result = export_book(fx.path, tmp_path, year=2025)
    txns = reparsed(Path(result.ledger_dir) / "2025.journal")
    assert len(txns) == 1
    return txns[0], (Path(result.ledger_dir) / "2025-haddon.journal").read_text(encoding="utf-8")


@pytest.mark.slow
def test_a_transaction_code_is_written_and_reparses(fx, tmp_path):
    txn, text = _reparse_one(fx, tmp_path, code="CHK-1")
    assert "2025-05-05 * (CHK-1) under test" in text
    assert txn.code == "CHK-1" and txn.description == "under test"


@pytest.mark.parametrize("comment, tags, expected_line", [
    ("on the transaction line\n", (), "2025-05-05 * under test  ; on the transaction line"),
    ("\nabove the legs\n", (), "    ; above the legs"),
    ("\nraw: PAYROLLCO DES:TAX 243340\n", (("raw", "PAYROLLCO DES:TAX 243340"),), "    ; raw: PAYROLLCO DES:TAX 243340"),
    ("head: 1\nand more\n", (("head", "1"),), "2025-05-05 * under test  ; head: 1"),
    ("\n\nafter an empty comment line\n", (), "    ;"),
    ("\n", (), "2025-05-05 * under test  ;"),
])
@pytest.mark.slow
def test_a_comment_reparses_to_exactly_what_was_stored(fx, tmp_path, comment, tags, expected_line):
    """What hledger gives back, which the manual cannot tell us.  Each shape is written by the exporter and read
    again with the pinned binary, and ``tcomment`` must be the stored string, character for character, including
    the leading newline that means "nothing on the transaction line"."""
    txn, text = _reparse_one(fx, tmp_path, comment=comment, tags=list(tags))
    assert expected_line in text.splitlines() or expected_line in text
    assert txn.comment == comment
    assert txn.tags == tags


@pytest.mark.slow
def test_a_comment_without_a_trailing_newline_round_trips_up_to_that_newline(fx, tmp_path):
    """hledger terminates a comment it writes with a newline, exactly as it does for a posting comment (see the
    ``posting_memos`` gate).  Every comment in the corpus books already ends with one, so this is only reachable
    for an entry minted in the store."""
    txn, _ = _reparse_one(fx, tmp_path, comment="no trailing newline")
    assert txn.comment == "no trailing newline\n"


def test_a_tag_the_comment_does_not_carry_is_refused_not_dropped(fx, tmp_path):
    """hledger derives ``ttags`` by parsing the comment, so a tag with nowhere to be written cannot be exported.
    Refusing is the posture the rest of the renderer already takes for content a journal cannot carry."""
    with fx.tx(AGENT) as ctx:
        en.post_entry(ctx, dt="2025-05-06", narration="orphan tag", origin="manual", legs=pair(fx, "1000000"),
                      comment="nothing tagged here\n", tags=[("ghost", "1")])
    with pytest.raises(R.UnrenderableContent) as ei:
        export_book(fx.path, tmp_path, year=2025)
    assert "ghost" in str(ei.value) and "could not produce it" in str(ei.value)


def test_narration_and_code_content_a_journal_cannot_carry_is_refused():
    base = R.RTransaction(entry_id="e1", dt="2025-01-01", narration="plain", mark="*", origin="manual", group="g",
                          postings=())
    from dataclasses import replace
    assert R.transaction_header(base) == "2025-01-01 * plain"
    assert R.transaction_header(replace(base, code="AB-1")) == "2025-01-01 * (AB-1) plain"
    assert R.render_code("") == "" and R.render_code("x") == "(x) "
    # A narration that starts with '(' would come back as a code, so it is refused rather than mangled.
    with pytest.raises(R.UnrenderableContent, match="transaction code"):
        R.transaction_header(replace(base, narration="(not a code) narration"))
    for bad in ("has ; a comment", "has\nnewline", "paren)inside", " leading", "(wrapped)"):
        with pytest.raises(R.UnrenderableContent):
            R.transaction_header(replace(base, code=bad))


def test_a_leading_paren_narration_really_would_be_read_as_a_code(fx, tmp_path):
    """The refusal above is not a guess about hledger: the same text, written by hand, reparses as a code."""
    journal = tmp_path / "probe.journal"
    journal.write_text("commodity USD 1000.00\naccount a:b\naccount c:d\n\n"
                       "2025-01-01 * (not a code) narration\n    a:b  USD -1.00\n    c:d  USD 1.00\n", encoding="utf-8")
    txn = reparsed(journal)[0]
    assert txn.code == "not a code" and txn.description == "narration"


def test_the_state_document_carries_the_note_fields(fx, tmp_path):
    """The export's state hash must move when a comment does, or a re-export of a changed book would reuse the
    snapshot of the old one."""
    with fx.tx(AGENT) as ctx:
        en.post_entry(ctx, dt="2025-05-07", narration="hashed", origin="manual", legs=pair(fx, "1000000"),
                      comment="\nwith a comment\n")
    with fx.reader() as conn:
        state = read_book_state(conn, start="2025-01-01", end="2025-12-31")
    doc = R.state_document(state)
    assert doc["transactions"][0]["comment"] == "\nwith a comment\n"
    assert set(doc["transactions"][0]) >= {"code", "comment", "tags"}
    from dataclasses import replace
    stripped = replace(state, transactions=tuple(replace(t, comment="", tags=()) for t in state.transactions))
    assert R.state_hash(state) != R.state_hash(stripped)


# 5. the corpus books
@pytest.fixture(scope="module")
def round_tripped(tmp_path_factory):
    """Inc and B.V. imported from the corpus journals and exported once, with the exports reparsed."""
    out = {}
    for entity in ("ExampleInc", "ExampleBV"):
        base = tmp_path_factory.mktemp(f"e1-{entity}")
        db = base / f"{entity}.db"
        upgrade(db)
        import_entity(entity, db)
        result = export_book(db, base / "out", year=2025)
        out[entity] = (db, Path(result.ledger_dir) / "2025.journal")
    return out


@pytest.mark.slow
@pytest.mark.parametrize("entity", ["ExampleInc", "ExampleBV"])
def test_the_real_books_keep_their_note_fields_in_the_columns(round_tripped, entity):
    """The note extent in the ``entries`` columns, against the pinned manifest, the one place it is written."""
    baseline = BASELINES[(entity, "2025-01-01", "2025-12-31")]
    db, _ = round_tripped[entity]
    assert counted(note_rows(db)) == baseline.note_counts
    engine = make_engine(db)
    try:
        with engine.connect() as conn:
            entity_id = conn.execute(sa.text("SELECT id FROM entities")).scalar_one()
            assert gap.stored_note_counts(conn, entity_id, start="2025-01-01", end="2025-12-31") == {
                "transactions": baseline.entries, **baseline.note_counts}
    finally:
        engine.dispose()


@pytest.mark.slow
@pytest.mark.parametrize("entity", ["ExampleInc", "ExampleBV"])
def test_the_real_books_reproduce_their_note_fields_in_the_export(round_tripped, entity):
    """Source -> store -> export, on the corpus journals: every transaction's code, comment and tag list comes back
    out of the export byte for byte, read with the pinned binary.

    The comparison against the source is the assertion and needs no number.  The counts that follow stop it
    passing vacuously on a book with no notes in it, and they come from the manifest."""
    from haddon.importing.corpus import load_corpus

    baseline = BASELINES[(entity, "2025-01-01", "2025-12-31")]
    _, exported = round_tripped[entity]
    source = reparsed(load_corpus().journal(entity))
    export = reparsed(exported)
    assert notes_of(export) == notes_of(source)
    assert {"with_comment": sum(1 for t in export if t.comment),
            "with_tags": sum(1 for t in export if t.tags),
            "with_code": sum(1 for t in export if t.code)} == baseline.note_counts


# 6. mutation probes
@pytest.mark.slow
def test_probe_a_note_column_that_disagrees_with_its_record_is_caught(round_tripped, tmp_path):
    """Break it: change one real entry's stored comment so it no longer matches its ``canonical_txn``.

    This shows ``test_the_real_books_note_columns_are_exactly_what_the_record_says`` can fail: a check that
    joined no rows would return no problems and read exactly like a pass.  The trigger that makes a posted entry
    immutable is dropped on a copy to do it, because only a tampered file can reach this state."""
    source, _ = round_tripped["ExampleBV"]
    db = tmp_path / "tampered.db"
    shutil.copy(source, db)
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TRIGGER entries_update")
        entry_id = con.execute("SELECT entry_id FROM journal_mappings ORDER BY source_index LIMIT 1").fetchone()[0]
        con.execute("UPDATE entries SET comment = ? WHERE id = ?", ("not what the record says\n", entry_id))
        con.commit()
    finally:
        con.close()

    engine = make_engine(db)
    try:
        with engine.connect() as conn:
            entity_id = conn.execute(sa.text("SELECT id FROM entities")).scalar_one()
            problems = gap.rebuild_problems(conn, entity_id)
    finally:
        engine.dispose()
    assert len(problems) == 1 and entry_id in problems[0] and "two copies" in problems[0]


@pytest.mark.slow
def test_probe_a_comment_dropped_by_the_importer_is_caught(tmp_path, monkeypatch):
    """Break it: make the import store an empty comment.  The canonical comparison (source vs store) must go
    red.  That is what ``gates.canonical_gate`` compares, and what
    ``test_the_real_books_keep_their_note_fields_in_the_columns`` counts."""
    real = en.post_entry

    def without_comment(ctx, **kw):
        return real(ctx, **{**kw, "comment": ""})

    monkeypatch.setattr(en, "post_entry", without_comment)
    db = tmp_path / "bv.db"
    upgrade(db)
    import_entity("ExampleBV", db)
    monkeypatch.undo()

    engine = make_engine(db)
    try:
        with engine.connect() as conn:
            store, _ = C.from_store(conn, start="2025-01-01", end="2025-12-31")
    finally:
        engine.dispose()
    from haddon.importing.corpus import load_corpus

    source, _ = C.from_journal(load_corpus().journal("ExampleBV"))
    assert C.multiset(source) != C.multiset(store)  # the canonical gate fails
    assert counted(note_rows(db))["with_comment"] == 0  # and the column count is 0, not the pinned extent


@pytest.mark.slow
def test_probe_a_tag_dropped_by_the_export_is_caught(fx, tmp_path, monkeypatch):
    """Break it: make the renderer drop a tag from the comment it writes.  Store and reparsed export must then
    disagree.  This comparison stands in for re-implementing hledger's tag parser."""
    with fx.tx(AGENT) as ctx:
        en.post_entry(ctx, dt="2025-06-01", narration="tagged", origin="manual", legs=pair(fx, "1000000"),
                      comment="\nkeep: 1, drop: 2\n", tags=[("keep", "1"), ("drop", "2")])
    good = export_book(fx.path, tmp_path / "good", year=2025)
    assert reparsed(Path(good.ledger_dir) / "2025.journal")[0].tags == (("keep", "1"), ("drop", "2"))

    real_lines = R.comment_lines
    monkeypatch.setattr(R, "comment_lines", lambda txn: [l.replace(", drop: 2", "") for l in real_lines(txn)])
    broken = export_book(fx.path, tmp_path / "broken", year=2025)
    monkeypatch.undo()
    txn = reparsed(Path(broken.ledger_dir) / "2025.journal")[0]
    assert txn.tags == (("keep", "1"),)  # the tag is gone from the export
    with fx.reader() as conn:
        stored = en.get_entry(conn, fx.entity_id, [r[0] for r in fx.q("SELECT id FROM entries")][0])
    assert tuple(stored.tags) != txn.tags  # store != reparsed export: the canonical gate fails


@pytest.mark.slow
def test_probe_a_reordered_tag_list_is_caught(fx, tmp_path):
    """Break it: store the tags in an order the comment does not produce.  hledger re-derives them in comment
    order, so the reparsed export and the store disagree.  That is why the order is part of the stored value."""
    with fx.tx(AGENT) as ctx:
        e = en.post_entry(ctx, dt="2025-06-02", narration="ordered", origin="manual", legs=pair(fx, "1000000"),
                          comment="\nfirst: 1, second: 2\n", tags=[("second", "2"), ("first", "1")])
    result = export_book(fx.path, tmp_path, year=2025)
    txn = reparsed(Path(result.ledger_dir) / "2025.journal")[0]
    with fx.reader() as conn:
        stored = en.get_entry(conn, fx.entity_id, e.id)
    assert txn.tags == (("first", "1"), ("second", "2"))
    assert tuple(stored.tags) != txn.tags
    canonical = lambda tags: C.Txn(date="d", narration="n", status="Cleared", code="", comment="c", tags=tags, legs=())
    assert canonical((("a", "1"), ("b", "2"))) != canonical((("b", "2"), ("a", "1")))


def test_probe_a_posted_entrys_note_fields_cannot_be_changed(fx):
    """Break it at the database, which is where immutability lives: each of the three columns is refused on a
    posted entry by the same ``immutable`` guard that refuses a changed narration."""
    with fx.tx(AGENT) as ctx:
        e = en.post_entry(ctx, dt="2025-06-03", narration="sealed", origin="manual", legs=pair(fx, "1000000"),
                          code="C", comment="c\n", tags=[("t", "1")])
    con = sqlite_connect(fx.path)
    try:
        for column, value in (("code", "HACKED"), ("comment", "hacked\n"), ("tags", '[["t","2"]]')):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                con.execute(f"UPDATE entries SET {column}=?, version=version+1 WHERE id=?", (value, e.id))
        assert con.execute("SELECT code, comment, tags FROM entries WHERE id=?", (e.id,)).fetchone() == \
            ("C", "c\n", '[["t","1"]]')
    finally:
        con.close()
