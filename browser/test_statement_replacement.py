"""Replacing a statement, in a browser: the refusal a reviewer has to act on, and then the write itself.

`replace_statement` is a published command (issue 0006), and the screen for it has to say why it will not run
yet.  Every one of its refusals is a state of the books rather than a slip in a form (a line somebody booked, a
document already loaded, a month somebody closed), and each has a specific thing to go and do.  So the sequence
driven below is the one a reviewer actually walks:

    open the statements → find one that cannot be replaced and see which line holds it → go to that line →
    come back to one that can be → be refused for loading a document that is already loaded, and reach the
    statement it belongs to → replace properly → read the chain from both ends.

`tests/` proves the service refuses these writes and says why; `tsc` proves the screen compiles.  Neither can
see the layer in between: whether the refusal's `details`, a list of ids, became links a person can follow, and
whether the page that has just written a replacement shows the replacement or the thing it replaced.  When
either is wrong the page still looks completely normal.

The last steps are two reads across one write, for the same reason the reviewer-workflow check ends with two
writes.  The replacement lands, the selection moves to the statement just written, and the predecessor has to
have stopped saying it is live, which it only does if the write invalidated more than the row it wrote.

Each assertion below is a sentence a reviewer would have read, not an implementation detail.  The two figures
this hangs on, what the lines sum to and what the balances declare, are asserted to the cent because they
decide whether somebody presses the button.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

REF = "DEMO: kept in the review folder, alongside the original download"
BAD_REASON = "DEMO: re-reading the june euro statement, this time to the end of the document"
REASON = "DEMO: the parse stopped at the page break and missed the last line; this is the whole document"


@pytest.fixture(scope="session")
def books(tmp_path_factory):
    """Books of this check's own, for the reason `statements_world.py` opens with: a closed period refuses a
    replacement before anything else is looked at, and `world.py` ends closed."""
    from statements_world import build

    root = tmp_path_factory.mktemp("haddon-replacement")
    return build(root.joinpath("entity.db"), root.joinpath("blobs"))


@pytest.fixture(scope="session")
def serving(books):
    from server import Harness

    with Harness({books.entity_id: books.db_path}, blob_root=books.blobs) as running:
        yield running


def go(page, serving, path: str) -> None:
    page.goto(f"{serving.base_url}{path}", wait_until="networkidle")


def section(page, heading: str):
    """The panel a heading belongs to, so the same words elsewhere on the page cannot satisfy an assertion.

    The booked line's description is also in the lines table above it.  `.last` because a refusal is rendered
    inside the form it refused, so both sections contain the heading and the innermost is the one wanted."""
    return page.locator("section").filter(has=page.get_by_role("heading", name=heading)).last


def test_a_reviewer_is_told_what_holds_a_statement_in_place_and_then_replaces_one(page, serving, books):
    loaded: list[str] = []
    page.on("response", lambda r: loaded.append(r.url) if r.url.endswith(".js") and r.ok else None)

    statements = f"/e/{books.entity_id}/statements"
    go(page, serving, statements)
    expect(page.get_by_role("heading", name="Statements")).to_be_visible()

    # The browser executed the bundle that is on disk right now.  A run against a stale build passes and proves
    # nothing, so this is asserted rather than assumed.
    assert any(url.endswith(serving.asset) for url in loaded), \
        f"the page did not load {serving.asset}; it loaded {loaded}"

    # Both statements, and both standing on their own: nothing has been replaced yet.
    expect(page.get_by_role("cell", name="assets:cash", exact=True)).to_be_visible()
    expect(page.get_by_role("cell", name="assets:cash-eur", exact=True)).to_be_visible()
    expect(page.get_by_text("Live", exact=True)).to_have_count(2)

    # The one that cannot be replaced, and the line that holds it
    page.get_by_role("cell", name="assets:cash", exact=True).click()
    expect(page.get_by_role("heading", name=re.compile(r"assets:cash · 1 Jun 2025 to 30 Jun 2025"))).to_be_visible()

    blocked = section(page, "One line of this statement is still booked")
    expect(blocked).to_be_visible()
    # It names the line rather than the statement: "2 line(s) of statement stm_… are still booked" is the
    # service's own wording and gives a reviewer nothing to click.
    expect(blocked).to_contain_text(books.usd_booked_description)
    expect(blocked.get_by_role("link", name="The entry it posted")).to_have_attribute(
        "href", f"/e/{books.entity_id}/entries/{books.usd_booked_entry_id}")

    # And there is no form. A screen that offered one here would be offering a refusal.
    expect(page.get_by_role("heading", name="Replace this statement")).to_have_count(0)

    # The way out of the refusal is a real address, not a sentence about one.
    blocked.get_by_role("link", name="Open the line").click()
    expect(page).to_have_url(re.compile(rf"/e/{re.escape(books.entity_id)}/lines/"))
    expect(page.get_by_role("heading", name=books.usd_booked_description)).to_be_visible()

    # The one that can be, refused for a document already loaded
    go(page, serving, statements)
    page.get_by_role("cell", name="assets:cash-eur", exact=True).click()
    expect(page.get_by_role("heading", name="Replace this statement")).to_be_visible()

    page.get_by_role("button", name="A different file").click()
    page.set_input_files("#replace-file", files=[{
        "name": books.usd_document,
        "mimeType": "text/csv",
        "buffer": books.usd_document_text.encode("utf-8"),
    }])
    # The hash is computed from the bytes the browser was handed, not typed, so this proves the whole path:
    # picked file → digest → the identity the ledger refuses on.
    expect(page.get_by_text(re.compile(r"^[0-9a-f]{64}$"))).to_be_visible()
    page.get_by_label("Where the original is kept").fill(REF)
    page.get_by_label("Why this statement is being replaced").fill(BAD_REASON)
    page.get_by_role("button", name="Replace this statement").click()

    refused = section(page, "These bytes are already loaded as a statement")
    expect(refused).to_be_visible()
    expect(refused).to_contain_text("a statement is loaded once")          # the service's own words, kept
    expect(refused).to_contain_text(books.usd_document)
    refused.get_by_role("link").first.click()
    # The refusal named a statement; following it arrives at that statement, which is the one with the booked
    # line. Nothing but clicking it can tell whether the id in `details` became a working address.
    expect(page).to_have_url(re.compile(rf"statement={re.escape(books.usd_id)}"))
    expect(section(page, "One line of this statement is still booked")).to_be_visible()

    # Replacing it, with the line the parse missed
    go(page, serving, f"{statements}?statement={books.eur_id}")
    expect(page.get_by_role("heading", name="Replace this statement")).to_be_visible()
    expect(page.get_by_text("The statement balances.")).to_be_visible()

    dt, description, reference, amount = books.eur_missing
    page.get_by_role("button", name="Add a line").click()
    row = books.eur_lines_read + 1
    page.get_by_label(f"Date of line {row}").fill(dt)
    page.get_by_label(f"Description of line {row}").fill(description)
    page.get_by_label(f"Reference of line {row}").fill(reference)
    page.get_by_label(f"Amount of line {row}").fill(amount)

    # A statement's lines have to account for its own movement, and the page says so while somebody is typing
    # rather than leaving it to be the service's answer after they press the button.
    expect(page.get_by_text("A statement whose lines do not account for its own movement is refused.")).to_be_visible()
    page.get_by_label("Closing balance", exact=True).fill(books.eur_true_closing)
    expect(page.get_by_text("The statement balances.")).to_be_visible()

    page.get_by_label("Why this statement is being replaced").fill(REASON)
    page.get_by_role("button", name="Replace this statement").click()

    # The page is now on the statement it just wrote: four lines, and the row the first reading never reached.
    expect(page.get_by_text("Statement replaced")).to_be_visible()
    expect(page.get_by_role("heading", name="What this statement supersedes, and what superseded it")).to_be_visible()
    replacement = page.locator("section").filter(has_text="Replaces").last
    expect(replacement).to_contain_text("1 Jun 2025 to 30 Jun 2025")
    expect(replacement).to_contain_text("3 lines")                       # what the earlier reading said
    read = section(page, "What was read, and what became of it")
    expect(read.locator("tbody tr")).to_have_count(books.eur_lines_read + 1)
    expect(read).to_contain_text(description)
    expect(read).to_contain_text(reference)

    # And the predecessor, which has stopped being live
    # The second read across the one write.  The replacement invalidates the listing as well as the row it
    # wrote.  Without that, this page goes on showing two live statements, one of them the statement it has just
    # superseded, while looking completely normal.
    expect(page.get_by_text("Replaced", exact=True)).to_be_visible()
    expect(page.get_by_text("Live — replaced an earlier reading")).to_be_visible()

    page.get_by_role("button", name=re.compile(r"1 Jun 2025 to 30 Jun 2025")).last.click()
    expect(page).to_have_url(re.compile(rf"statement={re.escape(books.eur_id)}"))
    expect(page.get_by_text("This reading was replaced.")).to_be_visible()
    expect(page.get_by_text(REASON)).to_have_count(0)                    # the reason is the successor's, not its
    expect(page.get_by_role("button", name="The statement that replaced it")).to_be_visible()
    # A statement that has been replaced offers no form: it is no longer what the books read.
    expect(page.get_by_role("heading", name="Replace this statement")).to_have_count(0)

    # And the way in, from the month review
    # The screen has no tab of its own, so the month's provenance panel is how a reviewer gets to it: from a
    # figure, to the document behind it, to the reading of that document.
    go(page, serving, f"/e/{books.entity_id}/month?month=2025-06")
    page.get_by_text(re.compile(r"statements behind these figures")).click()
    page.get_by_role("link", name=books.usd_document).click()
    expect(page).to_have_url(re.compile(rf"/statements\?statement={re.escape(books.usd_id)}"))
    expect(page.get_by_role("heading", name="Statements")).to_be_visible()
