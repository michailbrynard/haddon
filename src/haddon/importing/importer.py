"""Import one corpus entity's journal for a date range into an empty, migrated entity database.

The corpus (which checkout, which entities, which year) is a manifest outside ``src/haddon``
(:mod:`haddon.importing.corpus`, issue 0006).  This module knows the layout a corpus has and nothing about which
company's books are in it.

A journal is continuous (issue 0002): an import creates no book and stores no year.  ``year`` is a CLI convenience
for the calendar-year range Jan 1..Dec 31, and the entries are simply dated rows on one ledger.

One ``write_transaction`` per entity, in this order (spec "Phase 1 migration"):

1. Strict gate.  ``hledger check --strict`` on the entity's main journal.  Failure aborts the whole import with
   the real hledger error (:class:`ImportBlocked`).  Nothing is written unless the caller opted in with
   ``on_blocked="skip"``, which records the block (entity row and ``import.blocked`` event) and returns a
   :class:`BlockedResult`.  That never looks like a success.
2. Provisioning from declarations only.  The entity, then the declared commodities (precision from the
   ``commodity`` directive), then ``hledger accounts --declared`` as the whole chart.  The chart is never built
   from observed posting names: a posting on an undeclared account is a hard error naming account and transaction.
   Account type is the first path segment (``assets``/``liabilities``/``equity``/``income``/``expenses``); any other
   first segment is an error, not a default.
3. Every transaction via ``entries.post_entry`` in ``tindex`` order.  ``origin`` is derived from the source file
   (``*-opening.journal`` -> ``opening``, else ``source``) in one place, :func:`origin_for`.  The one exception is
   an accounting one: an opening restatement of a period this journal already holds is verified against the
   continuous position and not posted.  Posting it would book every balance-sheet account twice while the journal
   still balanced per commodity (:mod:`haddon.importing.restatement`, issue 0002).  A restatement that does not
   agree ends the import; it is never skipped quietly.
   The transaction's ``tcode``, ``tcomment`` and ``ttags`` go into the ``entries`` columns migration 0002 added
   (chunk E1), so the export can reproduce them.  ``journal_mappings`` stores the source identity and a canonical
   JSON record of the transaction as imported, which carries them too: that record is the source-of-truth copy,
   the columns are the modelled one.
4. Nothing is verified or locked here: no ``migration_accept`` decision, no lock, nothing ``verified`` (C2).

A conservative default: every declared account is bound to all of the entity's declared commodities.  The spec
wants explicit per-account currencies.  The journals declare exactly one commodity each, so today this is the same
set, but the rule is wider than "what the account actually uses" and a future second commodity would be allowed
everywhere.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from haddon.db.connection import make_engine
from haddon.domain.canonical import canonical_json
from haddon.domain.dates import first_day, last_day
from haddon.domain.errors import ACCOUNT_UNKNOWN, IMMUTABLE, HaddonError, validation
from haddon.domain.micros import ZERO, Micros
from haddon.importing.corpus import Corpus, CorpusEntity, load_corpus
from haddon.importing.hledger import DeclaredCommodity, Hledger, HledgerError, Posting, Transaction
from haddon.importing.restatement import Restatement, verify_restatement
from haddon.repository import entries, events, provisioning
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction
from haddon.reports.postings import OPENING

ACTOR = "agent:haddon-import"

TYPE_BY_SEGMENT = {"assets": "asset", "liabilities": "liability", "equity": "equity", "income": "income", "expenses": "expense"}
CASH_ROOT = "assets:cash"


@dataclass(frozen=True)
class ImportResult:
    entity: str
    entity_id: str
    db_path: str
    start: str
    end: str
    source_revision: str
    commodities: int
    accounts: int
    entries: int
    postings: int
    pairs: int  # distinct (account, commodity) pairs among postings
    journal_mappings: int
    origins: dict[str, int]
    event_seq: int
    #: opening restatements of a period this journal already held, verified against the continuous position and
    #: not posted (see ``haddon.importing.restatement``).  Empty for every import that opens a journal.
    restatements: tuple[dict[str, Any], ...] = ()
    blocked: None = None

    def summary(self) -> str:
        carried = (f" restatements_verified={len(self.restatements)}" if self.restatements else "")
        return (f"{self.entity} ({self.entity_id}) -> {self.db_path}: {self.start}..{self.end} @ {self.source_revision[:12]} "
                f"commodities={self.commodities} accounts={self.accounts} entries={self.entries} "
                f"postings={self.postings} pairs={self.pairs} mappings={self.journal_mappings} "
                f"origins={self.origins}{carried}")


@dataclass(frozen=True)
class BlockedResult:
    """A skipped import.  Never a success: ``blocked`` carries the real hledger error, and an ``import.blocked``
    event was written (``event_seq``); no book is created and nothing else is written."""

    entity: str
    entity_id: str
    db_path: str
    start: str
    end: str
    blocked: dict[str, Any]  # message, stderr, file, line, command, returncode
    event_seq: int
    entries: int = 0
    postings: int = 0

    def summary(self) -> str:
        return (f"{self.entity} ({self.entity_id}) -> {self.db_path}: BLOCKED at "
                f"{self.blocked.get('file')}:{self.blocked.get('line')}: hledger check --strict failed; "
                f"entries=0 postings=0 event_seq={self.event_seq}\n{self.blocked['stderr'].rstrip()}")


class ImportBlocked(RuntimeError):
    """The strict gate failed.  Carries the entity and the :class:`HledgerError` (verbatim stderr, file, line)."""

    def __init__(self, entity: str, journal: Path, error: HledgerError) -> None:
        self.entity = entity
        self.journal = journal
        self.error = error
        super().__init__(f"{entity}: hledger check --strict failed on {journal}; nothing imported.\n{error.stderr.rstrip()}")


def journal_path(checkout: Path, entity: str, year: int) -> Path:
    """Where a corpus keeps one entity's year.  The layout, not the corpus: ``Corpus.journal`` is the one that
    knows which checkout and which default year (issue 0006)."""
    return Path(checkout).joinpath(entity, str(year), "ledger", f"{year}.journal")


def source_revision(checkout: Path) -> str:
    """The checkout's commit (``git rev-parse HEAD``, read-only; ``--no-optional-locks`` so git writes nothing)."""
    proc = subprocess.run(["git", "--no-optional-locks", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True)
    rev = proc.stdout.strip()
    if len(rev) != 40:
        raise validation(f"unexpected git revision {rev!r}", path="source_revision", reason="revision_shape")
    return rev


def origin_for(source_file: str) -> str:
    """Economic origin from the transaction's source file: ``*-opening.journal`` -> ``opening``, else ``source``.
    The only place this derivation lives."""
    return "opening" if Path(source_file).name.endswith("-opening.journal") else "source"


def account_type_for(path: str) -> str:
    """Account type from the first path segment.  An unrecognised first segment is an error, never a default."""
    segment = path.split(":", 1)[0]
    if segment not in TYPE_BY_SEGMENT:
        raise validation(f"account {path!r}: first segment {segment!r} is not one of {sorted(TYPE_BY_SEGMENT)}",
                         path="accounts", reason="unknown_account_type", account=path)
    return TYPE_BY_SEGMENT[segment]


def is_cash_path(path: str) -> bool:
    """``is_cash`` rule: ``assets:cash`` itself and every account under ``assets:cash:*``."""
    return path == CASH_ROOT or path.startswith(CASH_ROOT + ":")


def memo_for(posting: Posting) -> str | None:
    """The posting's raw comment, verbatim (trailing newline included); tags hledger reports that are not literally
    in the comment text (e.g. inherited ones) are appended as ``name: value`` lines so nothing is dropped.  ``None``
    only when there is neither a comment nor a tag."""
    memo = posting.comment
    for name, value in posting.tags:
        if f"{name}:" not in memo:
            memo = f"{memo}\n{name}: {value}" if memo else f"{name}: {value}"
    return memo or None


def canonical_txn(txn: Transaction, *, journal_rel: str, micros: list[str]) -> dict[str, Any]:
    """The record the C2 oracle compares against: complete enough that a same-month date swap or a compensating
    leg change is a different record.  Amounts are integer micro strings."""
    return {
        "date": txn.date, "description": txn.description, "code": txn.code, "comment": txn.comment,
        "tags": [list(tag) for tag in txn.tags], "status": txn.status, "journal_path": journal_rel, "source_index": txn.index,
        "postings": [{"account": p.account, "commodity": p.amount.commodity, "amount_micros": m, "comment": p.comment,
                      "tags": [list(tag) for tag in p.tags], "status": p.status}
                     for p, m in zip(txn.postings, micros, strict=True)],
    }


def relative_to_checkout(source_file: str, checkout: Path) -> str:
    try:
        return str(Path(source_file).resolve().relative_to(Path(checkout).resolve()))
    except ValueError:
        raise validation(f"transaction source {source_file!r} is outside the checkout {checkout}", path="tsourcepos",
                         reason="outside_checkout") from None


def _require_importable(engine: Engine, db_path: Path, spec: CorpusEntity, start: str, end: str,
                        into_existing: bool) -> bool:
    """Check the target and say whether this import extends an entity that is already there.

    The default is C1's rule: an import targets an empty migrated database, and a database that already holds an
    entity is refused.  ``into_existing=True`` is the explicit opt-in for importing a later date range of the same
    entity (a journal beside the one already loaded).  It still refuses a different entity and a range that
    already has entries, so a second run can never overwrite posted movement."""
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "entities" not in names or "journal_mappings" not in names:
            raise validation(f"{db_path} is not a migrated ledger database (run bin/haddon-migrate first)",
                             path="db_path", reason="not_migrated")
        row = conn.execute(sa.select(t.entities.c.id)).first()
        if row is None:
            if into_existing:
                raise validation(f"{db_path} holds no entity; into_existing imports a later range beside an existing journal",
                                 path="db_path", reason="not_provisioned")
            return False
        if not into_existing:
            raise HaddonError(IMMUTABLE, f"{db_path} already holds an entity; an import targets an empty migrated database",
                              details=[{"path": "db_path", "reason": "already_provisioned"}])
        if row[0] != spec.id:
            raise HaddonError(IMMUTABLE, f"{db_path} holds entity {row[0]!r}, not {spec.id!r}; one database is one entity",
                              details=[{"path": "db_path", "reason": "entity_mismatch", "stored": row[0], "requested": spec.id}])
        held = conn.execute(sa.select(t.entries.c.id)
                            .where(t.entries.c.entity_id == spec.id, t.entries.c.dt >= start, t.entries.c.dt <= end)
                            .limit(1)).first()
        if held is not None:
            raise HaddonError(IMMUTABLE, f"{db_path} already holds entries in {start}..{end}; a range is imported once",
                              details=[{"path": "from", "reason": "range_exists", "start": start, "end": end}])
        return True


def _provision(ctx: WriteCtx, spec: CorpusEntity, declared: dict[str, DeclaredCommodity], chart: list[str],
               year: int, *, extending: bool = False) -> dict[str, str]:
    """The commodities and the chart for ``year``.

    ``extending`` imports a later range beside entries this entity already has.  The commodity list and the chart
    are entity-wide and only ever grow.  A declaration already stored is reused, and one that disagrees with what
    is stored (a different precision, account type or cash rule) is an error, never an overwrite.  The declared
    chart of ``year`` is still the only chart this year's postings may use, so C1's rule stands: a posting on an
    account this range does not declare is a hard error."""
    if not declared:
        raise validation(f"{spec.key}: the journal declares no commodity", path="commodities", reason="none_declared")
    known = provisioning.all_commodities(ctx.conn, ctx.entity_id)
    for code, decl in declared.items():
        if code not in known:
            provisioning.add_commodity(ctx, code=code, precision=decl.precision)
        elif known[code].precision != decl.precision:
            raise validation(f"{spec.key} {year}: commodity {code!r} is declared with precision {decl.precision}, but the "
                             f"store already holds precision {known[code].precision}", path="commodities",
                             reason="precision_conflict", value=code)
    # Conservative default: every account allows every commodity the entity declares (see the module docstring).
    allowed = sorted(provisioning.all_commodities(ctx.conn, ctx.entity_id))
    if not chart:
        raise validation(f"{spec.key}: hledger accounts --declared listed no accounts", path="accounts", reason="none_declared")
    account_ids: dict[str, str] = {}
    for path in chart:
        existing = provisioning.account_by_path(ctx.conn, ctx.entity_id, path) if extending else None
        if existing is None:
            acct = provisioning.add_account(ctx, path=path, type=account_type_for(path), is_cash=is_cash_path(path),
                                           commodities=allowed, id=f"acct-{path}")
        else:
            if (existing.type, existing.is_cash) != (account_type_for(path), is_cash_path(path)):
                raise validation(f"{spec.key} {year}: account {path!r} is stored as {existing.type}/is_cash={existing.is_cash}, "
                                 f"which this year's declaration disagrees with", path="accounts",
                                 reason="account_conflict", account=path)
            for code in allowed:
                if not provisioning.is_commodity_allowed(ctx.conn, ctx.entity_id, existing.id, code):
                    provisioning.bind_account_commodity(ctx, account_id=existing.id, commodity=code)
            acct = existing
        account_ids[path] = acct.id
    return account_ids


def _legs_of(txn: Transaction, *, account_ids: dict[str, str],
             declared: dict[str, DeclaredCommodity]) -> tuple[list[entries.Leg], list[str]]:
    """The transaction's legs in store terms, and the integer micro strings the canonical record quotes.

    One conversion, used both by the entry that gets posted and by the opening restatement that gets verified
    instead: a restatement compared in different arithmetic from the postings it is compared against would not
    be a check at all."""
    legs, micros = [], []
    for i, p in enumerate(txn.postings):
        if p.account not in account_ids:
            raise HaddonError(ACCOUNT_UNKNOWN, f"transaction {txn.index} ({txn.date} {txn.description!r}) posts to "
                              f"{p.account!r}, which is not a declared account", details=[
                                  {"path": f"tpostings[{i}].paccount", "account": p.account, "tindex": txn.index,
                                   "source": f"{txn.source_file}:{txn.source_line}"}])
        decl = declared.get(p.amount.commodity)
        if decl is None:
            raise validation(f"transaction {txn.index}: commodity {p.amount.commodity!r} is not declared", path=f"tpostings[{i}]",
                             reason="commodity_unknown", tindex=txn.index)
        m = p.amount.micros(decl)
        micros.append(m.render())
        legs.append(entries.Leg(account_id=account_ids[p.account], commodity=p.amount.commodity, amount=m, memo=memo_for(p)))
    return legs, micros


def restates_a_period_already_held(conn: Connection, entity_id: str, txn: Transaction) -> bool:
    """Whether this transaction is an opening that the journal already reaches back past.

    Both halves matter.  An opening journal read into an empty journal *is* the journal's beginning and is
    posted normally; the same file read beside the year before it is a restatement of postings that are already
    there (see ``haddon.importing.restatement``)."""
    if origin_for(txn.source_file) != OPENING:
        return False
    earlier = conn.execute(sa.select(t.entries.c.id)
                           .where(t.entries.c.entity_id == entity_id, t.entries.c.status == "posted",
                                  t.entries.c.dt < txn.date).limit(1)).first()
    return earlier is not None


def _verify_restatement(ctx: WriteCtx, txn: Transaction, *, account_ids: dict[str, str],
                        declared: dict[str, DeclaredCommodity]) -> Restatement:
    """Check the restatement against the continuous position instead of posting it."""
    legs, _ = _legs_of(txn, account_ids=account_ids, declared=declared)
    stated: dict[tuple[str, str], Micros] = {}
    for leg in legs:
        key = (leg.account_id, leg.commodity)
        stated[key] = stated.get(key, ZERO) + leg.amount
    return verify_restatement(ctx.conn, ctx.entity_id, dt=txn.date, narration=txn.description, stated=stated)


def _post(ctx: WriteCtx, txn: Transaction, *, account_ids: dict[str, str], declared: dict[str, DeclaredCommodity],
          checkout: Path, revision: str, year: int) -> entries.Entry:
    legs, micros = _legs_of(txn, account_ids=account_ids, declared=declared)
    origin = origin_for(txn.source_file)
    # E1 (migration 0002): code, comment and tags go into ``entries`` as well as into the canonical_txn record
    # below.  Storing them in only one of the two is the C2a gap.  ``opening`` is a reserved origin that
    # post_entry refuses unless the caller says it may mint one.
    entry = entries.post_entry(ctx, dt=txn.date, narration=txn.description, origin=origin, legs=legs,
                               id=f"e-{year}-{txn.index:06d}", code=txn.code, comment=txn.comment, tags=txn.tags,
                               _allow_reserved_origin=(origin in entries.RESERVED_ORIGINS))
    journal_rel = relative_to_checkout(txn.source_file, checkout)
    record = canonical_txn(txn, journal_rel=journal_rel, micros=micros)
    ctx.insert(t.journal_mappings, f"jm-{year}-{txn.index:06d}", "jm", source_revision=revision,
               journal_path=journal_rel, source_index=txn.index, source_status=txn.status, canonical_txn=canonical_json(record),
               entry_id=entry.id, txn_id=None)
    return entry


def _counts(conn: Connection, entity_id: str, start: str, end: str) -> dict[str, Any]:
    """What this import put in the store.  The chart and the commodity list are entity-wide (they are the whole
    declared chart, which a later range can only widen); everything else is counted for the imported range only, so
    importing a second journal beside the first reports that journal's numbers rather than a running total."""
    n = lambda tbl: conn.execute(sa.select(sa.func.count()).select_from(tbl).where(tbl.c.entity_id == entity_id)).scalar_one()
    e, p, m = t.entries, t.postings, t.journal_mappings
    in_range = [e.c.entity_id == entity_id, e.c.dt >= start, e.c.dt <= end]
    ids = sa.select(e.c.id).where(*in_range).scalar_subquery()
    n_entries = conn.execute(sa.select(sa.func.count()).select_from(e).where(*in_range)).scalar_one()
    n_postings = conn.execute(sa.select(sa.func.count()).select_from(p)
                              .where(p.c.entity_id == entity_id, p.c.entry_id.in_(ids))).scalar_one()
    n_mappings = conn.execute(sa.select(sa.func.count()).select_from(m.join(e, m.c.entry_id == e.c.id))
                              .where(m.c.entity_id == entity_id, *in_range)).scalar_one()
    pairs = conn.execute(sa.select(sa.func.count()).select_from(
        sa.select(p.c.account_id, p.c.commodity).where(p.c.entity_id == entity_id, p.c.entry_id.in_(ids))
        .distinct().subquery())).scalar_one()
    origins = {o: c for o, c in conn.execute(sa.select(e.c.origin, sa.func.count())
                                             .where(*in_range)
                                             .group_by(e.c.origin).order_by(e.c.origin))}
    return {"commodities": n(t.commodities), "accounts": n(t.accounts), "entries": n_entries, "postings": n_postings,
            "pairs": pairs, "journal_mappings": n_mappings, "origins": origins}


def _record_blocked(engine: Engine, spec: CorpusEntity, journal: Path, exc: HledgerError, *, db_path: Path, start: str,
                    end: str, actor: str) -> BlockedResult:
    """``on_blocked="skip"``: write the entity row and an ``import.blocked`` event carrying the real error.

    No chart is built.  A journal that failed the gate may have an undeclared commodity or account, so its chart is
    not knowable.  No entity in the sample corpus is blocked; ``tests/test_import_spain.py`` proves the path on a
    stand-in."""
    blocked = {"message": str(exc), "stderr": exc.stderr, "file": exc.file, "line": exc.line, "command": exc.command,
               "returncode": exc.returncode, "journal": str(journal)}
    with write_transaction(engine) as conn:
        ctx = provisioning.provision_entity(conn, actor=actor, id=spec.id, name=spec.name, legal_form=spec.legal_form,
                                            country=spec.country, base_currency=spec.base_currency)
        seq = events.append_event(ctx, kind="import.blocked", subject_kind="import", subject_id=f"{spec.key}/{start}..{end}",
                                  after={"entity": spec.key, "start": start, "end": end, "error": blocked},
                                  reason="hledger check --strict failed; import skipped by explicit request")
    return BlockedResult(entity=spec.key, entity_id=spec.id, db_path=str(db_path), start=start, end=end,
                         blocked=blocked, event_seq=seq)


def import_entity(entity: str, db_path: str | Path, *, corpus: Corpus | None = None,
                  checkout: str | Path | None = None, year: int | None = None,
                  on_blocked: str = "raise", actor: str = ACTOR, hledger: Hledger | None = None,
                  into_existing: bool = False,
                  _fault: Callable[[Transaction], None] | None = None) -> ImportResult | BlockedResult:
    """Import ``entity``'s ``year`` journal from a corpus into the empty migrated database at ``db_path``.

    The entity, the checkout and the year all come from a corpus manifest (issue 0006).  ``corpus`` defaults to
    the one ``HADDON_CORPUS`` names, and ``checkout``/``year`` override that corpus's own for a test that reads a
    copied tree.

    ``on_blocked``: ``"raise"`` (default) raises :class:`ImportBlocked` when ``hledger check --strict`` fails;
    ``"skip"`` records the block and returns a :class:`BlockedResult`.  ``into_existing=True`` is the explicit
    opt-in for importing a later range of the same entity into a database that already holds an earlier range
    (a different entity, or a range that already has entries, is still refused).  ``_fault`` is a test hook called
    with each transaction before it is posted, so fault injection can prove the whole-entity rollback.
    """
    if on_blocked not in ("raise", "skip"):
        raise validation(f"on_blocked must be 'raise' or 'skip', got {on_blocked!r}", path="on_blocked", reason="enum")
    corpus = corpus or load_corpus()
    spec = corpus.entity(entity)
    year = corpus.default_year if year is None else year
    checkout = Path(corpus.checkout if checkout is None else checkout)
    db_path = Path(db_path)
    start, end = first_day(year), last_day(year)
    journal = journal_path(checkout, spec.key, year)
    if not journal.is_file():
        raise validation(f"journal {journal} does not exist", path="journal", reason="missing")
    hl = hledger or Hledger()
    engine = make_engine(db_path)
    try:
        extending = _require_importable(engine, db_path, spec, start, end, into_existing)
        # Step 1: the strict gate, before anything else runs.
        try:
            hl.check_strict(journal)
        except HledgerError as exc:
            if on_blocked == "skip":
                return _record_blocked(engine, spec, journal, exc, db_path=db_path, start=start, end=end, actor=actor)
            raise ImportBlocked(spec.key, journal, exc) from exc
        # Read-only: the declarations, the transactions and the source revision.
        declared = hl.declared_commodities(journal)
        chart = hl.declared_accounts(journal)
        txns = hl.print_json(journal)
        revision = source_revision(checkout)
        # Steps 2 and 3, in one write transaction for the whole entity.
        with write_transaction(engine) as conn:
            if extending:
                ctx = WriteCtx.open(conn, actor)
            else:
                ctx = provisioning.provision_entity(conn, actor=actor, id=spec.id, name=spec.name, legal_form=spec.legal_form,
                                                    country=spec.country, base_currency=spec.base_currency)
            account_ids = _provision(ctx, spec, declared, chart, year, extending=extending)
            restatements = []
            for txn in txns:  # already sorted by tindex
                if _fault is not None:
                    _fault(txn)
                if restates_a_period_already_held(conn, spec.id, txn):
                    restatements.append(_verify_restatement(ctx, txn, account_ids=account_ids, declared=declared))
                    continue
                _post(ctx, txn, account_ids=account_ids, declared=declared, checkout=checkout, revision=revision, year=year)
            counts = _counts(conn, spec.id, start, end)
            verified = tuple(r.as_json() for r in restatements)
            seq = events.append_event(ctx, kind="import.completed", subject_kind="import", subject_id=f"{spec.key}/{year}",
                                      after={"entity": spec.key, "start": start, "end": end, "source_revision": revision,
                                             "journal": str(journal), **counts, "restatements": list(verified)},
                                      reason="hledger journal imported; the journal is continuous")
        return ImportResult(entity=spec.key, entity_id=spec.id, db_path=str(db_path), start=start, end=end,
                            source_revision=revision, event_seq=seq, restatements=verified, **counts)
    finally:
        engine.dispose()


def result_dict(result: ImportResult | BlockedResult) -> dict[str, Any]:
    return asdict(result)
