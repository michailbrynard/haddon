"""Backing an entity up, and getting it back (issue 0008 D).

A backup nobody has restored is a guess, so the restore half is tested at least as hard as the backup half, and
the drill (the procedure an operator actually runs) is tested both passing and failing.

``test_a_plain_file_copy_of_a_live_database_loses_committed_work`` shows the procedure is necessary; everything
else shows it is correct.  One entity is one SQLite file, so "just copy the file" is the obvious thing to do,
and it silently drops whatever is still in the write-ahead log.  The result opens cleanly, passes
``integrity_check`` and is missing the most recent work.  If that test ever stops failing for ``cp``, this module
can be deleted.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from haddon import backup
from haddon.db.connection import sqlite_connect
from haddon.db.migrate import head_revision

pytestmark = pytest.mark.slow

STAMPS = ("20250101T000000Z", "20250102T000000Z", "20250103T000000Z")


@pytest.fixture
def live(provisioned_template, fast_dir):
    """A provisioned entity file, as a deployment would have on disk."""
    path = fast_dir / "live.db"
    shutil.copy(provisioned_template, path)
    return path


def rows_in(path, table="accounts") -> int:
    con = backup.read_only(path)
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


# Why this is not ``cp``
def test_a_plain_file_copy_of_a_live_database_loses_committed_work(live, fast_dir):
    """Why this module exists, stated as a failing ``cp``.

    With a connection still open, a committed write is in ``live.db-wal`` and not yet in ``live.db``.  Copying
    the one file loses the write, and the copy is a valid database, so nothing announces the loss.  ``create``
    reads through SQLite and does not lose it.
    """
    con = sqlite_connect(live)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO commodities(id,entity_id,code,precision,quantum,created_at,created_by) "
                    "VALUES('c-wal',(SELECT id FROM entities),'XTS',2,10000,'2026-01-01T00:00:00Z','test')")
        con.execute("COMMIT")

        naive = fast_dir / "naive-copy.db"
        shutil.copy(live, naive)                      # the mistake this module exists to prevent
        made = backup.create(live, fast_dir / "backups")
    finally:
        con.close()

    assert rows_in(naive, "commodities") == 3, "the premise has changed: the write was already checkpointed"
    assert rows_in(made, "commodities") == 4, "the backup lost a committed write"
    # The lossy copy still looks healthy, which is what makes it dangerous.
    assert backup.verify(naive).ok


def test_a_backup_brings_no_sidecar_files_with_it(live, fast_dir):
    """``VACUUM INTO`` writes one self-contained file.  A backup that needed its ``-wal`` beside it would be
    three files an operator has to keep together, and two of them are easy to lose."""
    made = backup.create(live, fast_dir / "backups")
    assert not (fast_dir / "backups" / f"{made.name}-wal").exists()
    assert sorted(p.name for p in (fast_dir / "backups").iterdir()) == [made.name]


# Verification
def test_a_fresh_backup_verifies_and_names_its_schema(live, fast_dir):
    report = backup.verify(backup.create(live, fast_dir / "backups"))
    assert report.ok, report.summary()
    assert report.integrity == "ok" and report.foreign_keys == 0
    assert report.revision == head_revision(), "a backup must say which schema it is, before anything opens it"
    assert report.tables > 1 and report.size_bytes > 0


def test_verification_does_not_write_to_the_backup_it_is_verifying(live, fast_dir):
    """Opened read-only on purpose: the service's own ``sqlite_connect`` sets ``journal_mode=WAL``, which is a
    write, and a verification that modified the file would not be checking the backup on disk."""
    made = backup.create(live, fast_dir / "backups")
    before = (made.stat().st_mtime_ns, made.stat().st_size, made.read_bytes())
    assert backup.verify(made).ok
    assert (made.stat().st_mtime_ns, made.stat().st_size, made.read_bytes()) == before
    assert not made.with_name(f"{made.name}-wal").exists()

    con = backup.read_only(made)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM accounts")
    finally:
        con.close()


def test_a_corrupt_backup_does_not_verify_and_cannot_be_restored(live, fast_dir):
    made = backup.create(live, fast_dir / "backups")
    raw = bytearray(made.read_bytes())
    raw[4096:8192] = b"\x00" * 4096            # a page of zeros in the middle: still a file, no longer a database
    made.write_bytes(bytes(raw))

    report = backup.verify(made)
    assert not report.ok and report.problems
    with pytest.raises(backup.BackupError, match="does not verify"):
        backup.restore(made, fast_dir / "restored.db")
    assert not (fast_dir / "restored.db").exists(), "a refused restore must leave nothing behind"


def test_verifying_something_that_is_not_a_haddon_database_says_so(fast_dir):
    stranger = fast_dir / "stranger.db"
    con = sqlite3.connect(stranger)
    con.execute("CREATE TABLE t(a)")
    con.close()
    report = backup.verify(stranger)
    assert not report.ok
    assert any("alembic_version" in p for p in report.problems)


# Restore
def test_a_restore_produces_a_working_entity_and_leaves_the_backup_alone(live, fast_dir):
    made = backup.create(live, fast_dir / "backups")
    before = made.read_bytes()

    restored = backup.restore(made, fast_dir / "restored.db")
    assert made.read_bytes() == before, "a restore must not consume the backup it restored from"

    # The restored file is a database the service can open on its own terms, pragmas, guard and all.
    con = sqlite_connect(restored)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM accounts").fetchone()[0] == rows_in(made)
    finally:
        con.close()


def test_a_restore_does_not_silently_replace_an_existing_file(live, fast_dir):
    made = backup.create(live, fast_dir / "backups")
    target = fast_dir / "occupied.db"
    target.write_bytes(b"something else entirely")

    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore(made, target)
    assert target.read_bytes() == b"something else entirely"

    backup.restore(made, target, overwrite=True)
    assert backup.verify(target).ok


def test_a_restore_clears_the_sidecars_of_whatever_was_there_before(live, fast_dir):
    """A restore over a path that held a live database must not leave that database's ``-wal`` next to the new
    file: SQLite would read it, and the restored books would quietly acquire somebody else's last transaction."""
    made = backup.create(live, fast_dir / "backups")
    target = fast_dir / "target.db"
    shutil.copy(live, target)
    stale = target.with_name(f"{target.name}-wal")
    stale.write_bytes(b"stale write-ahead log")

    backup.restore(made, target, overwrite=True)
    assert not stale.exists()
    assert backup.verify(target).ok


# Retention
def test_retention_keeps_the_newest_and_deletes_the_rest(live, fast_dir):
    from datetime import datetime, timezone

    directory = fast_dir / "backups"
    made = [backup.create(live, directory, now=datetime(2025, 1, day, tzinfo=timezone.utc)) for day in (1, 2, 3)]
    assert backup.backups(directory) == made, "backups must sort oldest-first by name alone"

    deleted = backup.prune(directory, keep=2)
    assert deleted == made[:1]
    assert backup.backups(directory) == made[1:]


def test_retention_that_would_keep_nothing_is_refused(live, fast_dir):
    directory = fast_dir / "backups"
    backup.create(live, directory)
    with pytest.raises(backup.BackupError, match="at least one"):
        backup.prune(directory, keep=0)
    assert len(backup.backups(directory)) == 1


def test_a_backup_never_overwrites_an_earlier_one(live, fast_dir):
    from datetime import datetime, timezone

    when = datetime(2025, 1, 1, tzinfo=timezone.utc)
    backup.create(live, fast_dir / "backups", now=when)
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.create(live, fast_dir / "backups", now=when)


# The drill
def test_the_drill_passes_on_a_live_entity_and_touches_nothing(live, fast_dir):
    before = (live.stat().st_size, live.read_bytes())
    result = backup.drill(live, workdir=fast_dir / "drill")

    assert result.ok, result.summary()
    assert result.verification.ok and result.digest_matches and result.journal_matches
    assert result.digest and result.journal, "a drill that compared nothing must not report a pass"
    assert (live.stat().st_size, live.read_bytes()) == before, "the drill wrote to the database it was proving"


def tamper(path, sql: str) -> None:
    """Change a restored copy the way an accident would: around the ledger's rules rather than through them.

    Every interesting table is immutable by trigger, so a write through the engine cannot simulate a damaged
    restore: it is refused, which is the schema working.  A backup truncated in transit, a half-written file, a
    disk that dropped a page: none of those respect triggers, and the drill has to catch them.  So the triggers
    come off first and the change goes in underneath.
    """
    con = sqlite3.connect(path)
    try:
        con.executescript("PRAGMA writable_schema=ON;"
                          "DELETE FROM sqlite_master WHERE type='trigger';"
                          "PRAGMA writable_schema=OFF;")
    finally:
        con.close()
    # Reopened, because a connection keeps the schema it prepared against: the triggers are gone on disk but
    # still armed on this handle until somebody reads the schema again.
    con = sqlite3.connect(path)
    try:
        con.execute(sql)
        con.commit()
    finally:
        con.close()


def restoring_then(mutate):
    """``backup.restore``, followed by damage.  Returned as a stand-in for the real one."""
    real = backup.restore

    def restore_and_damage(backup_path, destination, **kwargs):
        target = real(backup_path, destination, **kwargs)
        mutate(target)
        return target

    return restore_and_damage


def test_the_drill_fails_when_the_restored_database_is_not_the_same_books(live, fast_dir, monkeypatch):
    """The comparison is load-bearing, so it is tested by breaking it.

    Otherwise a restore that quietly lost a row would be reported as a pass, which is the failure a drill
    exists to rule out.
    """
    monkeypatch.setattr(backup, "restore", restoring_then(
        lambda target: tamper(target, "DELETE FROM account_commodities WHERE rowid=(SELECT min(rowid) "
                                      "FROM account_commodities)")))
    result = backup.drill(live, workdir=fast_dir / "drill")

    assert not result.ok and not result.digest_matches
    assert any("does not hold the same rows" in p for p in result.problems), result.summary()
    assert "DRILL FAILED" in result.summary()


def test_the_drill_notices_when_the_books_no_longer_export_the_same_journal(fx, fast_dir, monkeypatch):
    """The second comparison, broken on its own.

    The narration of a posted entry is reworded in the restored copy: one text column, no amount, no account,
    no balance moved, every row still present.  The row digest catches it too, since a narration is in a row,
    but this damage changes what the books say without changing what they add up to, and the journal hash makes
    that a failure in its own right.
    """
    from repo_helpers import post

    post(fx, "2026-03-15", "120000", narration="what the entry said when it was posted")
    fx.engine.dispose()

    monkeypatch.setattr(backup, "restore", restoring_then(
        lambda target: tamper(target, "UPDATE entries SET narration='something else' "
                                      "WHERE narration='what the entry said when it was posted'")))
    result = backup.drill(fx.path, workdir=fast_dir / "drill")

    assert not result.ok and not result.journal_matches
    assert any("does not export the same journal" in p for p in result.problems), result.summary()


def test_the_drill_refuses_early_when_the_backup_itself_does_not_verify(live, fast_dir, monkeypatch):
    """A backup that does not verify is never restored from, and the drill says so instead of comparing."""
    original = backup.create

    def broken(db_path, backup_dir, **kwargs):
        made = original(db_path, backup_dir, **kwargs)
        made.write_bytes(b"not a database at all" + made.read_bytes()[21:])
        return made

    monkeypatch.setattr(backup, "create", broken)
    result = backup.drill(live, workdir=fast_dir / "drill")

    assert not result.ok and not result.restored, "nothing may be restored from a backup that did not verify"
    assert not result.digest_matches and not result.journal_matches
    assert any("will not read this file" in p or "integrity_check" in p for p in result.problems), result.summary()
