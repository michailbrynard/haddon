"""The review inbox, in a browser: does the screen say what the service says?

`tests/` proves each of the five endpoints this screen reads, and `tsc` proves the screen compiles.  Neither
can see what goes wrong here, which is disagreement between registers: five reads, five cache entries, one
page, and nothing relating the number on screen to the number the service would give if asked right now.  A
stale bundle, a cache key that invalidates the wrong thing, an audit list keyed on the wrong id: all of these
type-check.

So this test never asserts a hard-coded count.  It asks the service the same five questions the page asks and
asserts the screen agrees.  That stays true when the fixture changes, and fails when the page reads the wrong
endpoint, filters it differently, or stops at the first page of a cursor.

Two steps:

1. Agreement: the five registers against five independent reads made by the test itself.
2. Invalidation: close the period on the closing page and come straight back, where two registers populated
   from two different endpoints both have to have moved.  This is asserted immediately after the write, and
   the test measures how long it took, because the property only exists inside the query client's
   `staleTime`.  After that window a remount refetches anyway and a broken cache key would pass, so the clock
   is asserted rather than assumed.
"""

from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.usefixtures("served")


# Books of its own, on purpose.  `conftest.py` builds one set of books and one server for the session.  This
# test closes a period, and a period can only be closed once, so sharing would mean whichever test ran second
# failed on a lock the other one set.  Shadowing `world` and `served` here gives this module its own database
# and its own uvicorn.  `conftest`'s `page` fixture resolves `served` from this module for these tests and from
# `conftest` for the rest, so the artifacts-on-failure behaviour is unchanged.
@pytest.fixture(scope="session")
def world(tmp_path_factory):
    from world import build

    root = tmp_path_factory.mktemp("haddon-inbox")
    return build(root.joinpath("entity.db"), root.joinpath("blobs"))


@pytest.fixture(scope="session")
def served(world):
    from server import Harness

    with Harness({world.entity_id: world.db_path}, blob_root=world.blobs) as running:
        yield running

#: `main.tsx` sets `staleTime: 10_000`.  Step 2 only proves invalidation while it is inside that window.
STALE_WINDOW_SECONDS = 10

THROUGH = "2025-03-31"
THROUGH_WORDS = "31 March 2025"
CLOSE_REASON = "March is reviewed from the inbox; what is still open is recorded against the checks."
OVERRIDE_REASON = "The open lines are a known transfer and a receipt still to come; both are chased."


def go(page, served, path: str) -> None:
    page.goto(f"{served.base_url}{path}", wait_until="networkidle")


def read(page, served, entity: str, path: str) -> dict:
    """Ask the service directly, through the same entrance the page uses."""
    response = page.request.get(f"{served.base_url}/ui/v1/entities/{entity}{path}")
    assert response.status == 200, f"{response.status} from {path}: {response.text()[:300]}"
    return response.json()


def read_all(page, served, entity: str, path: str) -> list:
    """The whole of a cursor-paginated collection.

    `/lines` applies its `state` filter after each page is selected, so a page can come back short, or empty,
    with `next_cursor` still set.  A helper that stopped at the first short page would compare the screen
    against the wrong number, and would agree with a page making the same mistake.
    """
    items: list = []
    cursor = None
    while True:
        joiner = "&" if "?" in path else "?"
        suffix = f"{joiner}cursor={cursor}" if cursor else ""
        body = read(page, served, entity, f"{path}{suffix}")
        items.extend(body["items"])
        cursor = body.get("next_cursor")
        if not cursor:
            return items


def register(page, title: str):
    """One register, found by its heading and returned as the whole card."""
    return page.locator("section").filter(has=page.get_by_role("heading", name=title, exact=False))


def count_in(page, title: str) -> int:
    """The number a register states about itself."""
    badge = register(page, title).locator("span.rounded-full").first
    expect(badge).to_be_visible()
    return int(badge.inner_text().strip())


def test_the_inbox_says_what_the_service_says_and_keeps_saying_it_after_a_write(page, served, world):
    entity = world.entity_id
    loaded: list[str] = []
    page.on("response", lambda r: loaded.append(r.url) if r.url.endswith(".js") and r.ok else None)

    # The way in, and the bundle under test
    # Reached the way a reviewer reaches it, by the tab rather than the address, so the tab's line in
    # `AppShell` is under test too.  A route with no tab is a screen nobody finds.
    go(page, served, f"/e/{entity}/month")
    assert any(url.endswith(served.asset) for url in loaded), \
        f"the page did not load {served.asset}; it loaded {loaded}"
    page.get_by_role("link", name="Inbox", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/e/{re.escape(entity)}/review$"))
    expect(page.get_by_role("heading", name="Inbox")).to_be_visible()

    # 1. unexplained statement lines
    open_lines = [ln for ln in read_all(page, served, entity, "/lines?state=unexplained&state=partly_explained")]
    assert open_lines, "the fixture has no unexplained lines, so this register would assert nothing"
    assert count_in(page, "Unexplained on a statement") == len(open_lines)
    # and the first of them is reachable: the count is only useful because it is a way to the line
    first = register(page, "Unexplained on a statement").get_by_role("link").first
    expect(first).to_have_attribute("href", re.compile(rf"/e/{re.escape(entity)}/lines/"))

    # 2. awaiting a document
    provisional = read_all(page, served, entity, "/provisional?limit=200")
    assert provisional, "the fixture has nothing awaiting a document"
    assert count_in(page, "Waiting on a document") == len(provisional)
    waiting = register(page, "Waiting on a document")
    expect(waiting.get_by_text(world.awaited_narration)).to_be_visible()
    expect(waiting.get_by_text(world.awaiting, exact=False)).to_be_visible()
    expect(waiting.get_by_role("link", name=world.awaited_narration)).to_have_attribute(
        "href", re.compile(rf"/e/{re.escape(entity)}/entries/"))

    # 3. recoverable tax not taken
    tax = read(page, served, entity, "/unrecovered-tax?limit=200")
    assert tax["items"], "the fixture has no unrecovered tax"
    assert count_in(page, "Recoverable tax not recovered") == len(tax["items"])
    taxes = register(page, "Recoverable tax not recovered")
    expect(taxes.get_by_text(world.tax_code, exact=False)).to_be_visible()
    # The totals are the service's own per-commodity figures, printed one per commodity and never summed
    # across them.  Asserting the count of totals against the service catches a page that added them up.
    for commodity in tax["totals"]:
        expect(taxes.get_by_text(f"Total {commodity}", exact=True)).to_be_visible()
    printed = taxes.get_by_role("definition")
    expect(printed).to_have_count(len(tax["totals"]))
    expect(printed.first).to_contain_text(world.tax_total)

    # 4. blocking the close
    # Pinned to a date rather than left on today's, so the register is about the period the fixture is for.
    go(page, served, f"/e/{entity}/review?through={THROUGH}")
    checks = read(page, served, entity, f"/close-checks?through={THROUGH}")
    blocking = [c for c in checks["checks"] if c["blocking"]]
    assert blocking, "nothing is blocking this close, so this register would assert nothing"
    closing = register(page, f"Blocking a close through {THROUGH_WORDS}")
    assert count_in(page, f"Blocking a close through {THROUGH_WORDS}") == len(blocking)
    # Every blocking check's title and its refusal, in the service's own words.  Rebuilding the close policy in
    # the page is how two implementations come to disagree about whether a period may be shut.
    for check in blocking:
        expect(closing.get_by_text(check["title"], exact=True)).to_be_visible()
        expect(closing.get_by_text(check["reason"], exact=False).first).to_be_visible()
    # and the first thing the first check says is outstanding. `.first` because two checks over the same
    # uncovered months report the same sentence, which is the service being consistent, not a duplicate.
    for item in blocking[0]["outstanding"][:1]:
        expect(closing.get_by_text(item["message"], exact=False).first).to_be_visible()
    # a truncated register still says how much it is not showing, so a count is never quietly a page size
    if len(blocking[0]["outstanding"]) > 6 or blocking[0]["outstanding_truncated"]:
        expect(closing.get_by_text(re.compile(r"^and \d+ more$")).first).to_be_visible()

    # 5. recently changed
    events = read(page, served, entity, "/events?since=0&limit=1")
    assert events["high_water"] > 0
    recent = register(page, "Recently changed")
    expect(recent.get_by_text("Booked from a statement line").first).to_be_visible()

    # No anchor leaves by the entrance that blanks the caller
    # Service links live under `/v1`, where the proxy blanks the user header, so an anchor pointed at one
    # arrives unauthenticated.  Everything this screen links to is a router path; this is the guard that keeps
    # it that way.
    hrefs = page.locator("a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert not [h for h in hrefs if h and h.startswith("/v1/")], \
        f"an inbox link goes to the agent entrance: {[h for h in hrefs if h and h.startswith('/v1/')]}"

    # Step 2: one write, two registers, no reload.
    # Closing March moves two of the five reads at once: `/close-checks` stops reporting what was blocking,
    # and `/events` gains a `period.closed`.  They are separate endpoints under separate cache keys, and
    # nothing but a page that invalidated both makes them agree afterwards.
    page.get_by_role("link", name=re.compile("Answer these on the closing page")).click()
    expect(page).to_have_url(re.compile(rf"/close\?through={THROUGH}$"))

    boxes = page.get_by_label("Close over this check, and say why")
    expect(boxes.first).to_be_visible()
    for i in range(boxes.count()):
        boxes.nth(i).fill(OVERRIDE_REASON)
    page.get_by_label("Why", exact=True).fill(CLOSE_REASON)
    page.get_by_role("button", name=re.compile("^Close")).click()
    expect(page.get_by_text(f"Closed through {THROUGH_WORDS}")).to_be_visible()
    wrote_at = time.monotonic()

    # Straight back, through history rather than the tab: the tab is `/review` with no query string, which
    # would ask about today instead of about the period just closed.  `go_back` is a client-side history
    # navigation, so the page is not reloaded and the query cache is the one the write acted on.
    bundles = len(loaded)
    page.go_back()
    expect(page.get_by_role("heading", name="Inbox")).to_be_visible()
    assert len(loaded) == bundles, "the page reloaded, so the cache under test was thrown away and rebuilt"

    # The lock the service now holds.  It comes from the entity listing, a different cache entry again, which
    # `useEntityMutation` invalidates second.
    listed = page.request.get(f"{served.base_url}/ui/v1/entities").json()["items"]
    lock = next(e for e in listed if e["entity_id"] == entity)["lock_through"]
    assert lock == THROUGH, f"the close did not move the lock; the service says {lock}"

    # register 4, off `/close-checks` and the entity listing: the period it was asked about is now shut
    expect(register(page, f"Blocking a close through {THROUGH_WORDS}")
           .get_by_text(f"These books are closed through {THROUGH_WORDS}", exact=False)).to_be_visible()
    # register 5, off `/events`: the same write, arriving by an entirely different read
    expect(register(page, "Recently changed").get_by_text("Period closed").first).to_be_visible()
    # `.first` because the close writes two events that carry the same sentence: the period closing and the
    # close record itself.
    expect(register(page, "Recently changed").get_by_text(CLOSE_REASON).first).to_be_visible()

    # The clock, asserted rather than assumed: outside the query client's stale window a remount refetches on
    # its own and the two assertions above would hold even with the invalidation removed.  If this ever fires,
    # the step has stopped being able to fail and needs rewriting, not a longer timeout.
    elapsed = time.monotonic() - wrote_at
    assert elapsed < STALE_WINDOW_SECONDS, (
        f"the round trip took {elapsed:.1f}s, past the {STALE_WINDOW_SECONDS}s stale window, so this step "
        f"could no longer tell an invalidated cache from a refetched one")
