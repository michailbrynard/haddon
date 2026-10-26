"""Export one entity's book back out of the store as hledger journal files (chunk C2a).

The mirror image of :mod:`haddon.importing.importer`, and deliberately ordered the other way round.  The store is
read first, the artifacts are rendered and verified outside any write transaction, and only once they are durable
on disk does one short ``write_transaction`` record what happened.

1. Read the book.  One read-only connection: the entity, the declared chart and commodities, and the
   ``status='posted'`` postings in the range, through ``reports.postings.posted_postings``.  That query excludes
   drafts and voids.  An original that was reversed is still posted, so it appears next to its reversal, which is
   what the store means by a correction.
2. Render (pure, :mod:`haddon.exporting.render`): ``{filename: text}``, ordered, with every amount taken from
   integer micros and the commodity's declared precision.  Two renders of the same state are the same bytes.
   Since chunk E1 this includes the transaction-level note fields (``entries.code`` / ``comment`` / ``tags``), so
   a source journal imported and exported again carries its codes, comments and tags back out.
3. Stage and verify.  The files are written into a staging directory, fsynced, and put to
   ``hledger check --strict`` with the pinned binary (re-verified before the run by ``hledger.resolve_binary``).
   A strict-check failure is an export failure (:class:`ExportBlocked`): nothing is published or recorded.
4. Publish.  The staged files are moved into ``<out>/<Entity>/<label>/ledger/`` and the published bytes are
   re-hashed and compared to the staged digests, so the row that follows describes exactly the bytes that passed
   the verifier.
5. Record.  One ``write_transaction``: an ``export`` operation, a snapshot carrying the state hash and the
   manifest, the immutable ``exports`` row (:mod:`haddon.repository.exports`) and an ``export.completed`` event
   carrying the whole manifest.  Every write goes through the repository API.

The export is minted by whoever is acting, on their own authority (``docs/design/decisions.md``,
"Authorisation").  It reads the book and writes files and decides no accounting question, so it records no
proposal.  ``actor`` is one opaque string, present and non-empty, recorded verbatim on every row it writes.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.db.connection import make_engine
from haddon.domain.canonical import canonical_json, parse_tags, sha256_hex
from haddon.domain.dates import first_day, last_day
from haddon.domain.errors import validation
from haddon.domain.micros import Micros
from haddon.exporting.render import (MARK_BY_SOURCE_STATUS, POSTED_MARK, SCHEMA, STORE_GROUP, BookState, RPosting,
                                     RTransaction, group_for, render_files, state_hash)
from haddon.importing.hledger import REPO_ROOT, Hledger, HledgerError, resolve_binary, sha256_file
from haddon.reports.postings import posted_postings
from haddon.reports.scope import scope
from haddon.repository import closing, events, exports, provisioning, requests
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction

ACTOR = "agent:haddon-export"
#: Directories an export must never write into, whatever a caller asks for.  The pinned verifier is always one of
#: them.  The rest come from the configured corpus, which is where a read-only source checkout is declared rather
#: than as a literal here (issue 0006).  The vendor tree is listed here, not in a manifest, because it belongs to
#: this repository.
ALWAYS_PROTECTED = (REPO_ROOT.joinpath("vendor"),)


def protected_roots() -> tuple[Path, ...]:
    """The vendored binary, plus every tree the configured corpus declares read-only.

    Read per call rather than at import: a process with no corpus configured still protects the verifier, and
    one with a corpus protects its source checkout without the exporter knowing which company's it is.
    """
    from haddon.importing.corpus import configured_path, load_corpus

    if configured_path() is None:
        return ALWAYS_PROTECTED
    try:
        return ALWAYS_PROTECTED + tuple(load_corpus().read_only_roots)
    except Exception:  # noqa: BLE001 - a broken manifest must not make the guard *weaker* than it was
        return ALWAYS_PROTECTED


class ExportBlocked(RuntimeError):
    """``hledger check --strict`` refused the export's own output.  Nothing was published and nothing recorded."""

    def __init__(self, entity_id: str, journal: Path, error: HledgerError) -> None:
        self.entity_id = entity_id
        self.journal = journal
        self.error = error
        super().__init__(f"{entity_id}: hledger check --strict failed on the exported journal {journal}; "
                         f"nothing published.\n{error.stderr.rstrip()}")


class ArtifactMismatch(RuntimeError):
    """A published file does not hash to what was verified (or is missing).  Named per file, never summarised."""


@dataclass(frozen=True)
class Artifact:
    path: str  # file name, relative to the ledger directory
    sha256: str
    bytes: int

    def as_json(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True)
class ExportResult:
    entity_id: str
    entity_dir: str
    start: str
    end: str
    ledger_dir: str
    export_id: str
    snapshot_id: str
    snapshot_seq: int
    operation_id: str
    state_hash: str
    manifest_sha256: str
    source_revision: str
    artifacts: tuple[Artifact, ...]
    counts: dict[str, Any]
    verifier: dict[str, Any]
    manifest: dict[str, Any]
    event_seq: int

    def summary(self) -> str:
        c = self.counts
        return (f"{self.entity_id} {self.start}..{self.end} -> {self.ledger_dir}: files={c['files']} entries={c['entries']} "
                f"postings={c['postings']} accounts={c['accounts']} groups={c['groups']} "
                f"state={self.state_hash[:12]} manifest={self.manifest_sha256[:12]} "
                f"verifier={self.verifier['result']} ({self.verifier['version']}) snapshot_seq={self.snapshot_seq}")


def _mappings(conn: Connection, entity_id: str, *, start: str | None, end: str | None) -> dict[str, tuple[str, str]]:
    """``entry_id -> (journal_path, source_status)`` for the imported entries dated in the range.  A second,
    disagreeing row for one entry is an error rather than a coin toss."""
    m, e = t.journal_mappings, t.entries
    stmt = (sa.select(m.c.entry_id, m.c.journal_path, m.c.source_status)
            .select_from(m.join(e, m.c.entry_id == e.c.id))
            .where(m.c.entity_id == entity_id)
            .order_by(m.c.entry_id))
    if start is not None:
        stmt = stmt.where(e.c.dt >= start)
    if end is not None:
        stmt = stmt.where(e.c.dt <= end)
    out: dict[str, tuple[str, str]] = {}
    for entry_id, journal_path, source_status in conn.execute(stmt):
        found = (journal_path, source_status)
        if out.setdefault(entry_id, found) != found:
            raise validation(f"entry {entry_id!r} has two disagreeing journal_mappings rows", path="journal_mappings",
                             reason="mapping_conflict", entry_id=entry_id)
    return out


def _source_revision(conn: Connection, entity_id: str, *, start: str | None, end: str | None) -> str:
    """The import revision the entries in this range came from.  One database is one continuous journal, so the
    range must not mix revisions; an empty range (nothing imported) reports no revision."""
    m, e = t.journal_mappings, t.entries
    stmt = (sa.select(m.c.source_revision).distinct()
            .select_from(m.join(e, m.c.entry_id == e.c.id))
            .where(m.c.entity_id == entity_id))
    if start is not None:
        stmt = stmt.where(e.c.dt >= start)
    if end is not None:
        stmt = stmt.where(e.c.dt <= end)
    revisions = sorted(r[0] for r in conn.execute(stmt))
    if len(revisions) > 1:
        raise validation(f"the range {start}..{end} mixes import revisions {revisions}", path="source_revision",
                         reason="revision_conflict")
    return revisions[0] if revisions else ""


def entity_dir_for(entity_id: str, mappings: dict[str, tuple[str, str]]) -> str:
    """The directory the artifacts go under: the first path segment the import recorded (``ExampleInc/2025/...``),
    so an export lands under the same name the source used.  A book with no imported entry uses the entity id."""
    segments = sorted({PurePosixPath(path).parts[0] for path, _ in mappings.values() if PurePosixPath(path).parts})
    if not segments:
        return entity_id
    if len(segments) > 1:
        raise validation(f"journal_mappings name more than one entity directory: {segments}", path="journal_mappings",
                         reason="entity_dir_conflict")
    name = segments[0]
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        raise validation(f"journal_mappings entity directory {name!r} is not a plain directory name",
                         path="journal_mappings", reason="entity_dir_shape")
    return name


def _posted_extent(conn: Connection, entity_id: str) -> tuple[str | None, str | None]:
    """The first and last date posted, or ``(None, None)`` when nothing is."""
    e = t.entries
    row = conn.execute(sa.select(sa.func.min(e.c.dt), sa.func.max(e.c.dt))
                       .where(e.c.entity_id == entity_id, e.c.status == "posted")).first()
    return (None, None) if row is None else (row[0], row[1])


def read_book_state(conn: Connection, *, start: str | None = None, end: str | None = None) -> BookState:
    """Everything the export renders, read once from the store under one date range.  An open bound is filled
    from the posted extent, so the state always names the range it actually covers (an empty journal names none)."""
    entity = provisioning.read_entity(conn)
    sc = scope(conn, entity.id, start=start, end=end)
    lo, hi = _posted_extent(conn, entity.id)
    start = sc.start if sc.start is not None else lo
    end = sc.end if sc.end is not None else hi
    commodities = provisioning.all_commodities(conn, entity.id)
    accounts = provisioning.all_accounts(conn, entity.id)
    mappings = _mappings(conn, entity.id, start=start, end=end)
    rows = posted_postings(conn, entity.id, start=start, end=end)
    by_entry: dict[str, list] = {}
    for r in rows:
        by_entry.setdefault(r.entry_id, []).append(r)
    txns = []
    for entry_id, legs in by_entry.items():
        journal_path, source_status = mappings.get(entry_id, (None, None))
        if source_status is not None and source_status not in MARK_BY_SOURCE_STATUS:
            raise validation(f"entry {entry_id!r} has source status {source_status!r}", path="source_status", reason="enum")
        head = legs[0]
        txns.append(RTransaction(
            entry_id=entry_id, dt=head.dt, narration=head.narration, origin=head.origin,
            mark=POSTED_MARK if journal_path is None else MARK_BY_SOURCE_STATUS[source_status],
            group=STORE_GROUP if journal_path is None else group_for(journal_path),
            code=head.code, comment=head.comment, tags=parse_tags(head.tags),
            postings=tuple(RPosting(account=accounts[l.account_id].path, commodity=l.commodity,
                                    amount=Micros(int(l.amount_micros)), memo=l.memo)
                           for l in sorted(legs, key=lambda l: l.seq))))
    return BookState(
        entity_id=entity.id, entity_name=entity.name, entity_dir=entity_dir_for(entity.id, mappings),
        start=start, end=end,
        source_revision=_source_revision(conn, entity.id, start=start, end=end),
        commodities=tuple(commodities[code] for code in sorted(commodities)),
        accounts=tuple(sorted(a.path for a in accounts.values())),
        transactions=tuple(sorted(txns, key=lambda x: x.sort_key)))


def is_managed(name: str, *, label: str) -> bool:
    """The files this export owns in the ledger directory: nothing else is ever written, moved or deleted."""
    return name in (f"{label}.journal", "accounts.journal") or (name.startswith(f"{label}-") and name.endswith(".journal"))


def protected_root_for(path: Path, roots: tuple[Path, ...] | None = None) -> Path | None:
    """The protected root ``path`` lies in, if any.

    A pure predicate, so the real paths can be tested without pointing a write at them.  The candidate and each
    root are compared both as written and resolved, so a symlink into a protected tree (``corpus/example`` ->
    elsewhere) is refused as well as its resolved target.  A candidate that resolves outside every root but is
    written through one is still caught, which a caller that resolved first would miss.
    """
    path = Path(path)
    candidates = [path]
    try:
        resolved = path.resolve()
        if resolved != path:
            candidates.append(resolved)
    except OSError:  # pragma: no cover - resolve(strict=False) rarely raises; keep the guard total
        pass
    for root in protected_roots() if roots is None else roots:
        root = Path(root)
        held = [root]
        try:
            held.append(root.resolve())
        except OSError:  # pragma: no cover
            pass
        for cand in candidates:
            if any(cand == h or h in cand.parents for h in held):
                return root
    return None


def _check_destination(ledger_dir: Path, *, label: str, overwrite: bool) -> None:
    root = protected_root_for(ledger_dir)
    if root is not None:
        raise validation(f"{ledger_dir} is inside {root}, which an export never writes to", path="out_root",
                         reason="protected_root")
    if not ledger_dir.exists():
        return
    if not ledger_dir.is_dir():
        raise validation(f"{ledger_dir} exists and is not a directory", path="out_root", reason="not_a_directory")
    present = sorted(p.name for p in ledger_dir.iterdir())
    unmanaged = [n for n in present if not is_managed(n, label=label)]
    if unmanaged:
        raise validation(f"{ledger_dir} holds files this export does not manage: {unmanaged}", path="out_root",
                         reason="unmanaged_files")
    if present and not overwrite:
        raise validation(f"{ledger_dir} already holds an export ({len(present)} file(s)); pass overwrite=True to replace it",
                         path="out_root", reason="exists")


def _stage(files: dict[str, str], staging: Path) -> tuple[Artifact, ...]:
    """Write every file, fsync it, and digest exactly the bytes on disk."""
    artifacts = []
    for name, text in files.items():
        path = staging.joinpath(name)
        data = text.encode("utf-8")
        with path.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        artifacts.append(Artifact(path=name, sha256=sha256_file(path), bytes=len(data)))
    return tuple(artifacts)


def _publish(staging: Path, ledger_dir: Path, artifacts: tuple[Artifact, ...], *, label: str) -> None:
    """Replace the managed files in ``ledger_dir`` with the staged ones."""
    ledger_dir.mkdir(parents=True, exist_ok=True)
    for existing in sorted(ledger_dir.iterdir()):  # only ever managed names: _check_destination refused the rest
        if existing.is_file() and is_managed(existing.name, label=label):
            existing.unlink()
    for artifact in artifacts:
        os.replace(staging.joinpath(artifact.path), ledger_dir.joinpath(artifact.path))
    # fsync the directory too, so the renames themselves survive a crash.
    fd = os.open(ledger_dir, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_artifacts(ledger_dir: Path, artifacts: list[dict[str, Any]] | tuple[Artifact, ...]) -> list[str]:
    """Re-hash published files against a manifest's artifact list.  Returns one problem string per file that is
    missing, short, long or different; empty when the directory is exactly what was recorded."""
    items = [a.as_json() if isinstance(a, Artifact) else a for a in artifacts]
    problems = []
    for item in items:
        path = Path(ledger_dir).joinpath(item["path"])
        if not path.is_file():
            problems.append(f"{item['path']}: missing")
            continue
        size, digest = path.stat().st_size, sha256_file(path)
        if size != item["bytes"]:
            problems.append(f"{item['path']}: {size} bytes on disk, {item['bytes']} recorded")
        if digest != item["sha256"]:
            problems.append(f"{item['path']}: sha256 {digest} on disk, {item['sha256']} recorded")
    extra = sorted({p.name for p in Path(ledger_dir).iterdir() if p.is_file()} - {i["path"] for i in items})
    problems += [f"{name}: present on disk but not in the manifest" for name in extra]
    return problems


def export_book(db_path: str | Path, out_root: str | Path, *, year: int | None = None,
                start: str | None = None, end: str | None = None, actor: str = ACTOR,
                overwrite: bool = False, hledger: Hledger | None = None, request_id: str | None = None,
                also_record: Callable[[WriteCtx, "ExportResult"], None] | None = None,
                _mutate: Callable[[BookState], BookState] | None = None) -> ExportResult:
    """Export a date range of the entity database at ``db_path`` into ``<out_root>/<Entity>/<label>/ledger/``.

    A journal is continuous (issue 0002), so the range is any inclusive range and either bound may be left open;
    an open bound is filled from what is actually posted.  ``year`` is only the calendar-year shorthand: exactly
    ``start=first_day(year), end=last_day(year)``, and it may not be combined with them.  The default is the whole
    journal, not a calendar year.  A default year would be a storage identity wearing a keyword argument, which is
    what issue 0002 removed.  The label, the snapshot and the manifest describe the range, not a year.

    ``overwrite`` replaces an export already in that directory (managed files only; an unmanaged file there is
    refused).  ``_mutate`` is a test hook that rewrites the read state before rendering, so the strict gate can be
    shown to fail on output this exporter itself produced.

    ``also_record`` is called with the finished :class:`ExportResult` inside the same write transaction that
    records the export, before it commits.  It is for a caller with its own row to write about the same act: the
    HTTP route writes the ``requests`` row that makes a retry replay rather than export twice.  It is a hook rather
    than a second transaction because the two must not come apart.  A recorded export whose recorded reply is
    missing is exactly the state the idempotency contract exists to prevent.

    The hook gets the ``WriteCtx`` the export wrote through, so whatever it writes carries the same actor and
    ``request_id``.  The caller pre-allocates that ``request_id`` and passes it in, so the events this export
    writes link to the request row that commits beside them, as ``execution.execute_once`` arranges for every other
    write.  Raising from the hook rolls the whole record back.  The files are already published by then: artifacts
    first, the row that describes them second.
    """
    if year is not None and (start is not None or end is not None):
        raise validation("year is shorthand for a calendar-year range; give it or give start/end, not both",
                         path="year", reason="range_conflict", year=year, start=start, end=end)
    if year is not None:
        start, end = first_day(year), last_day(year)
    db_path, out_root = Path(db_path), Path(out_root)
    # Guard the destination as written, before resolving: resolving follows a symlink into the read-only checkout
    # out of the protected root and would let an export write there.
    guard = protected_root_for(out_root)
    if guard is not None:
        raise validation(f"{out_root} is inside {guard}, which an export never writes to", path="out_root",
                         reason="protected_root")
    out_root = out_root.resolve()
    hl = hledger or Hledger()
    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            state = read_book_state(conn, start=start, end=end)
        if _mutate is not None:
            state = _mutate(state)
        label = state.label
        files = render_files(state)
        digest = state_hash(state)
        ledger_dir = out_root.joinpath(state.entity_dir, label, "ledger")
        _check_destination(ledger_dir, label=label, overwrite=overwrite)
        ledger_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".haddon-export-", dir=ledger_dir.parent))
        try:
            artifacts = _stage(files, staging)
            main = staging.joinpath(f"{label}.journal")
            try:
                hl.check_strict(main)
            except HledgerError as exc:
                raise ExportBlocked(state.entity_id, main, exc) from exc
            binary = resolve_binary(hl.binary, hl.pin_file)
            _publish(staging, ledger_dir, artifacts, label=label)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        problems = verify_artifacts(ledger_dir, artifacts)
        if problems:
            raise ArtifactMismatch(f"published artifacts in {ledger_dir} do not match what was verified:\n" + "\n".join(problems))
        verifier = {"tool": "hledger", "version": binary.version, "binary_sha256": binary.sha256,
                    "command": ["check", "--strict"], "journal": f"{label}.journal", "result": "pass"}
        counts = {"files": len(artifacts), "entries": len(state.transactions), "postings": state.posting_count,
                  "accounts": len(state.accounts), "commodities": len(state.commodities),
                  "groups": [group for group, _ in _groups_of(files, label)]}
        manifest_body = {
            "schema": SCHEMA, "entity": {"id": state.entity_id, "name": state.entity_name, "dir": state.entity_dir},
            "range": {"start": state.start, "end": state.end},
            "source_revision": state.source_revision,
            "ledger_dir": f"{state.entity_dir}/{label}/ledger", "state_hash": digest,
            "artifacts": [a.as_json() for a in artifacts], "counts": counts, "verifier": verifier,
        }
        return _record(engine, state, manifest_body, artifacts=artifacts, counts=counts, verifier=verifier,
                       state_digest=digest, ledger_dir=ledger_dir, label=label, actor=actor,
                       request_id=request_id, also_record=also_record)
    finally:
        engine.dispose()


def _groups_of(files: dict[str, str], label: str) -> list[tuple[str, str]]:
    prefix, suffix = f"{label}-", ".journal"
    return [(name[len(prefix): -len(suffix)], name) for name in files if name.startswith(prefix) and name.endswith(suffix)]


def _record(engine, state: BookState, manifest_body: dict[str, Any], *, artifacts: tuple[Artifact, ...],
            counts: dict[str, Any], verifier: dict[str, Any], state_digest: str, ledger_dir: Path, label: str,
            actor: str, request_id: str | None = None,
            also_record: Callable[[WriteCtx, "ExportResult"], None] | None = None) -> ExportResult:
    """One short write transaction, after the artifacts are durable: operation, snapshot, export row, event.

    The result is built inside the transaction so that ``also_record`` can be handed the finished thing.  A caller
    writing its own row about this export (the HTTP route's ``requests`` row) needs the export id and the manifest
    digest, and writing it in a second transaction afterwards would leave a window where the export is durable and
    the reply that describes it is not.
    """
    with write_transaction(engine) as conn:
        ctx = WriteCtx.open(conn, actor, request_id=request_id)
        op = requests.create_operation(ctx, kind="export", intent={"kind": "export",
                                                                   "range": {"start": state.start, "end": state.end},
                                                                   "ledger_dir": manifest_body["ledger_dir"]})
        op = requests.transition_operation(ctx, op.id, expected_version=op.version, state="running")
        snap = _snapshot(ctx, through=state.end, state_digest=state_digest, manifest=manifest_body, counts=counts, verifier=verifier)
        manifest = {**manifest_body, "snapshot": {"id": snap.id, "seq": snap.snapshot_seq, "state_hash": snap.state_hash}}
        manifest_sha256 = sha256_hex(canonical_json(manifest))
        exp = exports.record_export(ctx, snapshot_id=snap.id, operation_id=op.id, manifest_sha256=manifest_sha256,
                                    artifacts=[a.as_json() for a in artifacts], verifier=verifier)
        requests.transition_operation(ctx, op.id, expected_version=op.version, state="succeeded", counts=counts,
                                      result={"export_id": exp.id, "manifest_sha256": manifest_sha256})
        seq = events.append_event(ctx, kind="export.completed", subject_kind="export", subject_id=exp.id,
                                  after=manifest, reason="journal range exported and verified by the pinned hledger binary")
        result = ExportResult(entity_id=state.entity_id, entity_dir=state.entity_dir, start=state.start, end=state.end,
                              ledger_dir=str(ledger_dir), export_id=exp.id, snapshot_id=snap.id,
                              snapshot_seq=snap.snapshot_seq, operation_id=op.id, state_hash=state_digest,
                              manifest_sha256=manifest_sha256, source_revision=state.source_revision,
                              artifacts=artifacts, counts=counts, verifier=verifier, manifest=manifest, event_seq=seq)
        if also_record is not None:
            also_record(ctx, result)
    return result


def _snapshot(ctx: WriteCtx, *, through: str, state_digest: str, manifest: dict, counts: dict, verifier: dict):
    """The snapshot for this state, reusing the existing one when the state is unchanged.

    ``snapshots.state_hash`` is UNIQUE and one state is one snapshot.  Re-exporting an unchanged range reuses the
    existing row, so the manifest and its hash are the same as the first export's, rather than asking the database
    to refuse a duplicate."""
    existing = closing.snapshot_by_state_hash(ctx.conn, ctx.entity_id, state_digest)
    if existing is None:
        return closing.create_snapshot(ctx, through=through, state_hash=state_digest,
                                       manifest=manifest, report=counts, checks=verifier)
    if existing.through != through:
        raise validation(f"snapshot {existing.id!r} carries state hash {state_digest} through {existing.through}, "
                         f"not through {through}", path="state_hash", reason="snapshot_mismatch")
    return existing


def result_dict(result: ExportResult) -> dict[str, Any]:
    return asdict(result)
