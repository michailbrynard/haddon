"""CI greps over ``src/``: text that must never appear in the service code, whatever a review might miss.

* SQLite's REPLACE conflict resolution (review D1).  REPLACE deletes the conflicting row without firing
  DELETE triggers unless ``recursive_triggers`` is ON, which would silently bypass the immutability suite.  The
  connection guard enforces the pragma; this makes sure the forbidden syntax never enters the code at all.
* The test fixture's company (chunk G1 part (c)).  Haddon is a general bookkeeping ledger, so no company is
  special to it, including the synthetic one the suite is built around.  A fixture name reaching ``src/`` is
  the first symptom of code that only works for one set of books.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
FORBIDDEN = re.compile(r"\bOR\s+REPLACE\b|\bREPLACE\s+INTO\b", re.IGNORECASE)


def test_no_replace_conflict_resolution_in_src():
    hits = []
    for path in SRC.rglob("*"):
        if path.suffix not in {".py", ".sql", ".mako"} or not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(SRC.parent)}:{lineno}: {line.strip()}")
    assert not hits, "forbidden REPLACE syntax in src/:\n" + "\n".join(hits)


def test_grep_pattern_catches_the_shapes_we_care_about():
    for bad in ("INSERT OR REPLACE INTO t", "insert or  replace into t", "REPLACE INTO t VALUES(1)", "UPDATE OR REPLACE t SET x=1"):
        assert FORBIDDEN.search(bad), bad
    for ok in ("SELECT replace(a,'x','y')", "-- conflict-resolution REPLACE is forbidden", "ON CONFLICT DO UPDATE"):
        assert not FORBIDDEN.search(ok), ok


def test_the_fixture_company_never_appears_in_src():
    """The synthetic company exists only in ``tests/``.  ``src/`` must not know its name or its id.

    This deliberately does not forbid ``assets:cash``, which ``importing.importer`` uses as the
    general ``is_cash`` convention rather than as one company's account.  The line is identity, not vocabulary.
    """
    from repo_helpers import COMPANY, ENTITY_ID

    needles = (COMPANY, ENTITY_ID, COMPANY.split()[0].lower())
    hits = []
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in {".py", ".sql", ".json", ".mako"} or not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for needle in needles:
                if needle in line:
                    hits.append(f"{path.relative_to(SRC.parent)}:{lineno}: {needle!r} in {line.strip()}")
    assert not hits, "the test fixture's company reached src/:\n" + "\n".join(hits)
