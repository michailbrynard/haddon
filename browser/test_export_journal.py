"""Taking the journal away, in a browser: the button, the files it lists, and a link that actually serves.

``tests/test_api_export.py`` proves the route to the byte.  What it cannot prove is the thing this page has to
get right.  The service answers with links under ``/v1``, the entrance where the proxy blanks the user header,
so an anchor pointing straight at one arrives unauthenticated and downloads a ``401``.  The page rewrites them
through ``/ui`` (``api.uiHref``), and only a real request through the real entrances can tell whether it did;
`tsc` type-checks the wrong URL perfectly.

So the check follows the link the page built and hashes what comes back against the digest printed beside it.
That is the export's claim, that these bytes are the ones the pinned verifier passed, checked end to end: the
service rendered and verified them, the record kept the digest, the page showed it, and the file a reviewer
receives hashes to it.
"""

from __future__ import annotations

import hashlib
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.usefixtures("served")


def test_a_reviewer_can_take_the_journal_away(page, served, world):
    page.goto(f"{served.base_url}/e/{world.entity_id}/reports?period=month&from={world.month}-01",
              wait_until="networkidle")

    page.get_by_role("button", name="Export to hledger").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("written out as plain journal files")

    dialog.get_by_role("button", name="Export", exact=True).click()
    # The export renders, stages, runs `hledger check --strict` and publishes before it answers, so this is the
    # one step here that is not instant.
    expect(dialog.get_by_text("accounts.journal", exact=True)).to_be_visible(timeout=60_000)
    expect(dialog).to_contain_text("Checked by hledger")

    link = dialog.get_by_role("link", name="Download accounts.journal")
    href = link.get_attribute("href")
    assert href and href.startswith("/ui/v1/"), (
        f"the download points at {href!r}; under /v1 the proxy blanks the user header and the reviewer gets a "
        f"401 instead of a journal")

    served_file = page.request.get(f"{served.base_url}{href}")
    assert served_file.status == 200, (href, served_file.status, served_file.text()[:300])
    body = served_file.body()
    assert b"account assets:cash" in body, body[:400]

    row = dialog.get_by_role("listitem").filter(has_text="accounts.journal").inner_text()
    shown = re.search(r"sha256 ([0-9a-f]{12})", row)
    assert shown, row
    assert hashlib.sha256(body).hexdigest().startswith(shown.group(1)), (
        "the file the reviewer downloaded is not the one whose digest the page showed")
