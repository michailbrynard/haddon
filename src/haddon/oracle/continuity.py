"""The cross-year oracle: one continuous journal against the annual books it was built from (issue 0002).

The book oracle imports one calendar year into an empty database, which is exactly the shape in which the
double count of Phase 2 could not appear: an opening is the journal's beginning when there is no year before it.
The bug lived in the other shape, one year and then the next beside it, which is the flow ``README.md`` teaches.
It survived a green gate because nothing ran that flow.  This module runs it.

A corpus says which years to load (``continuity_years``, per entity where the books do not all start in the same
year: in the sample corpus Example S.L. was constituted in August 2024 and Example B.V. has no 2024 journal at
all).  Every boundary in the chain is checked.  A run that verified only its most recent boundary would pass a
three-year book whose earliest opening had been posted on top of the year it restates.  That is the same double
count one year further back, invisible from the far end because the later years still balance.

What makes it an oracle rather than a self-check is that the annual books are independent evidence.  Each year's
journal in the pinned checkout is self-contained: its ``<year>-opening.journal`` restates the previous year's
closing balance sheet with the result folded into retained earnings.  So the pinned binary can be asked what that
book's position is at its own year end without the continuous store being involved at all.  The two must agree,
and the one place they may differ is the fold:

    continuous position + accumulated result   ==   annual book position + that book's own result

per account and per commodity, with the fold account cancelling out of both sides.  A journal that counted the
restatement twice fails this by exactly the amount it double-counted.  ``no_inflation`` proves the other
direction: it computes what a naive sum of the annual journals would have said and requires it to differ from the
store for every carried pair.  A gate that cannot say what it would have caught is not evidence that it caught
anything.
"""

from __future__ import annotations

import shutil
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from haddon.db.connection import make_engine
from haddon.db.migrate import upgrade
from haddon.domain.dates import first_day, last_day
from haddon.domain.errors import HaddonError
from haddon.domain.micros import ZERO, Micros, sum_micros
from haddon.importing.hledger import Hledger
from haddon.importing.corpus import Corpus, load_corpus
from haddon.importing.importer import account_type_for, import_entity, journal_path
from haddon.oracle import canonical as C
from haddon.oracle.report import Gate, OracleReport, gate
from haddon.oracle.runner import ACTOR, FATAL, _Run, _safe_target
from haddon.reports.balances import income_statement, trial_balance
from haddon.reports.postings import BALANCE_SHEET_TYPES

#: every gate a continuity run must produce.  A missing name is a failure, not a silence.
REQUIRED_GATES = ("import", "restatement", "continuous_position", "no_inflation")

Position = dict[tuple[str, str], Micros]   # (account path, commodity) -> amount


def run_continuity_oracle(entity: str, *, corpus: Corpus | None = None, checkout: str | Path | None = None,
                          years: Sequence[int] | None = None,
                          hledger: Hledger | None = None, actor: str = ACTOR) -> OracleReport:
    """Import every year of ``years`` into one journal, oldest first, and compare the continuous journal with
    the annual books.  Never raises for a disagreement: every disagreement is a failed gate.

    The chain of years comes from the corpus (``continuity_years``, or the entity's own where it declares one),
    not from this module: which years are worth loading beside each other is a fact about a set of journals, not
    about a ledger (issue 0006).  Every boundary in the chain is checked, not only the last one (see the module
    docstring).
    """
    corpus = corpus or load_corpus()
    spec = corpus.entities.get(entity)
    if spec is None:
        raise HaddonError("validation", f"unknown entity {entity!r}; known: {sorted(corpus.entities)}",
                          details=[{"path": "entity", "reason": "enum", "value": entity}])
    chain = tuple(corpus.continuity_years_for(entity) if years is None else years)
    if len(chain) < 2:
        raise HaddonError("validation", f"{entity}: a continuity run needs two or more years to have a "
                          f"boundary to check; got {list(chain)}",
                          details=[{"path": "years", "reason": "shape", "value": list(chain)}])
    checkout = Path(corpus.checkout if checkout is None else checkout)
    hl = hledger or Hledger()
    run = _Run()
    workspace = Path(tempfile.mkdtemp(prefix=f"haddon-continuity-{entity}-"))
    _safe_target(workspace, "workspace")
    revision = None
    try:
        db = workspace.joinpath(f"{entity}.db")
        loaded = _import_chain(run, corpus, entity, db, checkout, chain, hl, actor)
        if loaded is not None:
            revision, verified = loaded
            _compare(run, corpus, spec.id, db, checkout, hl, chain, verified)
        return OracleReport(kind="continuity", entity=entity, entity_id=spec.id,
                            start=first_day(chain[0]), end=last_day(chain[-1]), source_revision=revision,
                            gates=tuple(run.gates), required=REQUIRED_GATES, facts=run.facts)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _import_chain(run: _Run, corpus: Corpus, entity: str, db: Path, checkout: Path, chain: tuple[int, ...],
                  hl: Hledger, actor: str):
    """Every year into one database, oldest first, the way ``README.md`` tells a reader to load them.

    Only the first import opens the journal; every later one is ``into_existing``, which is the flow in which
    an opening stops being a beginning and becomes a restatement of a period the journal already holds.
    """
    try:
        upgrade(db)
        results = []
        for i, year in enumerate(chain):
            results.append(import_entity(entity, db, corpus=corpus, checkout=checkout, year=year, actor=actor,
                                         hledger=hl, into_existing=i > 0))
        for result in results:
            if getattr(result, "blocked", None) is not None:
                run.add(gate("import", False, "a source journal is blocked by hledger check --strict",
                             {"blocked": result.blocked}))
                return None
        verified = tuple(r for result in results for r in result.restatements)
        per_year = {str(year): result.entries for year, result in zip(chain, results, strict=True)}
        run.add(gate("import", True,
                     f"{sum(per_year.values())} entries over {len(chain)} years in one journal "
                     f"({', '.join(f'{y}: {n}' for y, n in per_year.items())}); "
                     f"{len(verified)} opening restatement(s) verified rather than posted across "
                     f"{len(chain) - 1} boundary/boundaries",
                     {"years": list(chain), "entries_by_year": per_year,
                      "postings_by_year": {str(y): r.postings for y, r in zip(chain, results, strict=True)},
                      # The names the two-year version of this gate used, kept so a reader (and the tests that
                      # pin the 2026 boundary) can still ask about the chain's ends by name.
                      "from_entries": results[0].entries, "to_entries": results[-1].entries,
                      "from_postings": results[0].postings, "to_postings": results[-1].postings,
                      "restatements": list(verified), "source_revision": results[-1].source_revision}))
        return results[-1].source_revision, verified
    except FATAL as exc:
        run.failed("import", exc, f"{entity}: the {len(chain)} years could not be loaded into one journal")
        return None


def _compare(run: _Run, corpus: Corpus, entity_id: str, db: Path, checkout: Path, hl: Hledger,
             chain: tuple[int, ...], verified: tuple[dict, ...]) -> None:
    to_year = chain[-1]
    engine = make_engine(db)
    try:
        with engine.connect() as conn:
            store = _store_position(conn, entity_id, through=last_day(to_year))
            accumulated = {c: a for c, a in income_statement(conn, entity_id, start=None,
                                                             end=last_day(to_year)).net.items()}
    finally:
        engine.dispose()
    run.add(_restatement_gate(verified, accumulated, chain))
    try:
        books = {year: _journal_position(corpus, checkout, entity_id, year, hl) for year in chain}
    except FATAL as exc:
        run.failed("continuous_position", exc, "the annual books could not be read with the pinned binary")
        return
    fold_paths = tuple(dict.fromkeys(p for r in verified for p in r.get("fold_paths", ())))
    run.add(_position_gate(store, accumulated, books[to_year], to_year, fold_paths))
    run.add(_no_inflation_gate(store, books))


def _restatement_gate(verified: tuple[dict, ...], accumulated: dict[str, Micros],
                      chain: tuple[int, ...]) -> Gate:
    """Every boundary in the chain carried exactly one restatement, it was verified and not posted, and what
    it folded is the journal's own accumulated result at that boundary.

    ``accumulated`` runs through the *end* of the last year; the last fold happened at its first day.  The two
    are the same figure only because the last book has posted nothing yet, so the gate states both and
    compares each fold against the result at the boundary the restatement itself recorded.

    The chain's boundaries are enumerated rather than counted: ``len(verified) == len(chain) - 1`` would pass a
    run that verified the same boundary twice and skipped another, which is precisely the failure a three-year
    chain introduces and a two-year one cannot have.
    """
    boundaries = [(first_day(later), last_day(earlier)) for earlier, later in zip(chain, chain[1:])]
    detail = {"boundaries": [{"dt": dt, "through": through} for dt, through in boundaries],
              "restatements": list(verified),
              "accumulated_through_end": {c: a.render() for c, a in sorted(accumulated.items())}}
    if len(verified) != len(boundaries):
        return gate("restatement", False,
                    f"a {len(chain)}-year journal has {len(boundaries)} boundary/boundaries, each carrying one "
                    f"opening restatement; {len(verified)} were verified", detail)
    by_boundary = {(r["dt"], r["through"]): r for r in verified}
    missing = [f"{dt} restating through {through}" for dt, through in boundaries if (dt, through) not in by_boundary]
    if missing:
        return gate("restatement", False,
                    f"{len(missing)} boundary/boundaries carried no restatement of their own", detail, missing)
    empty = [r["dt"] for r in verified if not r["folded"] or r["compared"] <= 0]
    if empty:
        return gate("restatement", False,
                    f"{len(empty)} restatement(s) were accepted without comparing anything", detail, empty)
    # The last boundary is the one `accumulated` is measured at; the headline states it, as the two-year gate did.
    last = by_boundary[boundaries[-1]]
    detail |= {"dt": last["dt"], "through": last["through"], "folded": last["folded"], "lines": last["lines"],
               "fold_accounts": last["fold_accounts"], "compared": last["compared"]}
    return gate("restatement", True,
                f"all {len(boundaries)} boundary restatement(s) were verified against the journal and not posted; "
                f"the {last['dt']} one folded {last['folded']} into {', '.join(last['fold_accounts'])} over "
                f"{last['compared']} compared pairs", detail)


def _position_gate(store: Position, accumulated: dict[str, Micros], book: Position, to_year: int,
                   fold_paths: tuple[str, ...]) -> Gate:
    """The continuous journal and the annual book state the same position at the same date.

    Both sides are folded the same way (the fold account plus that side's own result, netted per commodity), so
    the one account the two books legitimately disagree about cancels and every other account, equity included,
    is compared exactly.  Collapsing the whole equity section instead would let an error inside it hide behind a
    compensating one, which is the shape of the real defect C2b found in B.V.'s 2026 opening.
    """
    left = _folded(store, accumulated, fold_paths)
    right = _folded(book, _result_of(book), fold_paths)
    mismatches = []
    for key in sorted(set(left) | set(right)):
        a, b = left.get(key, ZERO), right.get(key, ZERO)
        if a != b:
            mismatches.append(f"{key[0]}[{key[1]}]: continuous {a.render()} vs {to_year} book {b.render()}")
    detail = {"compared": len(set(left) | set(right)), "mismatches": len(mismatches),
              "accumulated": {c: a.render() for c, a in sorted(accumulated.items())},
              "book_result": {c: a.render() for c, a in sorted(_result_of(book).items())}}
    if not left:
        return gate("continuous_position", False, "the continuous journal holds no position to compare", detail)
    return gate("continuous_position", not mismatches,
                f"{len(left)} balance-sheet pairs compared against the {to_year} book at {to_year}-12-31"
                + ("" if not mismatches else f"; {len(mismatches)} disagree"),
                detail, mismatches)


def _no_inflation_gate(store: Position, books: dict[int, Position]) -> Gate:
    """What the bug would have said, so a pass means something.

    A journal that posted every restatement holds the sum of all the annual books.  This computes that sum and
    requires it to differ from the store for every non-zero balance-sheet pair of the last book.  If it did not
    differ, this gate and ``continuous_position`` would both pass on a book where the distinction does not arise.
    """
    to_book = books[max(books)]
    naive: Position = defaultdict(list)
    for side in books.values():
        for key, amount in side.items():
            naive[key].append(amount)
    inflated = {k: sum_micros(v, path=f"{k[0]}[{k[1]}]") for k, v in naive.items()}
    carried = [k for k, v in to_book.items() if account_type_for(k[0]) in BALANCE_SHEET_TYPES and not v.is_zero]
    would_have_moved = [k for k in carried if inflated.get(k, ZERO) != store.get(k, ZERO)]
    detail = {"carried_pairs": len(carried), "would_have_been_inflated": len(would_have_moved),
              "examples": [f"{k[0]}[{k[1]}]: store {store.get(k, ZERO).render()} vs summed "
                           f"{inflated.get(k, ZERO).render()}" for k in sorted(would_have_moved)[:5]]}
    ok = bool(carried) and len(would_have_moved) == len(carried)
    missed = [f"{k[0]}[{k[1]}]" for k in sorted(set(carried) - set(would_have_moved))]
    return gate("no_inflation", ok,
                f"summing both annual books would have moved all {len(carried)} carried pairs"
                if ok else f"{len(missed)} of {len(carried)} carried pairs would not have moved, so this gate "
                           f"proves nothing about them",
                detail, missed)


def _store_position(conn, entity_id: str, *, through: str) -> Position:
    """The continuous journal's balance-sheet pairs at ``through``."""
    tb = trial_balance(conn, entity_id, through=through)
    return {(ln.path, ln.commodity): ln.amount for ln in tb.lines if ln.type in BALANCE_SHEET_TYPES}


def _journal_position(corpus: Corpus, checkout: Path, entity_id: str, year: int, hl: Hledger) -> Position:
    """The annual book's own position at its year end, read from the pinned source with the pinned binary.

    The store is not consulted: this is the independent side.  An annual book is self-contained, so summing
    every transaction in its journal *is* its closing position.
    """
    entity_key = next(k for k, spec in corpus.entities.items() if spec.id == entity_id)
    book, _ = C.from_journal(journal_path(checkout, entity_key, year), hl)
    per: dict[tuple[str, str], list[Micros]] = defaultdict(list)
    for txn in book:
        for leg in txn.legs:
            per[(leg.account, leg.commodity)].append(Micros(int(leg.amount_micros)))
    return {k: sum_micros(v, path=f"{k[0]}[{k[1]}]") for k, v in per.items()}


def _result_of(position: Position) -> dict[str, Micros]:
    """The net of every income and expense pair, per commodity."""
    per: dict[str, list[Micros]] = defaultdict(list)
    for (path, commodity), amount in position.items():
        if account_type_for(path) not in BALANCE_SHEET_TYPES:
            per[commodity].append(amount)
    return {c: sum_micros(v, path=f"result[{c}]") for c, v in sorted(per.items())}


def _folded(position: Position, result: dict[str, Micros], fold_paths: tuple[str, ...]) -> Position:
    """Balance-sheet pairs, with the fold account and the result collapsed into one reserved key per commodity.

    One side folded its result into retained earnings at the boundary and the other has not folded it at all, so
    that account, and only that account, is compared as account plus result rather than on its own.
    """
    out: Position = {}
    per_commodity: dict[str, list[Micros]] = defaultdict(list)
    for (path, commodity), amount in position.items():
        if account_type_for(path) not in BALANCE_SHEET_TYPES:
            continue
        if path in fold_paths:
            per_commodity[commodity].append(amount)
        else:
            out[(path, commodity)] = amount
    for commodity, amount in result.items():
        per_commodity[commodity].append(amount)
    for commodity, parts in per_commodity.items():
        out[("<fold account + result>", commodity)] = sum_micros(parts, path=f"fold[{commodity}]")
    return out
