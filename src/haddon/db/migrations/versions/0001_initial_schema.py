"""The Haddon schema: 23 STRICT tables, 9 indexes, 5 views and the 65-trigger suite.

This is the only migration.  It replaces a chain of ten (0001..0010) that had accumulated over the build --
add a column, drop the dead half of the spec, rebuild a table to relax a constraint -- and that nobody could
read as a description of the schema, because it wasn't one: it was a diary.  The shape it ends at is
unchanged; `sql/0001/*.sql` is literally the `sqlite_master` text of a database built by running that chain
to head, so the squash could not drift from it.  The plan asked for this once the model settled
(docs/design/simplification-plan.md, "Data and migration policy"; issue 0008 deferred it out of phase 6).

An existing database cannot be walked here.  Those revisions are gone, so a database stamped with one fails
loudly on the next `upgrade` ("Can't locate revision identified by '0006'") rather than being silently
mis-stamped.  There are two honest paths, and which one applies is decided by the database, not by preference:

* **It is at 0010** -- the schema is already right, so only `alembic_version` needs rewriting:

      cp entity.db entity.db.bak
      .venv/bin/python -c "from alembic import command; from haddon.db.migrate import alembic_config; \
          command.stamp(alembic_config('entity.db'), '0001')"

  `stamp` runs no DDL, which is the point.  Verify with `haddon.db.migrate.current_revision` and by diffing
  the database's `.schema` against a freshly migrated one; for a database that really was at 0010 that diff
  is empty.

* **It is at anything else** -- rebuild it from its source documents.  Not as a fallback: migration 0009
  refuses to carry reconciliation rows forward *by design*, so a database below 0009 holding them could never
  reach 0010 in the first place, squash or no squash.  That is the project's stated policy for development
  databases (same section of the plan).

Revision ID: 0001
Revises:
Create Date: 2026-09-15
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from alembic import op

from haddon.db.sqltext import iter_statements

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql" / "0001"
# The SQL files are frozen; a change to either requires a new migration.  Update these only if you are
# deliberately editing 0001 before it has ever been applied anywhere.
PINNED_SHA256 = {
    "schema.sql": "272f920ea74e2bcdcbe0e8d25e88742906cf8587c2ced0f61b350fbb895f698d",
    "triggers.sql": "a2fd0d384ae5f3dc8dee7186e7085f2817b9199be9b0165a0b557c72973c6585",
}


def _load(name: str) -> str:
    data = (SQL_DIR / name).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != PINNED_SHA256[name]:
        raise RuntimeError(f"{name} has changed (sha256 {digest}); write a new migration instead of editing 0001")
    return data.decode()


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("schema.sql", "triggers.sql"):
        for stmt in iter_statements(_load(name)):
            bind.exec_driver_sql(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.exec_driver_sql(
        "SELECT type, name FROM sqlite_master WHERE type IN ('trigger','view','table') "
        "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version' "
        "ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'view' THEN 1 ELSE 2 END"
    ).fetchall()
    for kind, name in rows:
        bind.exec_driver_sql(f'DROP {kind.upper()} IF EXISTS "{name}"')
