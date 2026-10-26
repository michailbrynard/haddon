"""Alembic environment: always migrate through Haddon's guarded engine (pragmas + connection guard)."""

from __future__ import annotations

from alembic import context

from haddon.db.connection import make_engine

config = context.config


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    path = ":memory:" if url == "sqlite://" else url.removeprefix("sqlite:///")
    engine = make_engine(path)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline mode is not supported: migrations run SQL scripts through a live connection")
run_migrations_online()
