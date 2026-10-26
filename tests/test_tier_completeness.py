"""Chunk E3: the two tiers must, between them, still run every test in the suite.

``make test-fast`` runs one selection and ``make test-slow`` the other.  Splitting the suite is only safe if
nothing can fall out of *both*: a test that lands in neither tier stops being run by either command, and
nothing says so.  This file reads the two selections out of the Makefile, the single place they are defined,
and proves the property by actually collecting.  So it holds for any way of narrowing a run (a marker
expression, a ``-k``, an ``--ignore``, a path), not only for the marker pair that happens to be there today.

It also pins what the fast tier is not allowed to erode: ``test-gate`` runs pytest with no selection at all,
and then runs the oracle.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAKEFILE = REPO / "Makefile"
COLLECTOR = Path(__file__).parent / "tier_collect.py"

#: every flag that can make a pytest run collect less than the whole suite.  The commit gate may use none.
NARROWING = frozenset({"-m", "-k", "--ignore", "--ignore-glob", "--deselect", "--collect-only", "--co",
                       "--last-failed", "--lf", "--failed-first", "--ff", "--stepwise", "--sw",
                       "--exitfirst", "-x", "--maxfail"})


def _variables() -> dict[str, str]:
    return dict(re.findall(r"^(\w+)\s*[:?]?=[ \t]*(.*?)[ \t]*$", MAKEFILE.read_text(), re.M))


def _expand(line: str, variables: dict[str, str]) -> str:
    for _ in range(5):  # variables may refer to variables; five rounds is far more than this file needs
        new = re.sub(r"\$\((\w+)\)", lambda m: variables.get(m.group(1), m.group(0)), line)
        if new == line:
            return new
        line = new
    raise AssertionError(f"make variables in {line!r} do not settle")


def makefile_select(name: str) -> list[str]:
    """One of the tier selections, as an argument list."""
    variables = _variables()
    assert name in variables, f"{name} is not defined in {MAKEFILE}"
    return shlex.split(_expand(variables[name], variables))


def makefile_recipe(target: str) -> list[list[str]]:
    """The command lines of one target, variables expanded, each split into an argument list."""
    text = MAKEFILE.read_text()
    m = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", text, re.M)
    assert m, f"{target} has no recipe in {MAKEFILE}"
    variables = _variables()
    return [shlex.split(_expand(line[1:], variables)) for line in m.group(1).splitlines()]


def collect(*argvs: list[str]) -> list[list[str]]:
    """Node ids collected under each argument list, from one subprocess (see ``tier_collect.py``)."""
    with tempfile.TemporaryDirectory() as tmp:          # never write into the repo: an export test would see it
        out = Path(tmp) / "collected.json"
        proc = subprocess.run([sys.executable, str(COLLECTOR), json.dumps(list(argvs)), str(out)],
                              cwd=REPO, capture_output=True, text=True)
        assert proc.returncode == 0, f"collection subprocess failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}"
        return json.loads(out.read_text())


def test_the_two_tiers_between_them_run_every_test_in_the_suite():
    fast_select, slow_select = makefile_select("FAST_SELECT"), makefile_select("SLOW_SELECT")
    whole, fast, slow = collect([], fast_select, slow_select)

    # a broken collection must not make the property hold vacuously
    assert len(whole) > 1, "the whole suite collected almost nothing; the check below would prove nothing"
    assert fast and slow, f"a tier is empty: fast={len(fast)} slow={len(slow)}"

    orphans = sorted(set(whole) - set(fast) - set(slow))
    assert not orphans, (
        f"{len(orphans)} test(s) are in neither tier, so neither `make test-fast` nor `make test-slow` runs "
        f"them:\n  " + "\n  ".join(orphans[:20]))

    both = sorted(set(fast) & set(slow))
    assert not both, f"{len(both)} test(s) are in both tiers:\n  " + "\n  ".join(both[:20])

    strays = sorted((set(fast) | set(slow)) - set(whole))
    assert not strays, f"{len(strays)} test(s) are selected by a tier but not collected at all:\n  " + "\n  ".join(strays[:20])

    # with no overlap and no orphan, the counts must add up exactly; this also catches a duplicated node id
    assert len(whole) == len(fast) + len(slow), (len(whole), len(fast), len(slow))


def test_the_commit_gate_runs_the_whole_suite_and_then_the_oracle():
    recipe = makefile_recipe("test-gate")
    pytest_lines = [line for line in recipe if line and Path(line[0]).name.startswith("pytest")]
    assert len(pytest_lines) == 1, f"expected exactly one pytest invocation in test-gate, got {pytest_lines}"

    argv = pytest_lines[0][1:]
    narrowing = [a for a in argv if a.split("=")[0] in NARROWING]
    assert not narrowing, f"the commit gate must run the COMPLETE suite, but it passes {narrowing}"
    paths = [a for a in argv if not a.startswith("-") and (REPO / a).exists()]
    assert not paths, f"the commit gate must run the COMPLETE suite, but it is narrowed to {paths}"

    assert any(line[:1] == ["bin/haddon-oracle"] and "all" in line for line in recipe), \
        f"the commit gate must run a full oracle run (`bin/haddon-oracle all`); its recipe is {recipe}"
