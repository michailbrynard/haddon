"""The three representations of one book, reduced to one comparable shape (chunk C2b).

The oracle compares pinned source -> store -> reparsed export.  This module turns each of those three into the
same record type, so a comparison is a plain equality of multisets and dictionaries rather than three bespoke walks
that could each be wrong in its own way.

* :func:`from_journal` reads a journal with the pinned binary (``hledger print -O json``) and is used for both the
  source and the reparsed export.  With one extractor, a bug in it cannot make those two agree with each other
  while both disagree with the store.
* :func:`from_store` reads the store through ``reports.postings.posted_postings``, the same query the export uses,
  so the oracle compares what the reports call official.  ``journal_mappings`` supplies the source status.

The record carries exactly what all three can represent: the date, the narration, the source status, the
transaction code, comment and tags (modelled since migration 0002, chunk E1), and the complete ordered legs with
account, commodity and exact integer micros.  The one field it still leaves out is the posting status, which
``postings`` has no column for.  :mod:`haddon.oracle.gap` measures what that leaves, and the ``round_trip`` gate
compares the source's posting statuses against the reparsed export's.  Amounts are base-10 integer strings
throughout; no float is created anywhere in this module.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.canonical import Tag, canonical_json, parse_tags, sha256_hex
from haddon.domain.errors import validation
from haddon.importing.hledger import DeclaredCommodity, Hledger, Transaction
from haddon.repository import tables as t
from haddon.repository.provisioning import all_accounts, read_entity
from haddon.reports.postings import posted_postings
from haddon.reports.scope import scope

SCHEMA = "haddon-oracle-1"


@dataclass(frozen=True, order=True)
class Leg:
    """One posting, reduced to what every representation carries exactly."""

    account: str
    commodity: str
    amount_micros: str  # base-10 integer string: never an int that could be summed by accident, never a float

    def as_json(self) -> dict[str, str]:
        return {"account": self.account, "commodity": self.commodity, "amount_micros": self.amount_micros}


@dataclass(frozen=True, order=True)
class Txn:
    """One transaction as all three representations carry it.

    Hashable, so a book is a ``Counter`` of these: two genuinely identical transactions stay two, and a same-month
    date swap or a compensating leg change is a different element even when every monthly total is unchanged.

    ``code`` / ``comment`` / ``tags`` are here because chunk E1 gave the store columns for them.  A dropped
    comment, an edited tag value or a reordered tag list is a different element of this multiset, in whichever of
    the three representations it happened."""

    date: str
    narration: str
    status: str  # the source status: 'Cleared' | 'Pending' | 'Unmarked'
    code: str  # hledger tcode, '' when there is none
    comment: str  # hledger tcomment, verbatim
    tags: tuple[Tag, ...]  # hledger ttags, in order: the order is part of the value, not an artefact
    legs: tuple[Leg, ...]

    def as_json(self) -> dict:
        return {"date": self.date, "narration": self.narration, "status": self.status, "code": self.code,
                "comment": self.comment, "tags": [list(t) for t in self.tags],
                "legs": [l.as_json() for l in self.legs]}


Book = tuple[Txn, ...]


def multiset(book: Book) -> Counter:
    return Counter(book)


def digest(book: Book) -> str:
    """A content hash of the whole book.

    Any change to a date, narration, status, code, comment, tag, account, commodity, amount or leg order changes
    it.  Transactions are sorted first, so it does not depend on the order a representation produced them in."""
    return sha256_hex(canonical_json({"schema": SCHEMA, "transactions": [x.as_json() for x in sorted(book)]}))


def micros_of(txn: Transaction, declared: dict[str, DeclaredCommodity]) -> list[str]:
    """Exact integer micros for every leg, through the same ``Amount.micros`` the import uses: mantissa and
    decimal places only, refused if the places disagree with the commodity directive.  No division, no float."""
    out = []
    for i, p in enumerate(txn.postings):
        decl = declared.get(p.amount.commodity)
        if decl is None:
            raise validation(f"transaction {txn.index}: commodity {p.amount.commodity!r} is not declared",
                             path=f"tpostings[{i}]", reason="commodity_unknown", tindex=txn.index)
        out.append(p.amount.micros(decl).render())
    return out


def txn_of(txn: Transaction, declared: dict[str, DeclaredCommodity]) -> Txn:
    return Txn(date=txn.date, narration=txn.description, status=txn.status, code=txn.code, comment=txn.comment,
               tags=tuple(txn.tags),
               legs=tuple(Leg(p.account, p.amount.commodity, m) for p, m in zip(txn.postings, micros_of(txn, declared), strict=True)))


def from_journal(journal: Path, hledger: Hledger | None = None) -> tuple[Book, list[Transaction]]:
    """Read a journal with the pinned binary.

    Returns the canonical book and the raw transactions, because the coverage and gap checks need the fields the
    canonical record deliberately drops."""
    hl = hledger or Hledger()
    journal = Path(journal)
    declared = hl.declared_commodities(journal)
    txns = hl.print_json(journal)
    return tuple(txn_of(x, declared) for x in txns), txns


def source_status_by_entry(conn: Connection, entity_id: str, *, start: str | None = None,
                           end: str | None = None) -> dict[str, str]:
    """``entry_id -> journal_mappings.source_status`` for the entries dated in the range.  Two disagreeing rows for
    one entry is an error, not a coin toss (the same rule the exporter applies)."""
    m, e = t.journal_mappings, t.entries
    stmt = (sa.select(m.c.entry_id, m.c.source_status)
            .select_from(m.join(e, m.c.entry_id == e.c.id))
            .where(m.c.entity_id == entity_id)
            .order_by(m.c.entry_id))
    if start is not None:
        stmt = stmt.where(e.c.dt >= start)
    if end is not None:
        stmt = stmt.where(e.c.dt <= end)
    out: dict[str, str] = {}
    for entry_id, status in conn.execute(stmt):
        if out.setdefault(entry_id, status) != status:
            raise validation(f"entry {entry_id!r} has two disagreeing journal_mappings source statuses",
                             path="journal_mappings", reason="mapping_conflict", entry_id=entry_id)
    return out


def from_store(conn: Connection, *, start: str | None = None, end: str | None = None,
               store_status: str = "Cleared") -> tuple[Book, dict[str, Txn]]:
    """The posted book of a range, read exactly the way the reports and the export read it.

    ``store_status`` is the source status attributed to an entry that has no ``journal_mappings`` row, meaning it
    was minted in the store rather than imported.  The corpus books have none.  It is a named parameter rather than
    a silent default so that an oracle run over a range with store-native entries says what it assumed.
    """
    entity = read_entity(conn)
    sc = scope(conn, entity.id, start=start, end=end)
    accounts = all_accounts(conn, entity.id)
    statuses = source_status_by_entry(conn, entity.id, start=sc.start, end=sc.end)
    rows = posted_postings(conn, entity.id, start=sc.start, end=sc.end)
    by_entry: dict[str, list] = {}
    for r in rows:
        by_entry.setdefault(r.entry_id, []).append(r)
    by_id: dict[str, Txn] = {}
    for entry_id, legs in by_entry.items():
        head = legs[0]
        by_id[entry_id] = Txn(
            date=head.dt, narration=head.narration, status=statuses.get(entry_id, store_status),
            code=head.code, comment=head.comment, tags=parse_tags(head.tags),
            legs=tuple(Leg(accounts[l.account_id].path, l.commodity, str(int(l.amount_micros)))
                       for l in sorted(legs, key=lambda l: l.seq)))
    return tuple(by_id.values()), by_id


def totals(book: Book) -> dict[tuple[str, str], int]:
    """Exact (account, commodity) totals, summed in Python ints."""
    out: dict[tuple[str, str], int] = {}
    for txn in book:
        for leg in txn.legs:
            key = (leg.account, leg.commodity)
            out[key] = out.get(key, 0) + int(leg.amount_micros)
    return out


def monthly_totals(book: Book) -> dict[tuple[str, str, str], int]:
    """Exact (account, commodity, ``YYYY-MM``) totals, summed in Python ints."""
    out: dict[tuple[str, str, str], int] = {}
    for txn in book:
        month = txn.date[:7]
        for leg in txn.legs:
            key = (leg.account, leg.commodity, month)
            out[key] = out.get(key, 0) + int(leg.amount_micros)
    return out


def totals_digest(mapping: dict) -> str:
    return sha256_hex(canonical_json({"schema": SCHEMA, "totals": [[list(k), str(v)] for k, v in sorted(mapping.items())]}))
