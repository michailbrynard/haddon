"""Writing an entry the bank never mentioned, in a browser, including the post that gets refused.

Balance is the service's rule (summed per commodity, at the precision each commodity declares), and the form
deliberately does not re-decide it.  That is only safe if two things hold, neither visible to ``tsc`` or to
``tests/``: the refusal has to arrive on screen in the service's own words, and the form has to still be there
afterwards.  A reviewer who loses four legs to a wrong sign writes the entry in a spreadsheet instead, and the
books stop being the record.

``tests/`` proves ``POST /entries`` to the cent but cannot see what the page sends.  Issue 0006 makes the
command vocabulary account paths and exact decimal strings, not the internal ids the picker holds and not the
micro integers responses are written in, and the conversion happens on the way out of the page.  So the request
body is captured off the wire and asserted.

"Balance the last leg" is exercised on a form that already has an amount in its last row.  The residual already
contains that row, so filling it with the negated residual would count the row's own amount twice and leave the
entry out.  The figure asserted below (``-100.00`` from ``100.00`` and ``-60.00``) tells a correct
implementation from that one, which is why this step is not on an empty row.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.usefixtures("served")

#: dated clear of the fixture month and of the period the reviewer workflow shuts, so the tests sharing this
#: set of books do not depend on which ran first
WHEN = "2025-06-10"
NARRATION = "DEMO - June rent accrued, invoice not yet received"
REASON = "DEMO - the cost belongs to June; the invoice arrives in July and the books should not wait for it."
DEBIT = "expenses:office"
CREDIT = "liabilities:payables"


def go(page, served, path: str) -> None:
    page.goto(f"{served.base_url}{path}", wait_until="networkidle")


def choose_account(page, field_id: str, path: str) -> None:
    """Open the picker for one leg and take the named account out of the chart."""
    page.locator(f"#{field_id}").click()
    page.get_by_placeholder("Search the chart…").fill(path)
    page.get_by_role("option", name=path, exact=True).click()
    expect(page.locator(f"#{field_id}")).to_contain_text(path)


def test_a_reviewer_can_write_an_entry_no_statement_line_stands_behind(page, served, world):
    sent: list[dict] = []

    def remember(request):
        if request.method == "POST" and request.url.endswith("/entries"):
            sent.append(json.loads(request.post_data or "{}"))

    page.on("request", remember)

    go(page, served, f"/e/{world.entity_id}/entries/new")
    expect(page.get_by_role("heading", name="Write an entry")).to_be_visible()

    # An empty form has done no arithmetic, so it claims none.  "The legs balance" over nothing would be a
    # reassurance about a sum nobody has performed.
    expect(page.get_by_text("The legs balance.")).to_have_count(0)

    # The books' own currency is what it offers to write in, and the other declared ones are there to pick.
    expect(page.get_by_role("button", name="USD")).to_have_attribute("aria-pressed", "true")

    page.locator("#entry-dt").fill(WHEN)
    page.get_by_label("What the entry says").fill(NARRATION)
    page.get_by_label("Why you are writing it").fill(REASON)

    choose_account(page, "entry-leg-account-0", DEBIT)
    page.locator("#entry-leg-amount-0").fill("100.00")
    choose_account(page, "entry-leg-account-1", CREDIT)
    page.locator("#entry-leg-amount-1").fill("-60.00")

    # An amount the commodity cannot hold is named, not dropped
    # A decimal comma is what half of Europe types.  It is not a number this ledger can hold, and the failure
    # guarded against is the row silently leaving the sum: it still looks filled in, and the form reports a
    # balance reached by leaving that row out.
    page.locator("#entry-leg-amount-1").fill("-60,00")
    expect(page.get_by_text("This amount is not in the sum").first).to_be_visible()
    expect(page.get_by_text("The legs balance.")).to_have_count(0)
    page.locator("#entry-leg-amount-1").fill("-60.00")

    # The refusal, and the form surviving it
    page.get_by_role("button", name="Post it to the books").click()

    refusal = page.get_by_role("alert")
    expect(refusal).to_contain_text("legs do not net to zero per commodity")
    # the service's own residual, per commodity, off the error's details rather than the page's arithmetic
    expect(page.get_by_text(re.compile(r"The USD legs come to\s+40\.00\s+between them"))).to_be_visible()

    # Everything that was typed is still typed.  The form does not pre-empt the rule, so it has to be somewhere
    # to go back to when the rule refuses.
    expect(page.get_by_label("What the entry says")).to_have_value(NARRATION)
    expect(page.get_by_label("Why you are writing it")).to_have_value(REASON)
    expect(page.locator("#entry-dt")).to_have_value(WHEN)
    expect(page.locator("#entry-leg-amount-0")).to_have_value("100.00")
    expect(page.locator("#entry-leg-amount-1")).to_have_value("-60.00")
    expect(page.locator("#entry-leg-account-0")).to_contain_text(DEBIT)
    expect(page.locator("#entry-leg-account-1")).to_contain_text(CREDIT)

    # The aid, doing the arithmetic
    # -100.00, not -40.00.  The residual is 40.00 and the last row already says -60.00; filling the row with the
    # negated residual would leave the entry 60.00 out.
    page.get_by_role("button", name="Balance the last leg").click()
    expect(page.locator("#entry-leg-amount-1")).to_have_value("-100.00")
    expect(page.get_by_text("The legs balance.")).to_be_visible()

    # And then it is written
    page.get_by_role("button", name="Post it to the books").click()
    expect(page).to_have_url(re.compile(rf"/e/{re.escape(world.entity_id)}/entries/[^/]+$"))

    expect(page.get_by_role("heading", name=NARRATION)).to_be_visible()
    expect(page.get_by_text("In the books", exact=True)).to_be_visible()
    # the legs, as the chart writes them, and the balance proved on the page rather than asserted
    expect(page.get_by_role("link", name="expenses · office")).to_be_visible()
    expect(page.get_by_role("link", name="liabilities · payables")).to_be_visible()
    expect(page.get_by_text("The legs balance.")).to_be_visible()
    # who wrote it: the proxy authenticated a person and the service turned that into the actor on the row
    expect(page.get_by_text(re.compile(r"written by ui:reviewer"))).to_be_visible()
    # and why, kept verbatim against the action rather than folded into the narration
    expect(page.get_by_role("heading", name="What happened to it")).to_be_visible()
    expect(page.get_by_text("Posted", exact=True).first).to_be_visible()
    expect(page.get_by_text(REASON)).to_be_visible()

    # What actually went over the wire (issue 0006)
    # Two requests: the one that was refused and the one that was written.  Both are asserted, because a refusal
    # is only informative if the service was sent what the reviewer meant.
    assert len(sent) == 2, f"expected a refused post and a written one, got {len(sent)}"
    refused, written = sent
    assert [leg["amount"] for leg in refused["legs"]] == ["100.00", "-60.00"]
    assert [leg["amount"] for leg in written["legs"]] == ["100.00", "-100.00"], \
        "the amounts are exact decimal strings in whole units, not micro integers"
    assert [leg["account"] for leg in written["legs"]] == [DEBIT, CREDIT], \
        "legs name account paths, not the internal ids the picker holds"
    assert all(leg["commodity"] == "USD" for leg in written["legs"])
    assert written["dt"] == WHEN and written["narration"] == NARRATION and written["reason"] == REASON
    assert "amount_micros" not in json.dumps(written), "micro units are a response shape, not a request one"


def test_the_form_is_not_offered_where_the_command_is_not_published(page, served, world):
    """The whole screen is gated on `post_entry` appearing in `/capabilities.commands` (issue 0006).

    The negative branch cannot be reached by configuring the service (`post_entry` is core and always mounted),
    so the capabilities document is intercepted on its way to the browser and the command taken out of it.  A
    gate asserted only where it lets everything through would pass with the check deleted.
    """
    def without_post_entry(route):
        answered = route.fetch()
        body = answered.json()
        body["commands"] = [c for c in body["commands"] if c["name"] != "post_entry"]
        route.fulfill(response=answered, json=body)

    page.route(re.compile(r"/ui/v1/entities/[^/]+/capabilities"), without_post_entry)
    go(page, served, f"/e/{world.entity_id}/entries/new")

    expect(page.get_by_text("These books are not written to from here")).to_be_visible()
    expect(page.get_by_role("button", name="Post it to the books")).to_have_count(0)
    expect(page.get_by_label("What the entry says")).to_have_count(0)
