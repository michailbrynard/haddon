""""Booked, awaiting a document" is a state the books can hold, and answer questions about
(docs/design/decisions.md, "Booked, awaiting a document").

Lines are sometimes booked provisionally, pending a document that has not arrived, and that matters: when a
missing invoice turns up, a booking at its gross amount can split into a smaller net cost plus recoverable tax.
If the store does not record that the line is waiting, the list of open items lives in somebody's notes, and if
nobody remembers, the line stays wrong.

The three claims worth testing, in order of how badly each would hurt to get wrong:

* It clears by doing the real thing.  Filing the awaited document settles the expectation itself and names the
  evidence link that settled it.  There is no separate "mark it done", because a tick box is one more thing to
  remember and forgetting is the failure being prevented.
* Withdrawing is a different ending from a document arriving, and the record keeps them apart: a receipt nobody
  will reissue is not an invoice that turned up.
* The entry does not move.  This is a note beside an immutable posted entry, so its version, its postings and
  every report are identical before and after.  That is also why a closed period does not refuse it.
"""

from __future__ import annotations

import uuid

import pytest

from repo_helpers import load_fixture, lock_books
from test_api_documents import document, file_against, register, write_entry

AWAITING = {"awaiting": "the supplier's invoice for this desk rental",
            "reason": "booked gross from the bank line meanwhile, so the month is not understated"}


def expect(api, entry_id: str, *, key: str | None = None, **overrides):
    return api.post(f"/entries/{entry_id}/awaiting-document", key=key or str(uuid.uuid4()),
                    json={**AWAITING, **overrides})


def withdraw(api, entry_id: str, *, reason: str = "the supplier issues nothing further", key: str | None = None):
    return api.post(f"/entries/{entry_id}/awaiting-document/withdraw", key=key or str(uuid.uuid4()),
                    json={"reason": reason})


def provisional_ids(api, query: str = "") -> list[str]:
    return [item["entry"]["id"] for item in api.json(f"/provisional{query}")["items"]]


@pytest.fixture
def entry_id(api_small) -> str:
    return write_entry(api_small)


# Recording the state
def test_a_provisional_booking_is_recorded_and_is_answerable_as_a_query(api_small, entry_id):
    response = expect(api_small, entry_id)
    assert response.status_code == 201, response.text
    record = response.json()["expectation"]
    assert record["entry_id"] == entry_id and record["state"] == "awaiting"
    assert record["awaiting"] == AWAITING["awaiting"] and record["reason"] == AWAITING["reason"]
    assert record["supersedes_id"] is None and record["evidence_id"] is None

    listed = api_small.json("/provisional")["items"]
    assert [i["expectation"]["id"] for i in listed] == [record["id"]]
    # The entry is beside it, whole, so the list is readable without a second call per row.
    assert listed[0]["entry"]["id"] == entry_id
    assert listed[0]["entry"]["narration"] == "Desk rental for May"
    assert len(listed[0]["entry"]["postings"]) == 2
    assert listed[0]["links"]["documents"].endswith(f"/entries/{entry_id}/documents")


def test_the_booking_itself_does_not_move(api_small, entry_id):
    """It is a note beside a posted entry, not a change to it: the entry is immutable and stays so."""
    before = api_small.json(f"/entries/{entry_id}")
    balances = api_small.json("/reports/trial-balance?year=2026")["lines"]
    expect(api_small, entry_id)
    after = api_small.json(f"/entries/{entry_id}")
    assert (after["entry"], after["version"]) == (before["entry"], before["version"])
    assert api_small.json("/reports/trial-balance?year=2026")["lines"] == balances
    assert api_small.json(f"/entries/{entry_id}")["entry"]["status"] == "posted"


def test_it_is_recorded_as_an_audit_action_with_a_reason_and_an_event(api_small, entry_id):
    """The audit action carries actor and reason; there is no proposal/decision pair (issue 0005)."""
    from conftest import API_ACTOR

    record = expect(api_small, entry_id).json()["expectation"]
    assert record["created_by"] == API_ACTOR
    assert "decision_id" not in record
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='entry.awaiting_document' AND subject_id=?",
                            (entry_id,)) == 1
    assert api_small.scalar("SELECT created_by FROM events WHERE kind='entry.awaiting_document' AND subject_id=?",
                            (entry_id,)) == API_ACTOR
    assert api_small.scalar("SELECT reason FROM events WHERE kind='entry.awaiting_document' AND subject_id=?",
                            (entry_id,)) == record["reason"]
    # It reaches the entry's own history, which is where somebody looking at the entry will find it.
    kinds = [e["kind"] for e in api_small.json(f"/entries/{entry_id}/events")["items"]]
    assert "entry.awaiting_document" in kinds


def test_restating_what_is_awaited_supersedes_rather_than_duplicating(api_small, entry_id):
    """An expectation that gets more specific is a revision, and both readings stay in the record."""
    first = expect(api_small, entry_id).json()["expectation"]
    second = expect(api_small, entry_id, awaiting="a full invoice carrying our tax number",
                    reason="the simplified receipt will not support a claim").json()["expectation"]
    assert second["supersedes_id"] == first["id"]
    assert provisional_ids(api_small) == [entry_id]   # one live record, not two
    assert api_small.scalar("SELECT count(*) FROM document_expectations WHERE entry_id=?", (entry_id,)) == 2
    history = api_small.json("/provisional")["items"][0]
    assert history["expectation"]["awaiting"] == "a full invoice carrying our tax number"


def test_an_expectation_must_say_what_it_is_waiting_for(api_small, entry_id):
    """"This is provisional" without naming what would settle it is a note to nobody."""
    for field in ("awaiting", "reason"):
        response = expect(api_small, entry_id, **{field: ""})
        assert response.status_code == 422
        assert any(field in d.get("path", "") for d in response.json()["error"]["details"]), response.text
    assert api_small.scalar("SELECT count(*) FROM document_expectations") == 0


def test_an_entry_this_entity_does_not_hold_is_404(api_small):
    assert expect(api_small, "e-not-real").status_code == 404
    assert withdraw(api_small, "e-not-real").status_code == 404


# How it clears
def test_filing_the_awaited_document_is_what_settles_it(api_small, entry_id):
    """Resolving the state and doing the work are one act."""
    record = expect(api_small, entry_id).json()["expectation"]
    doc = register(api_small, document("invoice-24267")).json()["document"]
    filing = file_against(api_small, entry_id, doc["id"]).json()["filing"]

    assert provisional_ids(api_small) == []            # it has left the list
    assert filing["met_expectation_id"] is not None    # ... and the filing says so
    met = api_small.scalar("SELECT state FROM document_expectations WHERE id=?", (filing["met_expectation_id"],))
    assert met == "met"
    # The record names the evidence link that met it, so "what settled this?" reaches the document.
    evidence_id = api_small.scalar("SELECT evidence_id FROM document_expectations WHERE id=?",
                                   (filing["met_expectation_id"],))
    assert evidence_id == filing["id"]
    assert api_small.scalar("SELECT supersedes_id FROM document_expectations WHERE id=?",
                            (filing["met_expectation_id"],)) == record["id"]
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='entry.document_arrived' AND subject_id=?",
                            (entry_id,)) == 1


def test_filing_against_a_booking_nobody_called_provisional_settles_nothing(api_small, entry_id):
    """The ordinary case, and the one an over-eager resolution would get wrong."""
    doc = register(api_small, document("invoice-24267")).json()["document"]
    filing = file_against(api_small, entry_id, doc["id"]).json()["filing"]
    assert filing["met_expectation_id"] is None
    assert api_small.scalar("SELECT count(*) FROM document_expectations") == 0


def test_a_second_document_can_be_awaited_after_the_first_arrived(api_small, entry_id):
    expect(api_small, entry_id)
    doc = register(api_small, document("invoice-a")).json()["document"]
    file_against(api_small, entry_id, doc["id"])
    assert provisional_ids(api_small) == []
    assert expect(api_small, entry_id, awaiting="the credit note the supplier promised").status_code == 201
    assert provisional_ids(api_small) == [entry_id]


def test_withdrawing_is_a_different_ending_and_is_recorded_as_one(api_small, entry_id):
    """Sometimes no better document exists.  That is not a document arriving and must not read as one."""
    expect(api_small, entry_id)
    response = withdraw(api_small, entry_id, reason="the receipt is simplified and will not be reissued")
    assert response.status_code == 201, response.text
    record = response.json()["expectation"]
    assert record["state"] == "withdrawn" and record["evidence_id"] is None
    assert record["reason"] == "the receipt is simplified and will not be reissued"
    assert provisional_ids(api_small) == []
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='entry.document_not_expected' "
                            "AND subject_id=?", (entry_id,)) == 1
    # The whole chain is readable, so why it stopped being provisional survives.
    assert [e["state"] for e in response.json()["history"]] == ["awaiting", "withdrawn"]


def test_the_same_document_can_be_awaited_again_after_a_withdrawal(api_small, entry_id):
    """Word for word the same expectation, reopened after a withdrawal, is a new record.

    Same entry, same words, same reason: it must not be refused as a duplicate of the first one.
    """
    expect(api_small, entry_id)
    assert withdraw(api_small, entry_id).status_code == 201
    again = expect(api_small, entry_id)
    assert again.status_code == 201, again.text
    assert provisional_ids(api_small) == [entry_id]
    assert [r["state"] for r in again.json()["history"]] == ["awaiting", "withdrawn", "awaiting"]


def test_withdrawing_when_nothing_is_awaited_is_refused(api_small, entry_id):
    response = withdraw(api_small, entry_id)
    assert response.status_code == 409
    assert response.json()["error"]["details"][0]["reason"] == "nothing_awaited"


# The query
def test_the_list_spans_months_and_can_be_narrowed_to_one(api_small):
    """The month review has to be able to say "and two of these are older than this month" without guessing."""
    april = write_entry(api_small, dt="2026-04-24", amount="312400000")
    may = write_entry(api_small, dt="2026-05-28", amount="205000000")
    expect(api_small, april, awaiting="the supplier's full invoice")
    expect(api_small, may, awaiting="the desk rental invoice")
    assert set(provisional_ids(api_small)) == {april, may}
    assert provisional_ids(api_small, "?from=2026-05-01&to=2026-05-31") == [may]
    assert provisional_ids(api_small, "?from=2026-04-01&to=2026-04-30") == [april]
    assert provisional_ids(api_small, "?from=2026-01-01&to=2026-12-31") == provisional_ids(api_small)
    assert provisional_ids(api_small, "?from=2025-01-01&to=2025-12-31") == []


def test_the_list_is_empty_when_nothing_is_provisional(api_small, entry_id):
    body = api_small.json("/provisional")
    assert body["items"] == [] and body["next_cursor"] is None
    assert body["entity_id"] == api_small.entity_id


def test_a_closed_month_does_not_stop_a_booking_being_called_provisional(api_small):
    """The month somebody realises a booking was provisional is often not the month the booking is in.

    A state that could not be recorded after a close would be missing exactly when it matters."""
    entry = write_entry(api_small, dt="2026-05-28")
    engine = api_small.engine()
    try:
        lock_books(load_fixture(engine), "2026-05-31")
    finally:
        engine.dispose()
    assert api_small.scalar("SELECT through FROM period_locks") == "2026-05-31"

    balances = api_small.json("/reports/trial-balance?year=2026")["lines"]
    assert expect(api_small, entry).status_code == 201
    assert provisional_ids(api_small) == [entry]
    assert api_small.json("/reports/trial-balance?year=2026")["lines"] == balances

    # It still clears the real way, with the document filed into the closed month (migration 0004).
    doc = register(api_small, document("invoice-24267")).json()["document"]
    assert file_against(api_small, entry, doc["id"]).json()["filing"]["met_expectation_id"] is not None
    assert provisional_ids(api_small) == []


def test_the_state_is_never_edited_in_place(api_small, entry_id):
    """Every transition appends; nothing is updated.  A judgement that was revised has to stay legible."""
    expect(api_small, entry_id)
    expect(api_small, entry_id, awaiting="a full invoice")
    history = withdraw(api_small, entry_id).json()["history"]
    assert [r["state"] for r in history] == ["awaiting", "awaiting", "withdrawn"]
    assert [r["awaiting"] for r in history[1:]] == ["a full invoice", "a full invoice"]
    assert api_small.scalar("SELECT count(*) FROM document_expectations") == 3
    con = api_small.raw()
    try:
        with pytest.raises(Exception, match="immutable"):
            con.execute("UPDATE document_expectations SET state='met' WHERE state='withdrawn'")
    finally:
        con.close()


def test_an_expectation_replays_under_the_same_key(api_small, entry_id):
    first = expect(api_small, entry_id, key="dx-1")
    again = expect(api_small, entry_id, key="dx-1")
    assert first.status_code == again.status_code == 201
    assert again.headers["Idempotency-Replayed"] == "true"
    assert api_small.scalar("SELECT count(*) FROM document_expectations") == 1
