"""Running the oracle: import -> export -> reparse -> gates, in a workspace nothing else can see (chunk C2b).

One requirement shapes this module: a failing oracle must leave the target entity unimported, with no partial
state.  So the run never imports into the caller's database.  It builds everything inside a private temporary
workspace, runs every gate, and copies the verified database to ``into`` only when the report is ``ok``.  A corrupt
source, a blocked export, a tampered artifact or a single disagreeing amount all end the same way: nonzero, nothing
published, workspace removed.

What is compared:

* source: the corpus's read-only checkout, read with the pinned hledger binary.  C2a's ``protected_root_for``
  guard is reused (never disabled) to refuse a workspace or a publish target inside it.
* store: what ``haddon.importing`` put in the database, read back through ``reports.postings`` (the same query
  the export and the reports use).
* reparsed export: what ``haddon.exporting`` wrote, read back with the same extractor as the source.

A run compares one calendar year (``--year``, or the corpus's default) against the source journals.  The
carry-forward oracle went with the annual model (issue 0002): there is no second book to carry into, so its
baseline went too.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from haddon.db.connection import make_engine
from haddon.db.migrate import upgrade
from haddon.domain.dates import first_day, last_day
from haddon.domain.errors import HaddonError
from haddon.exporting.exporter import ArtifactMismatch, ExportBlocked, export_book, protected_root_for
from haddon.importing.hledger import Hledger, HledgerBinaryError, HledgerError, HledgerShapeError
from haddon.importing.corpus import Corpus, load_corpus
from haddon.importing.importer import ImportBlocked, import_entity, journal_path
from haddon.oracle import canonical as C
from haddon.oracle import gates as G
from haddon.oracle.report import REQUIRED_GATES, Gate, OracleReport, gate

ACTOR = "agent:haddon-oracle"
# Errors that mean the run cannot get as far as comparing.  Each becomes a failed gate, never an escaped exception.
FATAL = (ImportBlocked, ExportBlocked, ArtifactMismatch, HaddonError, HledgerError, HledgerShapeError,
         HledgerBinaryError, ValueError, OSError)


@dataclass(frozen=True)
class Baseline:
    """The pinned figures for one entity and range, measured from the corpus journals at the pinned checkout.

    They are what makes a run an oracle rather than a self-consistency check: every one of them moves if the
    source, the import or the export changes.  ``docs/design/decisions.md`` records how they were derived."""

    entity: str
    entity_id: str
    start: str
    end: str
    entries: int
    postings: int
    pairs: int
    monthly_pairs: int
    accounts: int
    commodities: int
    source_files: int
    canonical_sha256: str   # the modelled core: dates, narrations, statuses, codes, comments, tags, ordered legs, micros
    totals_sha256: str      # account/commodity totals
    monthly_sha256: str     # account/commodity/month totals
    mapping_sha256: str     # journal_mappings.canonical_txn: the full-fidelity record C1 wrote, unchanged by E1
    txn_comments: int
    txn_tags: int
    txn_codes: int
    posting_statuses: dict[str, int]
    memo_normalised: int    # postings whose memo round trips only up to a trailing newline (see gates.posting_memos)

    @property
    def pins(self) -> dict[str, Any]:
        """The subset :func:`gates.baseline_gate` compares (everything but the identifying fields)."""
        skip = {"entity", "entity_id", "start", "end", "memo_normalised"}
        return {k: v for k, v in asdict(self).items() if k not in skip}

    @property
    def note_counts(self) -> dict[str, int]:
        """The pinned extent of the transaction note fields, as :func:`gates.round_trip_gate` compares it."""
        return {"with_comment": self.txn_comments, "with_tags": self.txn_tags, "with_code": self.txn_codes}


def _safe_target(path: Path, what: str) -> Path:
    """Refuse to write anywhere C2a marked protected.

    The guard is reused, never disabled: the read-only source checkout and the pinned verifier are not places an
    oracle run may put anything.  The path is checked as written.  Resolving it first would follow a symlink out
    of a protected root (a symlinked checkout -> a directory elsewhere) and miss the protection entirely.
    """
    raw = Path(path)
    root = protected_root_for(raw)
    if root is not None:
        raise ValueError(f"{what} {raw} is inside {root}, which is read-only for every Haddon command")
    return raw


@dataclass
class _Run:
    """Mutable scratch for one run: the gates collected so far and the facts the report will carry."""

    gates: list[Gate] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def add(self, g: Gate) -> Gate:
        self.gates.append(g)
        return g

    def failed(self, name: str, exc: BaseException, summary: str) -> Gate:
        return self.add(gate(name, False, summary, {"error": type(exc).__name__, "message": str(exc)[:4000]}))


def run_oracle(entity: str, *, corpus: Corpus | None = None, checkout: str | Path | None = None,
               year: int | None = None, hledger: Hledger | None = None,
               into: str | Path | None = None, keep: str | Path | None = None, actor: str = ACTOR,
               baseline: Baseline | None = None,
               _after_export: Callable[[G.Views], None] | None = None,
               _mutate: Callable[[G.Views], G.Views] | None = None) -> OracleReport:
    """Compare ``entity``'s ``year`` range three ways and return the report.  Never raises for a disagreement.

    ``into`` is written only when every required gate passes, so a failing run leaves the target entity
    unimported.  ``keep`` keeps the workspace (database + exported journals) for inspection, again only on a pass.
    ``_after_export`` and ``_mutate`` are test hooks: the first runs against the published artifacts before the
    gates (so a tampered evidence hash can be shown to fail), the second rewrites the three read representations
    (so a same-month date swap can be shown to fail while every total still matches).
    """
    corpus = corpus or load_corpus()
    year = corpus.default_year if year is None else year
    spec = corpus.entities.get(entity)
    if spec is None:
        raise HaddonError("validation", f"unknown entity {entity!r}; known: {sorted(corpus.entities)}",
                          details=[{"path": "entity", "reason": "enum", "value": entity}])
    checkout = Path(corpus.checkout if checkout is None else checkout)
    if into is not None:
        into = _safe_target(Path(into), "publish target")
    if keep is not None:
        keep = _safe_target(Path(keep), "workspace")
    start, end = first_day(year), last_day(year)
    if baseline is None:
        baseline = load_baselines(corpus).get((entity, start, end))
    hl = hledger or Hledger()
    run = _Run()
    workspace = Path(tempfile.mkdtemp(prefix=f"haddon-oracle-{entity}-"))
    _safe_target(workspace, "workspace")
    revision: str | None = None
    try:
        views = _build(run, corpus, spec, entity, year, start, end, checkout, workspace, hl, actor,
                       _after_export)
        if views is not None:
            revision = views.export.source_revision
            if _mutate is not None:
                views = _mutate(views)
            _compare(run, views, workspace, baseline, run_facts=run.facts)
        report = OracleReport(kind="book", entity=entity, entity_id=spec.id, start=start, end=end,
                              source_revision=revision, gates=tuple(run.gates), required=REQUIRED_GATES, facts=run.facts)
        if report.ok and (into is not None or keep is not None):
            report = _publish(report, workspace, into=into, keep=keep, entity=entity)
        return report
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _build(run: _Run, corpus: Corpus, spec, entity: str, year: int, start: str, end: str, checkout: Path,
           workspace: Path, hl: Hledger, actor: str, _after_export) -> G.Views | None:
    """Import, export and read the three representations inside the workspace.

    If any step fails there is nothing to compare.  The run ends with that step recorded as a failed gate, which
    is never ``ok`` because the rest of the required gates are then missing."""
    db = workspace.joinpath(f"{entity}.db")
    source = journal_path(checkout, spec.key, year)
    try:
        upgrade(db)
        result = import_entity(entity, db, corpus=corpus, checkout=checkout, year=year, actor=actor,
                               hledger=hl)
        if getattr(result, "blocked", None) is not None:
            run.add(gate("import", False, "the source journal is blocked by hledger check --strict",
                         {"blocked": result.blocked}))
            return None
        run.add(gate("import", True, f"{result.entries} entries / {result.postings} postings imported for "
                                     f"{result.start}..{result.end} from {result.source_revision[:12]}",
                     {"entries": result.entries, "postings": result.postings, "pairs": result.pairs,
                      "accounts": result.accounts, "journal_mappings": result.journal_mappings,
                      "origins": result.origins, "source_revision": result.source_revision}))
    except FATAL as exc:
        run.failed("import", exc, f"{entity}: the source could not be imported")
        return None
    try:
        export = export_book(db, workspace.joinpath("export"), year=year, actor=actor, hledger=hl)
        run.add(gate("export", True, export.summary(),
                     {"ledger_dir": export.ledger_dir, "state_hash": export.state_hash, "counts": export.counts}))
    except FATAL as exc:
        run.failed("export", exc, f"{entity}: the book could not be exported")
        return None
    try:
        source_book, source_txns = C.from_journal(source, hl)
        source_declared = hl.declared_commodities(source)
        source_micros = {x.index: C.micros_of(x, source_declared) for x in source_txns}
        exported_main = Path(export.ledger_dir).joinpath(f"{year}.journal")
        reparsed_book, reparsed_txns = C.from_journal(exported_main, hl)
        reparsed_declared = hl.declared_commodities(exported_main)
        reparsed_micros = {x.index: C.micros_of(x, reparsed_declared) for x in reparsed_txns}
        engine = make_engine(db)
        try:
            with engine.connect() as conn:
                store_book, store_by_id = C.from_store(conn, start=start, end=end)
                mappings = G.mapping_rows(conn, spec.id, start=start, end=end)
                store_memos = G.memos_of_store(conn, spec.id, start=start, end=end)
        finally:
            engine.dispose()
    except FATAL as exc:
        run.failed("read", exc, f"{entity}: the three representations could not be read")
        return None
    views = G.Views(entity=entity, entity_id=spec.id, start=start, end=end, checkout=checkout, db_path=db,
                    source_journal=source, export=export, source=source_book, source_txns=source_txns,
                    source_micros=source_micros, store=store_book, store_by_id=store_by_id, reparsed=reparsed_book,
                    reparsed_txns=reparsed_txns, mappings=mappings,
                    memos={"source": G.memos_of_journal(source_txns, source_micros), "store": store_memos,
                           "export": G.memos_of_journal(reparsed_txns, reparsed_micros)})
    if _after_export is not None:
        _after_export(views)
    return views


def _compare(run: _Run, v: G.Views, workspace: Path, baseline: Baseline | None, run_facts: dict) -> None:
    run_facts["workspace"] = str(workspace)
    run_facts["ledger_dir"] = str(v.ledger_dir)
    run.add(G.counts_gate(v))
    run.add(G.totals_gate(v))
    run.add(G.monthly_totals_gate(v))
    run.add(G.canonical_gate(v))
    engine = make_engine(v.db_path)
    try:
        with engine.connect() as conn:
            run.add(G.coverage_gate(v))
            run.add(G.posting_memos_gate(v, expect_normalised=None if baseline is None else baseline.memo_normalised))
            run.add(G.round_trip_gate(conn, v, expected=None if baseline is None else baseline.note_counts))
            run.add(G.artifacts_gate(v))
            measured = G.fingerprint(conn, v)
    finally:
        engine.dispose()
    run.add(G.baseline_gate(measured, None if baseline is None else baseline.pins))
    run_facts["fingerprint"] = measured


def _copy_database(src: Path, dst: Path) -> None:
    """Publish a database by copying it as a database, not as a file.

    The workspace database is in WAL mode, so everything committed since the last checkpoint lives in the
    ``-wal`` sibling and not in the file itself.  ``shutil.copy`` copies only the file, so it can publish a
    database with every table missing while the run that produced it reported success.  Every gate read it
    through a connection that could see main and WAL, so nothing noticed.  SQLite checkpoints on last connection
    close, but only as best effort: it is skipped while any other connection is open, and whether one is open
    depends on when the interpreter last collected a reference cycle.  A published artifact must not depend on
    that.  ``PRAGMA wal_checkpoint`` does not help either, since it reports busy and carries on when a reader is
    active.

    SQLite's backup API has none of those failure modes: it reads the database through the pager, so the copy
    includes the WAL and is consistent against concurrent access.
    """
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _publish(report: OracleReport, workspace: Path, *, into: Path | None, keep: Path | None,
             entity: str) -> OracleReport:
    """Copy the verified database to ``into`` and the workspace to ``keep``.

    Only ever called on a fully passing report.  A failing run has nothing to publish, which is what "leaves the
    target entity unimported" means."""
    published = None
    source = workspace.joinpath(f"{entity}.db")
    if into is not None:
        into.parent.mkdir(parents=True, exist_ok=True)
        _copy_database(source, into)
        published = str(into)
    if keep is not None:
        keep.mkdir(parents=True, exist_ok=True)
        _copy_database(source, keep.joinpath(f"{entity}.db"))
        dst = keep.joinpath("export")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(workspace.joinpath("export"), dst)
    from dataclasses import replace
    return replace(report, published=published, facts={**report.facts, "kept": str(keep) if keep else None})


def load_baselines(corpus: Corpus | None = None) -> dict[tuple[str, str, str], Baseline]:
    """The pinned figures of one corpus, read from the file its manifest names.

    They are measured from a particular set of journals, so they belong with those journals rather than in the
    package (issue 0006): ``corpus/example-baselines.json`` sits beside the manifest that points at it.  A
    corpus that names none simply has no pins, and the baseline gate says so rather than passing vacuously.
    """
    corpus = corpus or load_corpus()
    found: dict[tuple[str, str, str], Baseline] = {}
    if corpus.baselines is None or not corpus.baselines.is_file():
        return found
    body = json.loads(corpus.baselines.read_text(encoding="utf-8"))
    for item in body["baselines"]:
        b = Baseline(**item)
        found[(b.entity, b.start, b.end)] = b
    return found
