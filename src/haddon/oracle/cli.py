"""``bin/haddon-oracle [book|continuity|all] [Entity ...]``: run the oracle and exit nonzero on any disagreement.

``book`` compares one calendar year three ways (source -> store -> reparsed export).  ``continuity`` loads the
corpus's chain of years into one journal, the way ``README.md`` teaches, and compares the continuous position with
the annual books it was built from.  That is the shape in which the Phase 2 double count could appear, and the book
oracle cannot see it (``haddon.oracle.continuity``).  ``all`` runs both for every entity, which is what the commit
gate runs.

The exit status is the point of the command (spec: "the oracle is a failing CI/test command, not a report that
prints mismatches and exits successfully").  It prints every gate, pass and fail, and then exits:

    0  every required gate of every requested run passed
    2  a gate failed, or a required gate never ran
    1  the command itself could not run (unknown entity, a protected target, a broken invocation)

``--into DIR`` publishes each verified database.  It is written only for a run that passed everything, so a
failing oracle leaves the target entity unimported.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from haddon.domain.canonical import canonical_json
from haddon.domain.errors import HaddonError
from haddon.importing.corpus import load_corpus
from haddon.oracle.continuity import run_continuity_oracle
from haddon.oracle.runner import run_oracle

MODES = ("book", "continuity", "all")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="haddon-oracle", description="Compare pinned source -> store -> reparsed "
                                     "export and fail if anything disagrees.")
    parser.add_argument("mode", nargs="?", default="all", help="book | continuity | all (default: all)")
    parser.add_argument("entities", nargs="*", default=[],
                        help="default: every entity the corpus manifest declares")
    parser.add_argument("--corpus", help="corpus manifest to verify against (default: $HADDON_CORPUS)")
    parser.add_argument("--checkout", help="override the corpus's own checkout (read-only)")
    parser.add_argument("--year", type=int, help="override the corpus's default book year")
    parser.add_argument("--into", help="publish each verified database here (only ever written by a passing run)")
    parser.add_argument("--keep", help="keep each passing run's workspace (database + exported journals) here")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON instead of text")
    args = parser.parse_args(argv)
    mode, entities = args.mode, list(args.entities)
    if mode not in MODES:  # `haddon-oracle ExampleBV` names an entity, not a mode
        entities.insert(0, mode)
        mode = "all"

    reports = []
    try:
        corpus = load_corpus(args.corpus)
        # Every entity the corpus declares, in the order it declares them: which books exist is the manifest's
        # to say, so an entity added to it is verified without editing this command (issue 0006).
        entities = entities or list(corpus.entity_keys)
        for entity in entities:
            if mode in ("book", "all"):
                into = Path(args.into).joinpath(f"{entity}.db") if args.into else None
                keep = Path(args.keep).joinpath(entity) if args.keep else None
                reports.append(run_oracle(entity, corpus=corpus, checkout=args.checkout, year=args.year,
                                          into=into, keep=keep))
            if mode in ("continuity", "all"):
                reports.append(run_continuity_oracle(entity, corpus=corpus, checkout=args.checkout))
    except HaddonError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(canonical_json({"ok": all(r.ok for r in reports), "reports": [r.as_json() for r in reports]}))
    else:
        for report in reports:
            print(report.text())
            print()
    ok = bool(reports) and all(r.ok for r in reports)
    if not ok:
        failed = [f"{r.kind}/{r.entity}" for r in reports if not r.ok]
        print(f"ORACLE FAILED: {', '.join(failed) or 'no run produced a report'}", file=sys.stderr)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
