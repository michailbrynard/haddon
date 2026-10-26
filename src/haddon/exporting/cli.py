"""``bin/haddon-export <db-path> <out-root> [--year 2025 | --from D --to D] [--overwrite] [--manifest PATH]``:
export a range of one continuous journal, verify it with the pinned hledger binary and print the counts.  The
logic lives in :mod:`haddon.exporting.exporter`.

The default is the whole journal.  ``--year`` is shorthand for one calendar year and ``--from``/``--to`` is any
inclusive range with either side open; they are alternatives, never combined.  The directory an export lands in is
named after the range it covers, so a whole journal that happens to be one calendar year still lands in
``<Entity>/2025/ledger/``.  Exits 3 when ``hledger check --strict`` refuses the export and 5 when the published
files do not hash to what was verified."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from haddon.domain.canonical import canonical_json
from haddon.exporting.exporter import ArtifactMismatch, ExportBlocked, export_book


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haddon-export", description="Export an entity book as hledger journal files.")
    parser.add_argument("db_path")
    parser.add_argument("out_root", help="export root; artifacts go to <out-root>/<Entity>/<label>/ledger/")
    parser.add_argument("--year", type=int, help="calendar-year shorthand for --from/--to")
    parser.add_argument("--from", dest="start", help="inclusive range start; omit for the first posting")
    parser.add_argument("--to", dest="end", help="inclusive range end; omit for the last posting")
    parser.add_argument("--overwrite", action="store_true", help="replace an export already in the target directory")
    parser.add_argument("--manifest", help="also write the manifest JSON here (outside the managed directory)")
    args = parser.parse_args(argv)
    if args.year is not None and (args.start is not None or args.end is not None):
        parser.error("--year is shorthand for a calendar year; use it OR --from/--to, not both")
    try:
        result = export_book(args.db_path, args.out_root, year=args.year, start=args.start, end=args.end,
                             overwrite=args.overwrite)
    except ExportBlocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    except ArtifactMismatch as exc:
        print(f"MISMATCH: {exc}", file=sys.stderr)
        return 5
    if args.manifest:
        Path(args.manifest).write_text(canonical_json(result.manifest) + "\n", encoding="utf-8")
    print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
