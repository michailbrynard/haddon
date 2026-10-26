"""Issue 0007: an entry read as something to review, not as a row to display.

Legs and a version are enough to show an entry but not to review one.  A reviewer arrives asking "what is this
now?", "what superseded it?" and "what did it supersede?", and ``GET /entries/{id}`` has to answer all three.

Three fields carry that, and each can go wrong quietly:

* ``state``: ``posted``, ``corrected`` or ``reversed``.  Derived, never stored, because a stored copy would be a
  second thing to keep true.  The two non-posted states are kept apart on purpose: a reviewer told "reversed"
  about a corrected entry goes looking for a restatement that is already there.
* ``corrections``: the chain, from whichever end this entry sits on.  The mirror's own page has to reach back to
  what it reversed, or a reader lands on a lone negative entry and concludes the books are broken.
* ``reversed_by``: also on the list payload.  Without it on the single read, a page showing one entry could not
  tell whether "reverse this" was still an available action.

There is no new route.  ``test_api_spine`` pins the served set.
"""

from __future__ import annotations

import uuid

REASON = "the April invoice was raised on the 1st; the receipt is a separate line"


def post(api, path: str, body: dict):
    return api.post(path, key=str(uuid.uuid4()), json=body)


def accrual(**overrides) -> dict:
    return {"dt": "2026-04-01", "narration": "Invoice 0041 — services, April", "reason": REASON,
            "legs": [{"account": "assets:receivables", "commodity": "USD", "amount": "7316.67"},
                     {"account": "income:sales", "commodity": "USD", "amount": "-7316.67"}], **overrides}


def write(api, **overrides) -> str:
    response = post(api, "/entries", accrual(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["entry"]["id"]


def read(api, entry_id: str) -> dict:
    return api.json(f"/entries/{entry_id}")


# State
def test_an_untouched_posted_entry_is_posted(api_small):
    assert read(api_small, write(api_small))["state"] == "posted"


def test_an_entry_that_was_only_reversed_says_reversed(api_small):
    """Withdrawn: the books stopped saying it, and nothing stands in its place."""
    entry_id = write(api_small)
    assert post(api_small, f"/entries/{entry_id}/reverse",
                {"reason": "the invoice was never issued"}).status_code == 201
    assert read(api_small, entry_id)["state"] == "reversed"


def test_an_entry_that_was_restated_says_corrected_and_not_reversed(api_small):
    """Corrected and reversed are different states.

    Both leave the original standing with a mirror beside it.  Only one of them also posts what the entry
    should have said, and a reviewer who cannot tell them apart goes looking for a restatement that either is
    already there or is never coming.
    """
    entry_id = write(api_small)
    response = post(api_small, f"/entries/{entry_id}/correct", {
        "reason": "the invoice was for 6,000.00, not 7,316.67",
        "replacement": {"dt": "2026-04-01", "narration": "Invoice 0041 — services, April",
                        "legs": [{"account": "assets:receivables", "commodity": "USD", "amount": "6000.00"},
                                 {"account": "income:sales", "commodity": "USD", "amount": "-6000.00"}]}})
    assert response.status_code == 201, response.text
    assert read(api_small, entry_id)["state"] == "corrected"


def test_a_mirror_is_not_itself_corrected_by_the_correction_it_belongs_to(api_small):
    """The state is about what happened to this entry, not about every correction that names it.

    A mirror appears in the chain as the ``mirror`` end.  Reading that as "this entry was corrected" would
    label every reversal in the books as itself having been reversed.  That bug would survive review because the
    word on screen is plausible for both.
    """
    entry_id = write(api_small)
    mirror_id = post(api_small, f"/entries/{entry_id}/reverse",
                     {"reason": "the invoice was never issued"}).json()["mirror_id"]
    assert read(api_small, mirror_id)["state"] == "posted"


# The chain, from both ends
def test_the_original_reaches_forward_to_its_mirror_and_replacement(api_small):
    entry_id = write(api_small)
    result = post(api_small, f"/entries/{entry_id}/correct", {
        "reason": "the invoice was for 6,000.00, not 7,316.67",
        "replacement": {"dt": "2026-04-01", "narration": "Invoice 0041 — services, April",
                        "legs": [{"account": "assets:receivables", "commodity": "USD", "amount": "6000.00"},
                                 {"account": "income:sales", "commodity": "USD", "amount": "-6000.00"}]}}).json()

    body = read(api_small, entry_id)
    assert body["reversed_by"] == result["mirror_id"]
    (chain,) = body["corrections"]
    assert chain["role"] == "original"
    assert chain["mirror_id"] == result["mirror_id"]
    assert chain["replacement_id"] == result["replacement_id"]
    assert chain["links"]["replacement"].endswith(f"/entries/{result['replacement_id']}")


def test_the_mirror_reaches_back_to_what_it_reversed(api_small):
    """The mirror's page reaches back to the entry it reverses.

    A reader who follows a register to a mirror sees a negative entry with no narration of its own beyond the
    original's.  Without this, nothing on the page says what it undoes, and the honest reading is that the books
    are wrong."""
    entry_id = write(api_small)
    mirror_id = post(api_small, f"/entries/{entry_id}/reverse",
                     {"reason": "the invoice was never issued"}).json()["mirror_id"]

    (chain,) = read(api_small, mirror_id)["corrections"]
    assert chain["role"] == "mirror"
    assert chain["original_id"] == entry_id
    assert chain["links"]["original"].endswith(f"/entries/{entry_id}")


def test_the_replacement_reaches_back_to_what_it_replaced(api_small):
    entry_id = write(api_small)
    result = post(api_small, f"/entries/{entry_id}/correct", {
        "reason": "the invoice was for 6,000.00, not 7,316.67",
        "replacement": {"dt": "2026-04-01", "narration": "Invoice 0041 — services, April",
                        "legs": [{"account": "assets:receivables", "commodity": "USD", "amount": "6000.00"},
                                 {"account": "income:sales", "commodity": "USD", "amount": "-6000.00"}]}}).json()

    (chain,) = read(api_small, result["replacement_id"])["corrections"]
    assert chain["role"] == "replacement"
    assert chain["original_id"] == entry_id


def test_the_chain_carries_the_reason_the_correction_was_made(api_small):
    """The reason is not a column on ``corrections``.  It is why somebody did something, so it lives in the
    append-only log under the correction's own id.  Reading it back is a join, and a chain rendered without it
    withholds the one part of a correction nobody can reconstruct from the numbers."""
    entry_id = write(api_small)
    post(api_small, f"/entries/{entry_id}/reverse", {"reason": "the invoice was never issued"})
    (chain,) = read(api_small, entry_id)["corrections"]
    assert chain["reason"] == "the invoice was never issued"
    assert chain["created_by"] and chain["created_at"]


def test_an_entry_in_no_correction_carries_an_empty_chain_rather_than_a_missing_field(api_small):
    """A page reads ``corrections`` unconditionally.  An absent key and an empty list look the same to a reader
    but not to ``.map``."""
    body = read(api_small, write(api_small))
    assert body["corrections"] == [] and body["reversed_by"] is None


# The history of an entry booked off a line
def test_an_entry_booked_from_a_statement_line_can_see_its_own_creation(api_small):
    """An entry booked from a statement line shows the line's events in its history.

    Nearly every entry in a real set of books arrives through a statement line, and ``book_line`` records
    ``line.booked`` under the line's id (subject kind ``statement_line``).  An event read that gathered only the
    entry's own id and its corrections would tell the entry page that nothing had been recorded, when its whole
    provenance sits in the log one id over.  The browser harness found exactly that.

    Reversing it is the second half: a correction is its own subject again, so a read that showed the booking
    and not the reversal would be the same bug in the other direction.
    """
    document = {"sha256": "b" * 64, "filename": "may.csv", "media_type": "text/csv", "size_bytes": 64,
                "blob_path": "/var/haddon/blobs/may.csv"}
    statement = post(api_small, "/statements", {
        "account": "assets:cash", "commodity": "USD", "period_start": "2026-05-01", "period_end": "2026-05-31",
        "opening": "0", "closing": "-120.00",
        "lines": [{"dt": "2026-05-04", "description": "a fictional supplier", "amount": "-120.00",
                   "currency": "USD"}],
        "document": document, "locator": "the closing balance on page 1", "page": 1,
        "reason": "the May statement, as downloaded"})
    assert statement.status_code == 201, statement.text
    statement_id = statement.json()["statement"]["statement_id"]

    line_id = api_small.json(f"/statements/{statement_id}/lines")["items"][0]["line_id"]
    booked = post(api_small, f"/lines/{line_id}/book", {
        "legs": [{"account": "expenses:office", "commodity": "USD", "amount": "120.00"}],
        "narration": "the supplier's May invoice", "reason": "office supplies bought on the card"})
    assert booked.status_code == 201, booked.text
    entry_id = booked.json()["line"]["entries"][0]["id"]

    kinds = [e["kind"] for e in api_small.json(f"/entries/{entry_id}/events")["items"]]
    assert "line.booked" in kinds, kinds

    assert post(api_small, f"/entries/{entry_id}/reverse",
                {"reason": "the invoice belonged to the next month"}).status_code == 201
    kinds = [e["kind"] for e in api_small.json(f"/entries/{entry_id}/events")["items"]]
    assert kinds.count("line.booked") == 1 and "entry.reversed" in kinds, kinds
