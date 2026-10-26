"""Programmatic and CLI entry point for Alembic migrations on one entity database file."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from haddon.db.connection import make_engine, sqlite_url

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(db_path: str | Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sqlite_url(db_path))
    return cfg


def upgrade(db_path: str | Path, revision: str = "head") -> None:
    """Apply migrations to ``db_path`` (created if missing) through the guarded engine."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True) if str(db_path) != ":memory:" else None
    command.upgrade(alembic_config(db_path), revision)


def current_revision(db_path: str | Path) -> str | None:
    engine = make_engine(db_path)
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config(":memory:")).get_current_head()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: haddon-migrate <entity.db>", file=sys.stderr)
        return 2
    upgrade(argv[0])
    print(f"{argv[0]}: at revision {current_revision(argv[0])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
