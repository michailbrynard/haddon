"""A second rule the reviewer interface's words have to keep: a placeholder never asserts a fact.

A reason field on a bank-statement line once carried ``placeholder="This is a client refund, not office
spend"``: a complete sentence making an accounting claim, rendered inside an input.  Three things go wrong at
once:

* it looks like stored content.  Placeholder grey against value white is a thin distinction on a phone in a
  dark room; ``web/src/index.css`` measures it, and the best a placeholder can do while staying readable is
  about 3:1 against a value, so the eye cannot be asked to carry this alone;
* the claim is about nothing.  It is a canned example, unrelated to the line on screen;
* the reader can therefore carry away a false statement about the books, which is the one thing a ledger
  interface must never cause.

So a placeholder shows the shape or the expectation of an input ("Filter the chart"), never a plausible
example of its content.  Best of all is no placeholder; what a field wants belongs in its
label and in the helper text beneath it, where the interface's explanatory copy already lives and where it
cannot be mistaken for an answer.

It is an allowlist rather than a rule about the words because every linguistic test tried was wrong in one
direction or the other.  "Office rent, March" is short and verbless and is still a plausible narration; "A
transfer between our own accounts" contains no copula at all.  A guard that fires on some example sentences
and not others teaches that the rest are fine, which is worse than no guard.  What can be checked exactly is
that every placeholder in the app is one somebody declared here on purpose.  Adding one costs a line in this
file and the sentence of reasoning that goes with it, which is the deliberation the rule is asking for.  A new placeholder failing this test is the test working.

Like ``tests/test_ui_copy.py`` this reads the frontend sources rather than rendered pages: a placeholder is a
property of the text somebody typed, and rendering every state of every page to find it would mean a browser
in the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT.joinpath("web", "src")

#: shadcn's own components are vendored by its CLI and are not ours to word.
VENDORED = ("components", "ui")

#: Every placeholder the interface is allowed to render, and why each one cannot be read as a record.  Both
#: name what the input does; neither could be mistaken for an account, an amount or a booking.
ALLOWED = {
    "Search the chart…": "names the action of a search box inside the account picker",
    "Filter the chart": "names the action of the filter box above the chart of accounts",
}

#: `placeholder="..."` and `placeholder={"..."}`, with the literal captured.  Deliberately does not match
#: `placeholder:` (a Tailwind variant inside a className) or `placeholder,` (a prop being destructured).
LITERAL = re.compile(r'placeholder=\{?"([^"\n]*)"\}?')

#: Any other use of the attribute (`placeholder={expr}`) is a value this guard cannot read, so it cannot vouch
#: for it either.
ANY_ATTRIBUTE = re.compile(r"placeholder\s*=")


def sources() -> list[Path]:
    return sorted(p for p in WEB.rglob("*.ts*") if VENDORED not in p.relative_to(WEB).parts[:2])


def test_the_placeholder_guard_is_reading_the_app():
    """A guard that silently stops reading anything is a guard that always passes."""
    assert WEB.is_dir(), f"{WEB} is missing; the placeholder guard would pass over nothing"
    found = sources()
    assert len(found) >= 12, f"only {len(found)} interface sources found; the guard is not reading the app"
    assert any(p.name == "BookingDialog.tsx" for p in found), "the booking dialog is what this rule is for"


def test_every_placeholder_is_one_that_was_declared_here():
    offenders: list[tuple[str, str]] = []
    for path in sources():
        for text in LITERAL.findall(path.read_text(encoding="utf-8")):
            if text not in ALLOWED:
                offenders.append((str(path.relative_to(ROOT)), text))
    assert not offenders, (
        f"undeclared placeholders: {offenders}. A placeholder shows the shape or the expectation of an "
        f"input, never a plausible example of its content -- inside the field it reads as something already "
        f"recorded. Prefer no placeholder: say what the field wants in its label or its helper text. If this "
        f"one really is a shape hint, add it to ALLOWED in this file with the reason it cannot be misread."
    )


def test_no_placeholder_is_hidden_behind_an_expression():
    """`placeholder={something}` is a value only the running app knows, so the guard cannot vouch for it."""
    offenders = []
    for path in sources():
        body = path.read_text(encoding="utf-8")
        if len(ANY_ATTRIBUTE.findall(body)) != len(LITERAL.findall(body)):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"{offenders} sets a placeholder from an expression. This guard can only read literals, so a "
        f"computed placeholder would pass it unchecked; write the literal, or drop the placeholder."
    )


def test_the_guard_would_actually_catch_the_one_it_is_for():
    """The check is only worth having if it fails on the sentence that prompted it."""
    reported = 'placeholder="This is a client refund, not office spend"'
    (found,) = LITERAL.findall(reported)
    assert found not in ALLOWED, "the reported placeholder must never be declared acceptable"
    assert LITERAL.findall('placeholder={"Office rent, March"}'), "the braced form has to be caught too"
    assert not LITERAL.findall('className="placeholder:text-placeholder"'), "a Tailwind variant is not a placeholder"


def test_a_field_still_says_what_it_wants_somewhere():
    """The rule is "not in the input", not "never explain a field". If the helper text under the booking
    fields disappeared, this file would have been read as "say nothing", which is the opposite of the intent."""
    dialog = WEB.joinpath("components", "BookingDialog.tsx").read_text(encoding="utf-8")
    assert "text-xs text-muted-foreground" in dialog, "the booking dialog has lost its helper text"
    assert "Kept as the entry's description" in dialog, "the narration field no longer says what it is for"
