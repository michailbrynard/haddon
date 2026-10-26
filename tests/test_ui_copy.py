"""The one rule the reviewer interface's own words have to keep: the chrome never names a kind of caller.

This system does not distinguish a human caller from an automated one (``docs/design/decisions.md`` § "Authorisation").
Nothing in the model branches on who is acting; ``tests/test_authorisation_model.py`` holds that line in the
code.  This file holds it in the copy, because the easiest place to reintroduce a distinction the code does not
make is a heading.

"3 lines need a person" is wrong twice over: it asserts a difference the service does not implement, and it is
false besides, because any of those lines can be resolved by whoever gets to it, usually an agent working
through a statement.  "The agent booked this" is wrong the same way from the other end.

What is always fine, and is not what this file is about, is showing a recorded actor.  ``created_by`` is data,
and a page that shows ``ui:reviewer`` or ``agent:alpha`` beside something that happened is doing exactly what
the trail exists for.  So the check is over literal copy in the sources, the strings somebody wrote, and a value
that arrives from the API at runtime is untouched by it.

It reads the frontend sources rather than rendered pages on purpose: the interface is a client-side bundle, so
rendering every state of every page would mean a browser in the test suite to assert something that is really a
property of the text somebody typed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT.joinpath("web", "src")
PACKAGE = ROOT.joinpath("src", "haddon", "ui")

#: shadcn's own components are vendored by its CLI and are not ours to word.
VENDORED = ("components", "ui")

#: phrases that assert a kind of caller.  Matched case-insensitively against the whole of each source file,
#: which catches them in prose, in a string and in a comment alike.  All three are places the distinction has
#: been talked back into a codebase before.
FORBIDDEN = (
    "needs a person",
    "need a person",
    "needs a human",
    "need a human",
    "by a human",
    "the agent booked",
    "booked by the agent",
    "by the agent",
    "the agent has",
    "a human must",
    "requires a human",
    "human review",
    "manual review",
    "automated caller",
)

#: `haddon.ui`'s own module docstrings quote these phrases in order to forbid them, and the frontend's do the
#: same.  A line that is explaining the rule is not breaking it, so the quoted forms are skipped.
QUOTED = re.compile(r'"[^"\n]*"|``[^`\n]*``|“[^”\n]*”')


def sources() -> list[Path]:
    files = [p for p in WEB.rglob("*.ts*") if VENDORED not in p.relative_to(WEB).parts[:2]]
    files += sorted(PACKAGE.rglob("*.py"))
    return sorted(files)


def test_the_interface_sources_are_where_this_test_thinks_they_are():
    """A guard that silently stops reading anything is a guard that always passes."""
    assert WEB.is_dir(), f"{WEB} is missing; the copy guard would pass over nothing"
    found = sources()
    assert len(found) >= 12, f"only {len(found)} interface sources found; the guard is not reading the app"
    assert any(p.name == "Month.tsx" for p in found), "the month review is the page this rule is most about"


@pytest.mark.parametrize("forbidden", FORBIDDEN)
def test_no_interface_copy_names_an_actor_type(forbidden: str):
    offenders = []
    for path in sources():
        text = QUOTED.sub("", path.read_text(encoding="utf-8")).lower()
        if forbidden in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"{offenders} says {forbidden!r}. This system does not distinguish a human caller from an automated "
        f"one; say what happened and, where it matters, name the recorded actor."
    )


def test_the_guard_would_actually_catch_one():
    """The check is only worth having if it fails on the thing it is for."""
    sample = 'const heading = "3 lines need a person"'
    assert "need a person" in QUOTED.sub("", sample + " need a person").lower()


def test_a_recorded_actor_is_still_shown_somewhere():
    """The rule is about the chrome, not about the data.  If no page showed a recorded actor at all, this
    file would have been read as "never mention who did anything", which is the opposite of the intent."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in sources())
    assert "created_by" in text, "no page shows the recorded actor; the audit trail has gone quiet"
