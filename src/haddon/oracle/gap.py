"""What the store models, measured, and the one field it still does not (chunks C2b, E1).

C2b found the gap: ``entries`` had no column for a transaction-level code, comment or tags, so C1 could only keep
them inside ``journal_mappings.canonical_txn`` and a reparsed export could not reproduce them.  E1 closed it.
Migration 0002 added the three columns, the import fills them, the export writes them back and
:mod:`haddon.oracle.canonical` compares them.  So this module now asserts the opposite of the gap:

1. the schema still has every column the round trip depends on (:data:`MODELLED`), so a migration that took one
   away would fail here rather than quietly make the export lossy again;
2. ``journal_mappings.canonical_txn`` still carries all of them for every stored transaction.  That record is the
   source-of-truth copy and is what a backfill would be rebuilt from.  :func:`note_fields_of` does the rebuilding
   and :func:`rebuild_problems` compares the result against the columns, so "rebuildable from the record" is a
   checked claim.  Migration 0002 guaranteed it; the squash that deleted 0002 removed the implementation, not the
   guarantee;
3. the extent is still measured on the corpus books (:func:`measure`) and compared against a pinned count, so drift
   in either direction (a comment appearing, a tag disappearing) fails the suite;
4. the ``entries`` columns hold as many of them as the source has (:func:`stored_note_counts`), and the reparsed
   export reproduces exactly the same numbers (the ``round_trip`` gate in :mod:`haddon.oracle.gates`).

One field is still unmodelled (:data:`UNMODELLED`): the posting-level status, ``pstatus``.  Every posting in the
corpus books is ``Unmarked``, so today the round trip is lossless anyway.  The ``round_trip`` gate checks that
rather than assuming it, by comparing the source's posting-status histogram against the reparsed export's.  A
journal with a ``!`` or ``*`` posting would fail that comparison, which is correct: it would be a real loss, and
closing it is another migration.  ``docs/design/decisions.md`` carries the recommendation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.canonical import canonical_json
from haddon.importing.hledger import Transaction

# (table, column, the hledger field it holds) the store does model.  A column vanishing from the schema makes the
# round trip lossy again, which is what this module exists to notice.
MODELLED = (("entries", "code", "tcode"), ("entries", "comment", "tcomment"), ("entries", "tags", "ttags"),
            ("postings", "memo", "pcomment"))
# What it still does not.  The export writes no posting status, so a journal carrying one would not round trip;
# the round_trip gate compares the source's posting statuses against the reparsed export's and fails if so.
UNMODELLED = (("postings", "status", "pstatus"),)
# Every key ``journal_mappings.canonical_txn`` must carry, at the transaction and the posting level.
TXN_KEYS = ("date", "description", "code", "comment", "tags", "status", "journal_path", "source_index", "postings")
POSTING_KEYS = ("account", "commodity", "amount_micros", "comment", "tags", "status")
# The measured fields the source, the store columns and the reparsed export must all agree on.
NOTE_FIELDS = ("with_code", "with_comment", "with_tags")
# The ``entries`` columns those fields live in, which are also the ``canonical_txn`` keys they are rebuilt from.
NOTE_COLUMNS = ("code", "comment", "tags")


@dataclass(frozen=True)
class Extent:
    """How much of the source carries a transaction code, comment or tags, and what posting statuses it uses."""

    transactions: int
    with_code: int
    with_comment: int
    with_tags: int
    postings: int
    posting_statuses: dict[str, int]

    def as_json(self) -> dict:
        return {"transactions": self.transactions, "with_code": self.with_code, "with_comment": self.with_comment,
                "with_tags": self.with_tags, "postings": self.postings, "posting_statuses": self.posting_statuses}


def measure(txns: list[Transaction]) -> Extent:
    """The extent of the note fields in a parsed journal: the source, or a reparsed export."""
    statuses: dict[str, int] = {}
    for t in txns:
        for p in t.postings:
            statuses[p.status] = statuses.get(p.status, 0) + 1
    return Extent(transactions=len(txns), with_code=sum(1 for t in txns if t.code),
                  with_comment=sum(1 for t in txns if t.comment), with_tags=sum(1 for t in txns if t.tags),
                  postings=sum(len(t.postings) for t in txns), posting_statuses=dict(sorted(statuses.items())))


def columns(conn: Connection, table: str) -> set[str]:
    return {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')}


def schema_problems(conn: Connection) -> list[str]:
    """The schema shape the round trip depends on: every modelled column present, the unmodelled one still absent.

    A column appearing in :data:`UNMODELLED` is not a silent improvement either: the export, the canonical
    comparison and ``docs/design/decisions.md`` all have to change with it."""
    problems = []
    for table, column, field in MODELLED:
        if column not in columns(conn, table):
            problems.append(f"{table}.{column} is gone: the store can no longer model {field}, so the export cannot "
                            f"write it back and the round trip is lossy again")
    for table, column, field in UNMODELLED:
        if column in columns(conn, table):
            problems.append(f"{table}.{column} now exists: the store can model {field}; teach the exporter to render "
                            f"it, move it into the canonical record, and revisit docs/design/decisions.md")
    return problems


def record_problems(canonical_txns: list[tuple[int, str]]) -> list[str]:
    """Where a stored ``canonical_txn`` no longer carries the full-fidelity record.

    It is the source-of-truth copy of the import, and a backfill of the modelled columns is rebuilt from it, so a
    key dropping out of it is a loss even though the columns hold the same fields."""
    problems = []
    for source_index, raw in canonical_txns:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(f"tindex {source_index}: canonical_txn is not JSON: {exc}")
            continue
        missing = [k for k in TXN_KEYS if k not in rec]
        if missing:
            problems.append(f"tindex {source_index}: canonical_txn is missing {missing}")
        for i, p in enumerate(rec.get("postings") or []):
            missing = [k for k in POSTING_KEYS if k not in p]
            if missing:
                problems.append(f"tindex {source_index}: canonical_txn.postings[{i}] is missing {missing}")
    return problems


def note_fields_of(record: str, *, entry_id: str) -> dict[str, str]:
    """The three note columns as they would be rebuilt from one ``canonical_txn`` record.

    Migration 0002 added ``entries.code`` / ``comment`` / ``tags`` and, for a book already imported, filled them
    from this record, the only place the fields existed before the columns did.  The squash removed that
    migration, but what it guaranteed still holds because the record is still written and the columns are still
    filled from the same source:

        the two copies the store keeps of a transaction's code, comment and tags agree, and a record that
        cannot supply them is an error rather than three empty columns.

    An empty column would be the loss the whole chunk exists to prevent, and it would be silent.  ``''`` and
    ``'[]'`` are legitimate values meaning "this transaction has none", so nothing downstream could tell a
    transaction with no comment from one whose comment was dropped.  Hence the refusals.

    The tags are rendered with the same :func:`~haddon.domain.canonical.canonical_json` the importer uses, so a
    rebuilt value is byte-identical to the stored one rather than merely equivalent.
    """
    try:
        rec = json.loads(record)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"entry {entry_id}: journal_mappings.canonical_txn is not JSON: {exc}") from None
    missing = [k for k in NOTE_COLUMNS if k not in rec]
    if missing:
        raise RuntimeError(f"entry {entry_id}: journal_mappings.canonical_txn is missing {missing}; the note "
                           f"columns cannot be rebuilt from it without losing them")
    code, comment, tags = rec["code"], rec["comment"], rec["tags"]
    if not isinstance(code, str) or not isinstance(comment, str):
        raise RuntimeError(f"entry {entry_id}: canonical_txn code/comment are not strings")
    if not isinstance(tags, list) or any(not (isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p))
                                         for p in tags):
        raise RuntimeError(f"entry {entry_id}: canonical_txn tags is not a list of [name, value] string pairs")
    return {"code": code, "comment": comment, "tags": canonical_json([list(p) for p in tags])}


def rebuild_problems(conn: Connection, entity_id: str) -> list[str]:
    """Where the stored note columns and the record they would be rebuilt from disagree.

    Two things can go wrong and only one of them is visible to the counting gates: a column could hold a
    *different* value from the record (the counts still match), and two ``journal_mappings`` rows for the same
    entry could disagree about the transaction, in which case there is no single answer to rebuild to.  The
    0002 backfill refused both; this says the same thing about a book built by today's importer.
    """
    from haddon.repository import tables as t

    m, e = t.journal_mappings, t.entries
    stmt = (sa.select(e.c.id, m.c.id, m.c.canonical_txn, e.c.code, e.c.comment, e.c.tags)
            .select_from(m.join(e, sa.and_(m.c.entity_id == e.c.entity_id, m.c.entry_id == e.c.id)))
            .where(m.c.entity_id == entity_id).order_by(e.c.id, m.c.id))
    problems: list[str] = []
    rebuilt: dict[str, dict[str, str]] = {}
    for entry_id, mapping_id, record, code, comment, tags in conn.execute(stmt):
        try:
            found = note_fields_of(record, entry_id=entry_id)
        except RuntimeError as exc:
            problems.append(str(exc))
            continue
        if rebuilt.setdefault(entry_id, found) != found:
            problems.append(f"entry {entry_id}: journal_mappings row {mapping_id} disagrees with an earlier row "
                            f"about the transaction code/comment/tags; there is no value to rebuild to")
        stored = {"code": code, "comment": comment, "tags": tags}
        if stored != found:
            problems.append(f"entry {entry_id}: the stored note columns {stored} are not what canonical_txn says "
                            f"{found}; the store's two copies of the note fields disagree")
    return problems


def stored_records(conn: Connection, entity_id: str, *, start: str | None = None,
                   end: str | None = None) -> list[tuple[int, str]]:
    """``(source_index, canonical_txn)`` for every imported entry dated in the range."""
    from haddon.repository import tables as t
    m, e = t.journal_mappings, t.entries
    stmt = (sa.select(m.c.source_index, m.c.canonical_txn)
            .select_from(m.join(e, m.c.entry_id == e.c.id))
            .where(m.c.entity_id == entity_id)
            .order_by(m.c.source_index))
    if start is not None:
        stmt = stmt.where(e.c.dt >= start)
    if end is not None:
        stmt = stmt.where(e.c.dt <= end)
    return [(idx, raw) for idx, raw in conn.execute(stmt)]


def stored_note_counts(conn: Connection, entity_id: str, *, start: str | None = None,
                       end: str | None = None) -> dict[str, int]:
    """The same counts as :func:`measure`, taken from the ``entries`` columns of the posted entries in a range.

    This is the half a journal cannot tell you: whether the store itself holds the note fields, rather than the
    export having reproduced them from somewhere else."""
    from haddon.repository import tables as t
    e = t.entries
    where = [e.c.entity_id == entity_id, e.c.status == "posted"]
    if start is not None:
        where.append(e.c.dt >= start)
    if end is not None:
        where.append(e.c.dt <= end)
    count = lambda col: conn.execute(sa.select(sa.func.count()).select_from(e).where(*where, col)).scalar_one()
    return {"transactions": conn.execute(sa.select(sa.func.count()).select_from(e).where(*where)).scalar_one(),
            "with_code": count(e.c.code != ""), "with_comment": count(e.c.comment != ""),
            "with_tags": count(e.c.tags != "[]")}
