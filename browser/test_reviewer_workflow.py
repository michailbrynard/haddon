"""The reviewer's loop, in a browser, end to end.

A reviewer's month is one sequence: open the month, read what is unexplained, go to a line and find the entry
and the document behind it, then close the period.  It is the only sequence in which the service's state, the
cache holding it and the words on the page all have to agree at once, and the one where a mistake is expensive
because the output is a closed set of books.  The other pages (chart, reports, trial balance) each render one
endpoint once, and `tests/` proves those endpoints far better than a browser can.

The two writes at the end are the point.  The last step closes March and then, without reloading the page,
closes April: an ordinary evening's work, and the one sequence a stale cache cannot survive.
`useEntityMutation` invalidates `["entity", id]` and `["entities"]`.  The second looks redundant, but it is
where `lock_through`, `lock_version` and `lock_close_id` live.  Remove it and the write still lands, but the
page keeps what it knew before.  It goes on saying "nothing is closed yet", and the next close is refused as
stale against a lock this very page set.  Neither `tsc` nor the Python suite can see that, because neither has
a cache.

Each step below asserts something a person would notice rather than an implementation detail: the three figures
a month hangs off, to the cent; the document name and fingerprint under a line; the date the books are shut
through.  If an assertion here fails, the sentence it fails on is the sentence a reviewer would have read.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.usefixtures("served")

CLOSE_REASON = "March is reviewed; the two open lines are recorded below."
OVERRIDE_REASON = "The two open lines are a known transfer and a receipt still to come; both are chased."
SECOND_CLOSE_REASON = "April held nothing, so it is shut behind March."
SECOND_OVERRIDE_REASON = "No statement is expected for April; the account was dormant."


def go(page, served, path: str) -> None:
    page.goto(f"{served.base_url}{path}", wait_until="networkidle")


def close_over(page, reason: str) -> None:
    """Answer every check that is blocking this close.  A close with an unanswered one leaves the button
    disabled, so this is also what makes the click below meaningful."""
    boxes = page.get_by_label("Close over this check, and say why")
    expect(boxes.first).to_be_visible()
    for i in range(boxes.count()):
        boxes.nth(i).fill(reason)


def test_a_reviewer_can_work_a_month_from_the_headline_to_a_closed_period(page, served, world):
    loaded: list[str] = []
    page.on("response", lambda r: loaded.append(r.url) if r.url.endswith(".js") and r.ok else None)

    # The way in, and the bundle under test
    # One set of books goes straight through to its month rather than offering a list of one.
    go(page, served, "/")
    expect(page).to_have_url(re.compile(rf"/e/{re.escape(world.entity_id)}/month$"))

    # The browser executed the bundle that is on disk right now.  A run against a stale build passes and proves
    # nothing, so this is asserted rather than assumed.
    assert any(url.endswith(served.asset) for url in loaded), \
        f"the page did not load {served.asset}; it loaded {loaded}"

    # The month, and what it says about itself
    go(page, served, f"/e/{world.entity_id}/month?month={world.month}")
    expect(page.get_by_role("heading", name="March 2025")).to_be_visible()

    # The three figures the page hangs off, to the cent.  The numbers, not their labels: they carry the
    # reconciliation's arithmetic, sign convention and formatting all the way to the screen, and a reviewer acts
    # on them.
    standing = page.locator("section").first
    expect(standing).to_contain_text("Bank")
    expect(standing).to_contain_text(f"{world.bank_total} USD")
    expect(standing).to_contain_text("Books")
    expect(standing).to_contain_text(f"{world.books_total} USD")
    expect(standing).to_contain_text("Difference")
    expect(standing).to_contain_text(f"{world.difference} USD")
    expect(standing).to_contain_text("the bank holds more")

    # What is unexplained, and what is missing entirely: the two kinds of "not finished" a month can be in.
    expect(page.get_by_role("heading", name="3 things need your attention")).to_be_visible()
    expect(page.get_by_text(f"{world.unexplained} lines still need an account")).to_be_visible()
    expect(page.get_by_text("No statement covers 1 Mar to 31 Mar")).to_have_count(world.uncovered_accounts)

    # Triage: show only the unexplained lines.
    expect(page.locator("tbody tr")).to_have_count(world.statement_lines)
    page.get_by_role("button", name="Show them").click()
    expect(page.locator("tbody tr")).to_have_count(world.unexplained)
    expect(page.get_by_text("Give it an account").first).to_be_visible()
    # the line that was answered without being booked is not in the reviewer's way
    expect(page.get_by_text(world.excluded_reason)).to_have_count(0)

    # A line, the entry behind it, and the document behind that
    page.get_by_role("button", name="All").click()
    expect(page.locator("tbody tr")).to_have_count(world.statement_lines)
    page.get_by_role("button", name=f"Actions for {world.booked_description}").click()
    page.get_by_role("menuitem", name="Where did this come from?").click()

    expect(page.get_by_role("heading", name=world.booked_description)).to_be_visible()
    # what became of it: the entry, its narration and the account it went to
    expect(page.get_by_text("What became of it")).to_be_visible()
    expect(page.get_by_text("Booked to", exact=True)).to_be_visible()
    expect(page.get_by_text("expenses:office").first).to_be_visible()
    # where it came from: the document, the row in it, and the fingerprint of the bytes
    expect(page.get_by_text("Where it came from")).to_be_visible()
    expect(page.get_by_text(world.document_name, exact=True)).to_be_visible()
    # The fingerprint sits behind a disclosure (issue 0007, pitfall 3): lead with the document, and offer the
    # technical id to whoever came for it.
    page.get_by_text("Fingerprint").click()
    expect(page.get_by_text(world.document_sha256)).to_be_visible()
    page.get_by_text("The raw row, exactly as it was read").click()
    expect(page.locator("pre")).to_contain_text(world.booked_reference)

    # The document itself, actually opened (issue 0007).
    #
    # The href is read off the page rather than assembled here, because the trap is in the href.  The service
    # returns links under `/v1`, the agent entrance, where the proxy blanks the user header, so an anchor pointed
    # straight at one arrives unauthenticated.  `uiHref` rewrites it to `/ui/v1`, and only following the actual
    # link can tell whether it did.
    opened = page.get_by_role("link", name="Open")
    # The address arrives on `links.content` with the document read, and until then the link is a placeholder.
    # Wait for the real one rather than reading the attribute straight away.
    expect(opened).not_to_have_attribute("href", "#")
    href = opened.get_attribute("href")
    assert href and "/ui/v1/" in href, f"the evidence link does not go through the browser entrance: {href}"

    fetched = page.request.get(f"{served.base_url}{href}" if href.startswith("/") else href)
    assert fetched.status == 200, f"{fetched.status} from {href}: {fetched.text()[:300]}"
    # the bytes the browser was handed are the bytes the fixture wrote, which the service verified against the
    # hash they were registered under before returning any of them
    assert fetched.text() == world.document_text
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert world.document_name in fetched.headers["content-disposition"]

    # No response anywhere in this page's traffic names where the service keeps its files.
    assert "blob_path" not in page.content()

    # The entry's own page: what it is, and what it rests on.
    page.get_by_role("link", name=re.compile(re.escape(world.booked_description))).first.click()
    expect(page.get_by_role("heading", name=world.booked_description)).to_be_visible()

    # what it is now, said in a word and then in a sentence
    expect(page.get_by_text("In the books", exact=True)).to_be_visible()

    # the legs, and the balance proved on screen.  This is the check a reviewer is here to make; a page that only
    # listed the postings would leave them to do the addition.
    # `prettyPath` swaps the separator and changes nothing else, so this is the chart's own wording
    expect(page.get_by_role("link", name="expenses · office")).to_be_visible()
    expect(page.get_by_text("The legs balance.")).to_be_visible()

    # what happened to it, leading with the business action rather than the event kind
    expect(page.get_by_role("heading", name="What happened to it")).to_be_visible()
    expect(page.get_by_text("Booked from a statement line")).to_be_visible()

    # and the document is reachable from here too, which is the acceptance criterion: every posted entry is
    # reachable from the register, the account, the statement allocation and the audit links
    expect(page.get_by_text(world.document_name, exact=True).first).to_be_visible()

    # Closing, over what is still open
    go(page, served, f"/e/{world.entity_id}/close?through=2025-03-31")
    expect(page.get_by_text("Nothing is closed yet")).to_be_visible()
    # the checks are rendered as the service evaluated them; the open lines are what they are about
    expect(page.get_by_text(re.compile(r"checks are blocking this close"))).to_be_visible()
    close_over(page, OVERRIDE_REASON)
    page.get_by_label("Why", exact=True).fill(CLOSE_REASON)
    page.get_by_role("button", name=re.compile("^Close")).click()

    # The lock the page now shows is the one it just set, with the sentence somebody wrote against it.
    expect(page.get_by_text("Period closed")).to_be_visible()
    expect(page.get_by_text(re.compile("The lock is at 31 March 2025"))).to_be_visible()
    expect(page.get_by_text("Closed through 31 March 2025")).to_be_visible()
    expect(page.get_by_text(CLOSE_REASON)).to_be_visible()
    expect(page.get_by_text("Period closed")).to_have_count(0, timeout=20_000)   # the toast covers the form

    # The second write, on a page that has not been reloaded since.  The second close carries
    # `expected_version`, which comes from the entity listing, which the first close moved.  If that listing was
    # not invalidated, this is refused as stale against a lock this very page set, and the page looks completely
    # normal right up until the refusal.
    page.get_by_label("Close through").fill("2025-04-30")
    expect(page.get_by_text(re.compile("This close would newly shut.*30 April 2025"))).to_be_visible()
    close_over(page, SECOND_OVERRIDE_REASON)
    page.get_by_label("Why", exact=True).fill(SECOND_CLOSE_REASON)
    page.get_by_role("button", name=re.compile("^Close")).click()

    expect(page.get_by_text("Closed through 30 April 2025")).to_be_visible()
    expect(page.get_by_text(SECOND_CLOSE_REASON)).to_be_visible()
    expect(page.get_by_text(re.compile("stale|conflict", re.I))).to_have_count(0)
