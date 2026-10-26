"""Keyset pagination: ``{items, next_cursor, snapshot_seq}``, bounded by a snapshot the cursor carries.

The spec fixes three rules:

* Opaque cursors bind entity, filters, ordering and snapshot.  ``haddon.api.cursors`` signs the payload, and
  ``_check_binding`` refuses a cursor whose entity, collection or filter/ordering digest is not this request's.
* Pages never silently mix.  Page one records a watermark, the ordering key of the last row then present, and
  every later page excludes anything sorting after it, so a row created mid-listing cannot appear on page two
  and shift the rest.  The watermark is the ordering key itself, not a rowid (which VACUUM renumbers), so it
  survives maintenance.
* A cursor whose snapshot is gone is ``409 cursor_expired``, with a resync link.  Each page recomputes a probe
  over exactly the rows the cursor covers: their count, plus the sum of their ``version`` columns where rows are
  mutable, plus the row counts of the tables a derived field reads.  Anything that could change what an earlier
  page showed (a row archived, superseded or removed, rowids shuffled by a restore) moves the probe and expires
  the cursor.  Appending a row after the watermark does not, because the watermark already excludes it; that is
  the case pagination must survive.

Ordering is ``(created_at, id)`` unless a collection says otherwise.  ``created_at`` has one-second resolution
and is not unique, so the id is always the last component of both the key and the watermark.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.api import cursors
from haddon.api.config import CursorKeyring
from haddon.domain.canonical import canonical_json, sha256_hex
from haddon.domain.errors import CURSOR_EXPIRED, HaddonError, validation
from haddon.repository import tables as t
from haddon.repository.events import high_water

DEFAULT_ORDER = ("created_at", "id")


def page_limit(raw: object, *, default: int, maximum: int, path: str = "limit") -> int:
    """Return the page size, defaulted and bounded.  Above the published maximum is refused, never clamped."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise validation(f"{path} must be an integer", path=path, reason="int_required", value=raw)
    if not 1 <= raw <= maximum:
        raise validation(f"{path} must be between 1 and {maximum}", path=path, reason="range", value=raw,
                         maximum=maximum)
    return raw


def filters_digest(*, kind: str, order: Sequence[str], filters: dict[str, Any]) -> str:
    """Digest the collection, its ordering and every filter that shaped the query, for a cursor to bind."""
    return sha256_hex(canonical_json({"kind": kind, "order": list(order), "filters": filters}))


@dataclass(frozen=True)
class ListSpec:
    """One paginated collection: its table, its ordering, and what makes its rows change under a reader."""

    kind: str
    table: sa.Table
    order: tuple[str, ...] = DEFAULT_ORDER
    version_column: str | None = None          # summed into the probe when the rows are mutable
    derived_from: tuple[sa.Table, ...] = ()    # tables a derived field reads (a change there changes the page)

    def columns(self) -> list[sa.Column]:
        return [self.table.c[name] for name in self.order]


@dataclass(frozen=True)
class Page:
    items: list[dict]
    next_cursor: str | None
    snapshot_seq: int

    def body(self, **extra: Any) -> dict:
        return {"items": self.items, "next_cursor": self.next_cursor, "snapshot_seq": self.snapshot_seq, **extra}

    def with_items(self, items: list[dict]) -> "Page":
        """Return the same page with its rows replaced by shaped items.

        This lets a route batch the lookups an item needs (an entry's legs, an account's commodities) into one
        query instead of one per row."""
        return Page(items, self.next_cursor, self.snapshot_seq)


def _key_of(row, spec: ListSpec) -> list[Any]:
    return [getattr(row, name) for name in spec.order]


def _key_after(spec: ListSpec, key: Sequence[Any]):
    """Build ``(c1, c2, ...) > (k1, k2, ...)`` longhand, so it does not depend on SQLite row-value support."""
    cols = spec.columns()
    clauses = []
    for i, col in enumerate(cols):
        equal_prefix = [cols[j] == key[j] for j in range(i)]
        clauses.append(sa.and_(*equal_prefix, col > key[i]))
    return sa.or_(*clauses)


def _key_at_or_before(spec: ListSpec, key: Sequence[Any]):
    cols = spec.columns()
    clauses = []
    for i, col in enumerate(cols):
        equal_prefix = [cols[j] == key[j] for j in range(i)]
        clauses.append(sa.and_(*equal_prefix, col < key[i]))
    clauses.append(sa.and_(*[cols[j] == key[j] for j in range(len(cols))]))
    return sa.or_(*clauses)


def _watermark(conn: Connection, spec: ListSpec, where) -> list[Any] | None:
    """Return the ordering key of the last row present when the listing opens, or None if there are none."""
    row = conn.execute(sa.select(*spec.columns()).where(*where)
                       .order_by(*[c.desc() for c in spec.columns()]).limit(1)).first()
    return None if row is None else list(row)


def _probe(conn: Connection, spec: ListSpec, where, watermark: Sequence[Any] | None, entity_id: str) -> str:
    """Fingerprint exactly the rows the cursor covers, plus the tables their derived fields read."""
    if watermark is None:
        return "empty"
    bounded = [*where, _key_at_or_before(spec, watermark)]
    selectors: list[Any] = [sa.func.count()]
    if spec.version_column is not None:
        selectors.append(sa.func.coalesce(sa.func.sum(spec.table.c[spec.version_column]), 0))
    parts = [str(v) for v in conn.execute(sa.select(*selectors).where(*bounded)).one()]
    for table in spec.derived_from:
        parts.append(str(conn.execute(sa.select(sa.func.count()).where(table.c.entity_id == entity_id)).scalar_one()))
    return ":".join(parts)


def _check_binding(payload: dict, *, entity_id: str, spec: ListSpec, digest: str) -> None:
    if payload.get("e") != entity_id or payload.get("k") != spec.kind or payload.get("q") != digest:
        raise validation("this cursor belongs to a different entity, collection or set of filters",
                         path="cursor", reason="cursor_mismatch")


def expired(resync: str, *, reason: str) -> HaddonError:
    return HaddonError(CURSOR_EXPIRED, "the snapshot this cursor was taken at is no longer available; "
                                       "restart the listing from the resync link",
                       details=[{"path": "cursor", "reason": reason, "resync": resync}])


def paginate(conn: Connection, entity_id: str, *, spec: ListSpec, where: Sequence[Any], filters: dict[str, Any],
             limit: int, cursor: object, keys: CursorKeyring, resync: str, render) -> Page:
    """Return one page of ``spec``'s rows under the rules in the module docstring.  ``render(row)`` shapes each item."""
    scoped = [spec.table.c.entity_id == entity_id, *where]
    digest = filters_digest(kind=spec.kind, order=spec.order, filters=filters)
    after: list[Any] | None = None
    if cursor is None:
        watermark = _watermark(conn, spec, scoped)
        snapshot_seq = high_water(conn, entity_id)
    else:
        payload = cursors.decode(cursor, keys)
        _check_binding(payload, entity_id=entity_id, spec=spec, digest=digest)
        watermark = payload.get("w")
        after = payload.get("a")
        snapshot_seq = payload.get("s")
        if _probe(conn, spec, scoped, watermark, entity_id) != payload.get("p"):
            raise expired(resync, reason="snapshot_changed")
    bounded = list(scoped)
    if watermark is not None:
        bounded.append(_key_at_or_before(spec, watermark))
    if after is not None:
        bounded.append(_key_after(spec, after))
    rows = list(conn.execute(sa.select(spec.table).where(*bounded).order_by(*spec.columns()).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more and rows:
        next_cursor = cursors.encode({"e": entity_id, "k": spec.kind, "q": digest, "s": snapshot_seq,
                                      "w": watermark, "a": _key_of(rows[-1], spec),
                                      "p": _probe(conn, spec, scoped, watermark, entity_id)}, keys)
    return Page([render(r) for r in rows], next_cursor, snapshot_seq)


def paginate_sequence(items: Sequence[Any], *, entity_id: str, kind: str, filters: dict[str, Any], limit: int,
                      cursor: object, keys: CursorKeyring, resync: str, snapshot_seq: int, key_of,
                      render) -> Page:
    """Paginate a list the reports layer materialises (the register).

    The report is computed for the whole scoped range and then sliced, as "pre-page history included in the
    running balances" requires: every running balance accumulates from the start of the book, not the top of
    the page.  The cursor carries the last key emitted and a probe of the ordered set (its length and its first
    and last keys), so a line appearing in the range between pages expires the cursor instead of shifting rows.
    The slice is found by the position of that key, so it does not assume the report's ordering matches a
    lexicographic comparison of keys.
    """
    digest = filters_digest(kind=kind, order=("materialised",), filters=filters)
    probe = sha256_hex(canonical_json([len(items), [key_of(i) for i in items[:1]], [key_of(i) for i in items[-1:]]]))
    start = 0
    if cursor is not None:
        payload = cursors.decode(cursor, keys)
        if payload.get("e") != entity_id or payload.get("k") != kind or payload.get("q") != digest:
            raise validation("this cursor belongs to a different entity, collection or set of filters",
                             path="cursor", reason="cursor_mismatch")
        if payload.get("p") != probe:
            raise expired(resync, reason="snapshot_changed")
        wanted = payload.get("a")
        positions = [key_of(i) for i in items]   # not ``keys``: that is the signing keyring in this scope
        if wanted not in positions:
            raise expired(resync, reason="position_gone")
        start = positions.index(wanted) + 1
        snapshot_seq = payload.get("s", snapshot_seq)
    window = list(items[start:start + limit])
    more = start + limit < len(items)
    next_cursor = None
    if more and window:
        next_cursor = cursors.encode({"e": entity_id, "k": kind, "q": digest, "s": snapshot_seq,
                                      "a": key_of(window[-1]), "p": probe}, keys)
    return Page([render(i) for i in window], next_cursor, snapshot_seq)


ACCOUNTS = ListSpec("accounts", t.accounts, version_column="version")
ENTRIES = ListSpec("entries", t.entries, version_column="version")
SNAPSHOTS = ListSpec("snapshots", t.snapshots)
EXPORTS = ListSpec("exports", t.exports)
#: Statements order like everything else.  Their lines order by ``line_no``, the order they appear in the
#: original document, because a reader comparing a page against the bank's own file needs that order.  Whether
#: a statement is live is derived from the same table (a replacement retires it), so the table is its own
#: ``derived_from``.
STATEMENTS = ListSpec("statements", t.statements, derived_from=(t.statements,))
STATEMENT_LINES = ListSpec("statement_lines", t.statement_lines, order=("line_no", "id"))
#: Statement lines across every statement, in the order a reviewer reads a month: by date, then document order
#: within the day.  What has become of each line is derived from its explanations, their allocations and the
#: entries behind them, so a change in any of those must expire an open cursor.
LINES = ListSpec("lines", t.statement_lines, order=("dt", "line_no", "id"),
                 derived_from=(t.explanations, t.allocations, t.entries, t.statements))
#: Registered documents.  ``filing_count`` is derived from ``evidence_links``, so filing a document against
#: another entry mid-listing must expire an open cursor rather than quietly change an already-served page.
DOCUMENTS = ListSpec("documents", t.documents, derived_from=(t.evidence_links,))
#: The provisional bookings (docs/design/decisions.md, "Booked, awaiting a document").  Whether a row is live
#: is derived from the same table (a superseding row settles one), so the table is its own ``derived_from``.
#: Appending any expectation row then expires an open cursor, so page two cannot disagree with page one about
#: what is still provisional.
DOCUMENT_EXPECTATIONS = ListSpec("document_expectations", t.document_expectations,
                                 derived_from=(t.document_expectations,))
#: Recoverable tax that was not recovered (docs/design/decisions.md).  As with the expectations above, whether
#: a row is live is derived from the same table, so the table is its own ``derived_from``.
UNRECOVERED_TAX = ListSpec("unrecovered_tax", t.unrecovered_tax, derived_from=(t.unrecovered_tax,))
