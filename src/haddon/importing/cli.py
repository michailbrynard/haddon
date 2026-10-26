"""``bin/haddon-import <Entity> <db-path> [--year Y] [--into-existing] [--skip-blocked]``.

Migrates the database file if needed, runs the import and prints the counts.  The logic lives in
:mod:`haddon.importing.importer`.  Exits 3 when the strict gate refuses the journal, and 4 when ``--skip-blocked``
recorded the block instead."""

from __future__ import annotations

import argparse
import sys

from haddon.db.migrate import upgrade
from haddon.importing.corpus import load_corpus
from haddon.importing.importer import BlockedResult, ImportBlocked, import_entity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haddon-import",
                                     description="Import one corpus entity's hledger journal into a ledger database.")
    parser.add_argument("entity", help="an entity key the corpus manifest declares")
    parser.add_argument("db_path")
    parser.add_argument("--corpus", help="corpus manifest to read (default: $HADDON_CORPUS)")
    parser.add_argument("--checkout", help="override the corpus's own checkout (read-only)")
    parser.add_argument("--year", type=int, help="the calendar year whose range to import (default: the corpus's)")
    parser.add_argument("--into-existing", action="store_true",
                        help="import a LATER year of the same entity beside a book that is already there "
                             "(a different entity, or a year that already has a book, is still refused)")
    parser.add_argument("--skip-blocked", action="store_true",
                        help="record a journal that fails `hledger check --strict` as blocked instead of exiting nonzero")
    args = parser.parse_args(argv)
    corpus = load_corpus(args.corpus)
    upgrade(args.db_path)
    try:
        result = import_entity(args.entity, args.db_path, corpus=corpus, checkout=args.checkout, year=args.year,
                               into_existing=args.into_existing,
                               on_blocked="skip" if args.skip_blocked else "raise")
    except ImportBlocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    print(result.summary())
    return 4 if isinstance(result, BlockedResult) else 0


if __name__ == "__main__":
    raise SystemExit(main())
