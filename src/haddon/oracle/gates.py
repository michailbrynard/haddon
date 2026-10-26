"""The gates: what the oracle actually asserts about the three representations (chunk C2b).

Every function here takes the already-read :class:`Views` and returns one :class:`~haddon.oracle.report.Gate`.
None of them raises for a disagreement.  A disagreement is a failed gate, which is what makes the run's exit code
the single source of truth.  They raise only when the store or a journal is so malformed that there is
nothing to compare, and the runner turns that into a failed gate too.

The comparisons are three-way on purpose.  "Source equals export" is weak (both come out of the same extractor);
"source equals store equals export" is the loop closed, and each gate reports which *pair* disagreed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.canonical import canonical_json
from haddon.exporting.exporter import ExportResult, verify_artifacts
from haddon.importing.hledger import Transaction
from haddon.importing.importer import canonical_txn as source_canonical_txn
from haddon.importing.importer import memo_for, relative_to_checkout
from haddon.oracle import canonical as C
from haddon.oracle import gap
from haddon.oracle.report import Gate, gate
from haddon.repository import tables as t
from haddon.repository.provisioning import all_accounts
from haddon.reports.postings import posted_postings
from haddon.reports.scope import scope

VIEWS = ("source", "store", "export")


@dataclass(frozen=True)
class Views:
    """The three representations of one book, plus what the identity and gap gates need beyond the canonical form."""

    entity: str
    entity_id: str
    start: str
    end: str
    checkout: Path
    db_path: Path
    source_journal: Path
    export: ExportResult
    source: C.Book
    source_txns: list[Transaction]
    source_micros: dict[int, list[str]]  # tindex -> exact micros per leg, from the pinned binary
    store: C.Book
    store_by_id: dict[str, C.Txn]
    reparsed: C.Book
    reparsed_txns: list[Transaction]
    memos: dict[str, Counter] = field(default_factory=dict)  # 'source' | 'store' | 'export'
    mappings: tuple[tuple[str, ...], ...] = ()  # (id, entry_id, source_index, journal_path, source_status, canonical_txn)

    @property
    def books(self) -> dict[str, C.Book]:
        return {"source": self.source, "store": self.store, "export": self.reparsed}

    @property
    def ledger_dir(self) -> Path:
        return Path(self.export.ledger_dir)


def counts_gate(v: Views) -> Gate:
    """Transaction and posting counts, per entity/year, across all three."""
    got = {name: (len(book), sum(len(x.legs) for x in book)) for name, book in v.books.items()}
    distinct = set(got.values())
    detail = {name: {"transactions": n, "postings": p} for name, (n, p) in got.items()}
    if len(distinct) == 1:
        n, p = distinct.pop()
        return gate("counts", True, f"{n} transactions / {p} postings in all three representations", detail)
    return gate("counts", False, "the three representations do not hold the same number of transactions/postings",
                detail, [f"{name}: {n} transactions / {p} postings" for name, (n, p) in sorted(got.items())])


def _totals_gate(name: str, per_view: dict[str, dict], key_label: str) -> Gate:
    """Enumerate every key present in any of the three and compare the three values explicitly.

    A pair that one representation does not have is a mismatch, not a skipped row."""
    keys = sorted(set().union(*(set(d) for d in per_view.values())))
    mismatches = []
    for key in keys:
        values = {view: per_view[view].get(key) for view in VIEWS}
        if len({canonical_json(v) for v in values.values()}) != 1:
            mismatches.append(f"{' / '.join(str(k) for k in key)}: " +
                              ", ".join(f"{view}={'absent' if values[view] is None else values[view]}" for view in VIEWS))
    detail = {key_label: len(keys), "mismatches": len(mismatches),
              "per_view": {view: len(per_view[view]) for view in VIEWS},
              "enumerated": [list(k) for k in keys]}
    if mismatches:
        return gate(name, False, f"{len(mismatches)} of {len(keys)} {key_label} disagree across the three representations",
                    detail, mismatches)
    return gate(name, True, f"{len(keys)} {key_label} enumerated, 0 mismatches across source/store/export", detail)


def totals_gate(v: Views) -> Gate:
    return _totals_gate("totals", {view: C.totals(book) for view, book in v.books.items()}, "account/commodity pairs")


def monthly_totals_gate(v: Views) -> Gate:
    return _totals_gate("monthly_totals", {view: C.monthly_totals(book) for view, book in v.books.items()},
                        "account/commodity/month pairs")


def _describe(txn: C.Txn) -> str:
    legs = "; ".join(f"{l.account} {l.commodity} {l.amount_micros}" for l in txn.legs)
    notes = "".join([f" code={txn.code!r}" if txn.code else "", f" comment={txn.comment!r}" if txn.comment else "",
                     f" tags={[list(t) for t in txn.tags]}" if txn.tags else ""])
    return f"{txn.date} [{txn.status}]{notes} {txn.narration!r} :: {legs}"


def canonical_gate(v: Views) -> Gate:
    """The whole book as a multiset of complete transactions: exact date, source status, code, comment, tags, and
    every leg's account, commodity and exact integer amount, in order.

    Since chunk E1 it also carries the transaction's code, comment and tags, which the store models (migration
    0002) and the export writes back.  A dropped comment, an edited tag value and a reordered tag list are each a
    different element of this multiset, whichever of the three representations it happened in.

    This is the gate that separates an oracle from a totals check.  A same-month date swap and a compensating leg
    change both leave every account/commodity total and every monthly total untouched; both change this multiset,
    because a transaction is compared as a whole and counted with its multiplicity.
    """
    sets = {view: C.multiset(book) for view, book in v.books.items()}
    digests = {view: C.digest(book) for view, book in v.books.items()}
    detail = {"digests": digests, "transactions": {view: sum(sets[view].values()) for view in VIEWS},
              "distinct": {view: len(sets[view]) for view in VIEWS}}
    mismatches = []
    for other in ("store", "export"):
        only_a, only_b = sets["source"] - sets[other], sets[other] - sets["source"]
        for txn, n in sorted(only_a.items()):
            mismatches.append(f"in source but not {other} (x{n}): {_describe(txn)}")
        for txn, n in sorted(only_b.items()):
            mismatches.append(f"in {other} but not source (x{n}): {_describe(txn)}")
    if mismatches:
        return gate("canonical", False, "the canonical transaction/posting multisets are not equal", detail, mismatches)
    return gate("canonical", True, f"{sum(sets['source'].values())} transactions match exactly as a multiset "
                                   f"(digest {digests['source'][:12]})", detail)


def coverage_gate(v: Views) -> Gate:
    """Every source transaction maps to exactly one stored entry and back, through ``journal_mappings``.

    Four failure modes, each named separately: a source transaction with no row, a row with no source transaction,
    two rows sharing an entry id or a source index, and a stored entry in the book with no row at all.  The record
    itself is compared too.  The stored ``canonical_txn`` must equal the record recomputed from the source, which
    is the "and back" direction and covers the fields the canonical multiset leaves out.
    """
    by_index = {x.index: x for x in v.source_txns}
    rows = v.mappings
    mismatches, seen_entry, seen_index = [], {}, {}
    for _id, entry_id, source_index, journal_path, source_status, raw in rows:
        if source_index in seen_index:
            mismatches.append(f"source index {source_index} is claimed by two rows: {seen_index[source_index]} and {_id}")
        seen_index[source_index] = _id
        if entry_id in seen_entry:
            mismatches.append(f"entry {entry_id} is claimed by two rows: {seen_entry[entry_id]} and {_id}")
        seen_entry[entry_id] = _id
        txn = by_index.get(source_index)
        if txn is None:
            mismatches.append(f"row {_id} maps source index {source_index}, which the source journal does not have")
            continue
        if entry_id not in v.store_by_id:
            mismatches.append(f"row {_id} maps source index {source_index} to entry {entry_id}, which is not a posted "
                              f"entry of {v.start}..{v.end}")
            continue
        expected = source_canonical_txn(txn, journal_rel=relative_to_checkout(txn.source_file, v.checkout),
                                        micros=v.source_micros[txn.index])
        if canonical_json(expected) != raw:
            mismatches.append(f"source index {source_index}: the stored canonical_txn is not the record the source "
                              f"produces (entry {entry_id})")
        if source_status != txn.status:
            mismatches.append(f"source index {source_index}: stored source_status {source_status!r} != {txn.status!r}")
        if not journal_path.endswith(Path(txn.source_file).name):
            mismatches.append(f"source index {source_index}: stored journal_path {journal_path!r} is not "
                              f"{Path(txn.source_file).name!r}")
    for index in sorted(set(by_index) - set(seen_index)):
        mismatches.append(f"source transaction {index} has no journal_mappings row")
    for entry_id in sorted(set(v.store_by_id) - set(seen_entry)):
        mismatches.append(f"posted entry {entry_id} has no journal_mappings row: it is not in the source")
    detail = {"source_transactions": len(by_index), "mapping_rows": len(rows), "stored_entries": len(v.store_by_id),
              "distinct_entry_ids": len(seen_entry), "distinct_source_indices": len(seen_index),
              "mismatches": len(mismatches)}
    if mismatches:
        return gate("coverage", False, "journal_mappings is not a bijection between the source and the store",
                    detail, mismatches)
    return gate("coverage", True, f"{len(by_index)} source transactions <-> {len(rows)} mappings <-> "
                                  f"{len(v.store_by_id)} posted entries, one to one, records equal", detail)


def memos_of_journal(txns: list[Transaction], micros: dict[int, list[str]]) -> Counter:
    """Posting comments as the import would store them (``memo_for``), keyed by everything that identifies the leg."""
    return Counter((x.date, x.description, i + 1, p.account, p.amount.commodity, micros[x.index][i], memo_for(p))
                   for x in txns for i, p in enumerate(x.postings))


def memos_of_store(conn: Connection, entity_id: str, *, start: str | None = None, end: str | None = None) -> Counter:
    """Posting memos as the store holds them, keyed the same way as :func:`memos_of_journal`."""
    sc = scope(conn, entity_id, start=start, end=end)
    accounts = all_accounts(conn, entity_id)
    rows = posted_postings(conn, entity_id, start=sc.start, end=sc.end)
    return Counter((r.dt, r.narration, r.seq, accounts[r.account_id].path, r.commodity, str(int(r.amount_micros)), r.memo)
                   for r in rows)


def _memo_norm(item: tuple) -> tuple:
    """The one normalisation a journal round trip applies: hledger terminates a comment it wrote with a newline."""
    return item[:-1] + ((item[-1].rstrip("\n") or None) if item[-1] else item[-1],)


def posting_memos_gate(v: Views, *, expect_normalised: int | None = None) -> Gate:
    """Posting comments survive source -> store -> reparsed export.

    They are modelled (``postings.memo``) and the export re-emits them, so they are compared, not excluded.
    Source -> store must be exact.  Store -> reparsed export is exact except where hledger terminates a comment it
    wrote with a newline.  Those are enumerated and their number is pinned, so the exception can neither grow nor
    hide a different one.
    """
    src, store, exp = v.memos["source"], v.memos["store"], v.memos["export"]
    mismatches = []
    for item, n in sorted((src - store).items()):
        mismatches.append(f"source memo not in store (x{n}): {item}")
    for item, n in sorted((store - src).items()):
        mismatches.append(f"store memo not in source (x{n}): {item}")
    raw_diff = (store - exp) + (exp - store)
    store_n = Counter(_memo_norm(i) for i in store.elements())
    exp_n = Counter(_memo_norm(i) for i in exp.elements())
    residue = (store_n - exp_n) + (exp_n - store_n)  # what differs by MORE than a trailing newline
    for item, n in sorted(residue.items()):
        mismatches.append(f"store/export memo differs by more than a trailing newline (x{n}): {item}")
    n_normalised = (sum(raw_diff.values()) - sum(residue.values())) // 2
    if expect_normalised is not None and n_normalised != expect_normalised:
        mismatches.append(f"{n_normalised} postings round trip only up to a trailing newline; {expect_normalised} pinned")
    detail = {"postings": sum(src.values()), "source_vs_store_mismatches": sum((src - store).values()) + sum((store - src).values()),
              "store_vs_export_newline_only": n_normalised, "store_vs_export_other": sum(residue.values()),
              "examples": [str(i) for i, _ in sorted(raw_diff.items())][:6]}
    if mismatches:
        return gate("posting_memos", False, "posting comments do not survive the round trip", detail, mismatches)
    return gate("posting_memos", True, f"{sum(src.values())} posting comments: source == store exactly; "
                                       f"store == reparsed export but for {n_normalised} trailing newline(s)", detail)


def round_trip_gate(conn: Connection, v: Views, *, expected: dict[str, int] | None = None) -> Gate:
    """The transaction-level note fields survive source -> store -> export (chunk E1).

    This gate replaced C2b's ``unmodelled_fields``, which asserted the gap: the export reproduced none of the
    codes, comments and tags, and they survived only inside ``journal_mappings.canonical_txn``.  Migration 0002
    closed the gap, so this gate asserts the stronger statement that nothing is lost:

    * the ``entries`` columns hold as many codes, comments and tag sets as the source has (the store models them);
    * the reparsed export holds exactly the same numbers (the export writes them back);
    * the source and the reparsed export have the same posting-status histogram, so the one field still
      unmodelled (:data:`haddon.oracle.gap.UNMODELLED`) is not silently dropped either;
    * ``canonical_txn`` still carries the full-fidelity record, and there is one per source transaction;
    * the schema still has every column this depends on.

    The values are compared by :func:`canonical_gate`, whose multiset carries the code, comment and tags of every
    transaction.  This gate is the counting half: it is what fails when a field disappears wholesale rather than
    changing.
    """
    source = gap.measure(v.source_txns)
    reparsed = gap.measure(v.reparsed_txns)
    stored = gap.stored_records(conn, v.entity_id, start=v.start, end=v.end)
    columns = gap.stored_note_counts(conn, v.entity_id, start=v.start, end=v.end)
    problems = gap.schema_problems(conn) + gap.record_problems(stored)
    for key in ("transactions", "postings", *gap.NOTE_FIELDS):
        got, want = getattr(reparsed, key), getattr(source, key)
        if got != want:
            problems.append(f"the reparsed export has {got} {key.replace('_', ' ')}, the source has {want}: the "
                            f"export does not write the field back")
    if source.posting_statuses != reparsed.posting_statuses:
        problems.append(f"posting statuses: source {source.posting_statuses}, reparsed export "
                        f"{reparsed.posting_statuses} (postings.status is not modelled, so a source status other "
                        f"than Unmarked cannot survive the round trip)")
    for key in gap.NOTE_FIELDS:
        if columns[key] != getattr(source, key):
            problems.append(f"the entries columns hold {columns[key]} transactions {key.replace('_', ' ')}, the "
                            f"source has {getattr(source, key)}: the import does not store the field")
    if columns["transactions"] != source.transactions:
        problems.append(f"{columns['transactions']} posted entries for {source.transactions} source transactions")
    if len(stored) != source.transactions:
        problems.append(f"{len(stored)} canonical_txn records for {source.transactions} source transactions")
    if expected is not None:
        for key, want in sorted(expected.items()):
            got = getattr(source, key)
            if got != want:
                problems.append(f"the source now has {got} transactions {key.replace('_', ' ')}; {want} pinned "
                                f"(the extent of the note fields changed)")
    detail = {"modelled": [f"{table}.{column} (hledger {field_})" for table, column, field_ in gap.MODELLED],
              "unmodelled": [f"{table}.{column} (hledger {field_})" for table, column, field_ in gap.UNMODELLED],
              "source": source.as_json(), "store_columns": columns, "reparsed_export": reparsed.as_json(),
              "canonical_txn_records": len(stored)}
    if problems:
        return gate("round_trip", False, "the transaction note fields do not survive source -> store -> export",
                    detail, problems)
    return gate("round_trip", True,
                f"{source.with_comment} comments, {source.with_tags} tag sets and {source.with_code} codes: "
                f"in the source, in the entries columns and back out in the reparsed export; "
                f"{source.postings} postings all {'/'.join(sorted(source.posting_statuses))}; all {len(stored)} "
                f"canonical_txn records still carry the full record", detail)


def artifacts_gate(v: Views) -> Gate:
    """Every published file re-hashes to what the ``exports`` row recorded, and the pinned verifier passed."""
    recorded = [a.as_json() for a in v.export.artifacts]
    problems = verify_artifacts(v.ledger_dir, recorded)
    verifier = v.export.verifier
    if verifier.get("result") != "pass":
        problems.append(f"the recorded verifier result is {verifier.get('result')!r}, not 'pass'")
    if v.export.manifest.get("state_hash") != v.export.state_hash:
        problems.append("the manifest's state hash is not the export's state hash")
    detail = {"artifacts": len(recorded), "ledger_dir": str(v.ledger_dir), "state_hash": v.export.state_hash,
              "manifest_sha256": v.export.manifest_sha256, "verifier": verifier}
    if problems:
        return gate("artifacts", False, "the published export does not match its recorded evidence", detail, problems)
    return gate("artifacts", True, f"{len(recorded)} published artifacts re-hash to the recorded manifest; "
                                   f"{verifier['version']} check --strict passed", detail)


def fingerprint(conn: Connection, v: Views) -> dict[str, Any]:
    """Everything the baseline pins, measured from this run."""
    source = gap.measure(v.source_txns)
    return {
        "entries": len(v.source), "postings": sum(len(x.legs) for x in v.source),
        "pairs": len(C.totals(v.source)), "monthly_pairs": len(C.monthly_totals(v.source)),
        "accounts": v.export.counts["accounts"], "commodities": v.export.counts["commodities"],
        "source_files": len({Path(x.source_file).name for x in v.source_txns}),
        "canonical_sha256": C.digest(v.source), "totals_sha256": C.totals_digest(C.totals(v.source)),
        "monthly_sha256": C.totals_digest(C.monthly_totals(v.source)),
        "mapping_sha256": mapping_digest(v.mappings),
        "txn_comments": source.with_comment, "txn_tags": source.with_tags, "txn_codes": source.with_code,
        "posting_statuses": source.posting_statuses,
    }


def mapping_digest(rows: tuple[tuple[str, ...], ...]) -> str:
    """A digest over the full-fidelity ``journal_mappings`` records.

    ``canonical_txn`` carries fields the canonical multiset leaves out (posting statuses, comments and tags, the
    source path and index), so this hash moves when any of them does."""
    from haddon.domain.canonical import sha256_hex
    body = [[int(r[2]), r[3], r[4], r[5]] for r in sorted(rows, key=lambda r: r[2])]
    return sha256_hex(canonical_json({"schema": C.SCHEMA, "mappings": body}))


def baseline_gate(measured: dict[str, Any], pinned: dict[str, Any] | None) -> Gate:
    """Measured figures and content digests against the ones pinned for these books.

    Any difference in the source, the import or the export moves one of them."""
    if pinned is None:
        return gate("baseline", False, "no pinned baseline for this entity/year: an unpinned run is not an oracle",
                    {"measured": measured})
    mismatches = [f"{key}: measured {measured.get(key)!r}, pinned {pinned[key]!r}"
                  for key in sorted(pinned) if measured.get(key) != pinned[key]]
    detail = {"measured": measured, "pinned": pinned, "mismatches": len(mismatches)}
    if mismatches:
        return gate("baseline", False, f"{len(mismatches)} pinned figure(s) no longer describe the source", detail, mismatches)
    return gate("baseline", True, f"all {len(pinned)} pinned figures and content digests match the source", detail)


def mapping_rows(conn: Connection, entity_id: str, *, start: str | None = None,
                 end: str | None = None) -> tuple[tuple[str, ...], ...]:
    """The ``journal_mappings`` rows of entries dated in the range, in the shape ``Views.mappings`` documents."""
    m, e = t.journal_mappings, t.entries
    stmt = (sa.select(m.c.id, m.c.entry_id, m.c.source_index, m.c.journal_path, m.c.source_status, m.c.canonical_txn)
            .select_from(m.join(e, m.c.entry_id == e.c.id))
            .where(m.c.entity_id == entity_id)
            .order_by(m.c.source_index, m.c.id))
    if start is not None:
        stmt = stmt.where(e.c.dt >= start)
    if end is not None:
        stmt = stmt.where(e.c.dt <= end)
    return tuple(tuple(r) for r in conn.execute(stmt))
