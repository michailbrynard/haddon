"""Backing up an entity, and getting it back.

One entity is one SQLite file, which makes a backup sound like ``cp``.  It is not, and the difference is the
whole reason this module exists rather than a paragraph in a runbook.

Why not ``cp``: every connection runs in WAL (``db.connection.apply_pragmas``), so at any moment some committed
transactions live in ``<db>-wal`` and not yet in ``<db>``.  Copying the ``.db`` on its own silently drops them.
The copy opens cleanly, passes ``integrity_check``, and is simply missing the most recent work.  That is the worst
shape a backup can have, because nothing about it looks wrong.  ``create`` uses ``VACUUM INTO``, which
runs inside a read transaction and writes a single self-contained file holding everything committed as of that
moment, with no ``-wal`` or ``-shm`` beside it.  Writers are not blocked while it runs and no checkpoint is
needed first.

A restore is a copy, not a rename.  ``restore`` writes the backup to a new path and leaves the backup
untouched, so a restore that turns out to be the wrong one can be done again.  The restored file arrives in the
default journal mode, and the first guarded connection puts it back into WAL (``apply_pragmas``).  That is a
write, so a restore target is never opened read-only, and ``verify`` deliberately does not use the service's own
connection helpers.

A backup nobody has restored is a guess, which is why the drill exists.  ``drill`` does the whole round trip on a
copy and refuses at the first disagreement: create, verify read-only, restore, then compare the restored database
against the live one both by content digest and by the hash of the journal each of them exports.
It is what a deployment should run before it depends on the answer, and what a release should run to know the
procedure still works.

What ``verify`` checks, and what each failure would mean:

* ``PRAGMA integrity_check``: the file is a structurally sound database.
* ``PRAGMA foreign_key_check``: nothing references a row that is not there.  A backup taken mid-transaction
  by a naive copier is the way this goes wrong.
* The Alembic revision.  A backup restored into a service expecting a later schema is a migration problem, not
  a recovery; the revision is recorded so that is visible before anything opens it.
* ``content_digest``: every row of every table, ordered, hashed.  ``integrity_check`` says the file is not
  corrupt; only this says it holds the same books.
* ``state_hash``: the restored database exports a byte-identical journal.  The digest above
  would already catch a changed amount; this catches the case where the data survived but the books no longer
  export, which is what a person actually needs from a recovered ledger.

Usage::

    haddon-backup create  <entity.db> <backup-dir> [--keep N]
    haddon-backup verify  <backup.db>
    haddon-backup restore <backup.db> <destination.db>
    haddon-backup drill   <entity.db> [--keep N]

``drill`` exits non-zero on any disagreement, so it is usable as a cron check and as a release gate.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from haddon.domain.canonical import canonical_json, sha256_hex

#: ``YYYYmmddTHHMMSSZ`` sorts lexically in time order, which is what ``--keep`` prunes by.
STAMP = "%Y%m%dT%H%M%SZ"
SUFFIX = ".backup.db"

#: tables SQLite maintains for itself.  ``sqlite_sequence`` changes as rows are inserted and would make two
#: identical databases digest differently depending on how they got there.
INTERNAL_PREFIX = "sqlite_"


class BackupError(RuntimeError):
    """A backup, verification or restore that did not do what it says it did."""


def read_only(path: str | Path) -> sqlite3.Connection:
    """Open a database without being able to write to it.

    Deliberately not ``db.connection.sqlite_connect``: that applies ``PRAGMA journal_mode=WAL``, which is a
    write, so the service's own helper cannot open anything read-only.  A verification that modified the file it
    was verifying would be checking something other than the backup on disk.
    """
    file = Path(path)
    if not file.is_file():
        raise BackupError(f"{file} does not exist")
    return sqlite3.connect(f"file:{file}?mode=ro", uri=True)


def table_names(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [name for (name,) in rows if not name.startswith(INTERNAL_PREFIX)]


def content_digest(con: sqlite3.Connection) -> str:
    """Hash every row of every table, in a deterministic order.

    Rows are ordered by every column rather than by ``rowid``.  Two databases holding the same books may have
    been vacuumed differently and carry different rowids, and a digest that called those different would fail on
    every restore, since ``VACUUM INTO`` is exactly such a rewrite.
    """
    per_table = {}
    for table in table_names(con):
        cols = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
        if not cols:
            per_table[table] = sha256_hex("")
            continue
        order = ",".join(f'"{c}"' for c in cols)
        rows = con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        per_table[table] = sha256_hex(canonical_json([[_scalar(v) for v in row] for row in rows]))
    return sha256_hex(canonical_json(per_table))


def _scalar(value):
    """Make a cell something canonical JSON can hold.

    Blobs are hashed rather than decoded.  No column in this schema is a blob today, and a digest must not fail
    on one appearing.
    """
    return {"blob": sha256_hex(value.hex())} if isinstance(value, bytes) else value


def alembic_revision(con: sqlite3.Connection) -> str | None:
    if "alembic_version" not in table_names(con):
        return None
    row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    return None if row is None else str(row[0])


def journal_hash(path: str | Path) -> str:
    """Hash the journal this database exports, the whole of it, as one continuous journal.

    This is the digest the export writes into the journal header and a close records on its snapshot, so two
    databases agreeing here render the same journal files byte for byte.  The content digest above would already
    catch a changed amount; this catches the case where the rows survived but the books no longer export, which
    is what a person actually needs back from a recovered ledger.

    The database is opened through the guarded engine rather than read-only, because ``read_book_state`` runs on
    a SQLAlchemy connection with the service's pragmas.  That is why the drill hashes a restored copy and not the
    backup file itself.
    """
    from haddon.db.connection import make_engine
    from haddon.exporting.exporter import read_book_state
    from haddon.exporting.render import state_hash

    engine = make_engine(path, immediate=False)
    try:
        with engine.connect() as conn:
            return state_hash(read_book_state(conn))
    finally:
        engine.dispose()


@dataclass(frozen=True)
class Report:
    """What a verification found.

    ``ok`` is the only thing a caller has to read; the rest is what to say when it is false, and what to record
    when it is true.
    """

    path: str
    ok: bool
    integrity: str
    foreign_keys: int
    revision: str | None
    tables: int
    digest: str
    size_bytes: int
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = f"{self.path}: {'OK' if self.ok else 'FAILED'}"
        body = (f"integrity={self.integrity} foreign_key_violations={self.foreign_keys} "
                f"revision={self.revision} tables={self.tables} bytes={self.size_bytes} digest={self.digest[:16]}")
        return head + "  " + body + ("\n  " + "\n  ".join(self.problems) if self.problems else "")


def create(db_path: str | Path, backup_dir: str | Path, *, keep: int | None = None, now: datetime | None = None) -> Path:
    """Write a consistent copy of a live database to one file, without stopping writers.

    ``VACUUM INTO`` refuses if the target exists, which is the behaviour to want: a backup never overwrites an
    earlier one, so two backups in the same second are an error rather than a silent loss.
    """
    source = Path(db_path)
    if not source.is_file():
        raise BackupError(f"{source} does not exist")
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime(STAMP)
    target = directory.joinpath(f"{source.stem}-{stamp}{SUFFIX}")
    if target.exists():
        raise BackupError(f"{target} already exists; a backup never overwrites an earlier one")

    con = sqlite3.connect(f"file:{source}?mode=rw", uri=True)
    try:
        # A literal is required: VACUUM INTO does not take a bound parameter.  The path is quoted SQL-style, so a
        # directory with a quote in it cannot end the string early.
        con.execute("VACUUM INTO '" + str(target).replace("'", "''") + "'")
    finally:
        con.close()
    if not target.is_file():
        raise BackupError(f"VACUUM INTO reported success but {target} is not there")
    if keep is not None:
        prune(directory, keep=keep, stem=source.stem)
    return target


def backups(directory: str | Path, *, stem: str | None = None) -> list[Path]:
    """List every backup in a directory, oldest first.  The timestamp sorts lexically, so the name is the order."""
    found = sorted(p for p in Path(directory).glob(f"*{SUFFIX}") if p.is_file())
    return [p for p in found if stem is None or p.name.startswith(f"{stem}-")]


def prune(directory: str | Path, *, keep: int, stem: str | None = None) -> list[Path]:
    """Delete all but the newest ``keep`` backups.

    ``keep=0`` is refused.  Retention that keeps nothing is a mistake in a configuration file, not a policy
    anybody means.
    """
    if keep < 1:
        raise BackupError("retention must keep at least one backup")
    existing = backups(directory, stem=stem)
    doomed = existing[:max(len(existing) - keep, 0)]
    for path in doomed:
        path.unlink()
    return doomed


def verify(path: str | Path) -> Report:
    """Open a backup read-only and say whether it is a database that still holds the books.

    A file damaged badly enough that SQLite will not read it at all is a finding, not an exception: the
    caller asked whether this backup is usable and the answer is no.  It comes back as a failed report with the
    engine's own words in it, so a drill and an operator learn the same thing the same way.
    """
    file = Path(path)
    problems: list[str] = []
    integrity, violations, names, revision, digest = "unreadable", (), [], None, ""
    try:
        con = read_only(file)
        try:
            integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                problems.append(f"integrity_check: {integrity}")
            violations = con.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                problems.append(f"foreign_key_check: {len(violations)} violation(s), first {violations[0]}")
            names = table_names(con)
            if not names:
                problems.append("the database holds no table at all")
            revision = alembic_revision(con)
            if revision is None:
                problems.append("no alembic_version row: this is not a migrated Haddon database")
            digest = content_digest(con)
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        problems.append(f"SQLite will not read this file: {exc}")
    return Report(path=str(file), ok=not problems, integrity=integrity, foreign_keys=len(violations),
                  revision=revision, tables=len(names), digest=digest, size_bytes=file.stat().st_size,
                  problems=problems)


def restore(backup_path: str | Path, destination: str | Path, *, overwrite: bool = False) -> Path:
    """Put a verified backup back, at a new path.

    The backup is verified first and the restore refuses if it does not pass: restoring a database that fails
    ``integrity_check`` produces a service that starts and then lies, which is worse than a failed restore.
    """
    target = Path(destination)
    if target.exists() and not overwrite:
        raise BackupError(f"{target} already exists; pass --overwrite to replace it")
    report = verify(backup_path)
    if not report.ok:
        raise BackupError(f"refusing to restore a backup that does not verify:\n{report.summary()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    for stale in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
        if stale.exists():
            stale.unlink()
    # Copied through SQLite rather than the filesystem, for the same reason `create` does not use `cp`: this way
    # the restored file is written by the database engine and is a database on arrival, whatever the source was.
    src = read_only(backup_path)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


@dataclass(frozen=True)
class Drill:
    source: str
    backup: str
    restored: str
    verification: Report
    digest: str
    digest_matches: bool
    journal: str
    journal_matches: bool
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        lines = [f"backup   {self.backup}",
                 f"verify   {self.verification.summary()}",
                 f"restore  {self.restored}",
                 f"rows     digest {self.digest[:16]} "
                 f"{'matches the live database' if self.digest_matches else 'DIFFERS from the live database'}",
                 f"journal  state_hash {self.journal[:16]} "
                 f"{'renders identically' if self.journal_matches else 'DIFFERS from the live database'}",
                 "DRILL PASSED" if self.ok else "DRILL FAILED"]
        return "\n".join(lines + [f"  {p}" for p in self.problems])


def drill(db_path: str | Path, *, workdir: str | Path | None = None, keep: int | None = None) -> Drill:
    """Run the whole round trip on a copy, ending in a comparison against the live database.

    The live database is never touched: the backup is read out of it, and everything after that happens on files
    under ``workdir``.  A drill that had to write to the thing it was proving recoverable would be a worse risk
    than not drilling.
    """
    source = Path(db_path)
    con = read_only(source)
    try:
        live_digest = content_digest(con)
    finally:
        con.close()
    live_journal = journal_hash(source)

    temporary = None
    if workdir is None:
        temporary = tempfile.TemporaryDirectory(prefix="haddon-drill-")
        workdir = temporary.name
    try:
        root = Path(workdir)
        made = create(source, root.joinpath("backups"), keep=keep)
        report = verify(made)
        problems = list(report.problems)
        if not report.ok:
            return Drill(source=str(source), backup=str(made), restored="", verification=report,
                         digest="", digest_matches=False, journal="", journal_matches=False, problems=problems)

        restored = restore(made, root.joinpath("restored.db"))
        con = read_only(restored)
        try:
            restored_digest = content_digest(con)
        finally:
            con.close()
        restored_journal = journal_hash(restored)

        if restored_digest != live_digest:
            problems.append(f"the restored database does not hold the same rows: live {live_digest[:16]} vs "
                            f"restored {restored_digest[:16]}")
        if restored_journal != live_journal:
            problems.append(f"the restored database does not export the same journal: live {live_journal[:16]} "
                            f"vs restored {restored_journal[:16]}")
        return Drill(source=str(source), backup=str(made), restored=str(restored), verification=report,
                     digest=restored_digest, digest_matches=restored_digest == live_digest,
                     journal=restored_journal, journal_matches=restored_journal == live_journal,
                     problems=problems)
    finally:
        if temporary is not None:
            temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haddon-backup", description="Back up, verify and restore an entity.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="write a consistent copy of a live database")
    p.add_argument("db_path")
    p.add_argument("backup_dir")
    p.add_argument("--keep", type=int, default=None, help="delete all but the newest N backups afterwards")

    p = sub.add_parser("verify", help="open a backup read-only and check it")
    p.add_argument("backup_path")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("restore", help="put a verified backup back, at a new path")
    p.add_argument("backup_path")
    p.add_argument("destination")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("drill", help="create, verify, restore and compare -- the whole round trip")
    p.add_argument("db_path")
    p.add_argument("--workdir", default=None, help="keep the artifacts here instead of a temporary directory")
    p.add_argument("--keep", type=int, default=None)
    p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            made = create(args.db_path, args.backup_dir, keep=args.keep)
            print(f"{made} ({made.stat().st_size} bytes)")
            return 0
        if args.command == "verify":
            report = verify(args.backup_path)
            print(json.dumps(asdict(report), indent=2, sort_keys=True) if args.json else report.summary())
            return 0 if report.ok else 1
        if args.command == "restore":
            target = restore(args.backup_path, args.destination, overwrite=args.overwrite)
            print(f"restored {args.backup_path} -> {target}")
            return 0
        result = drill(args.db_path, workdir=args.workdir, keep=args.keep)
        if args.json:
            body = asdict(result)
            print(json.dumps(body, indent=2, sort_keys=True))
        else:
            print(result.summary())
        return 0 if result.ok else 1
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
