"""A document can enter the ledger without being a bank statement, and can be filed against an entry
(docs/design/decisions.md, "Documents that are not bank statements").

Without this, invoices that decide how a cost is booked survive only as narration text.  The accounting rule is
that treatment follows the document: a cost with a full invoice is booked net of recoverable tax, the same cost
without one is booked gross.  A ledger whose rule is about documents has to be able to hold them.

What the tests below are mostly about:

* The same bytes are the same document.  Registration is content-addressed, so registering twice is not an
  error.  It returns the row that is already there and writes nothing.  The refusal is reserved for a claim
  that cannot be true: the same hash with a different length.
* Filing reuses the evidence chain.  A filing is an ``evidence_links`` row, which is what ``GET /provenance``
  reads, so the document is reachable from the entry without new machinery.
* A closed month does not refuse a document (migration 0004), while a closed month's balance assertion still
  does.  The latter is proved in ``tests/test_integrity_mutation_matrix.py``, where the trigger's whole refusal
  surface lives.
"""

from __future__ import annotations

import uuid

import pytest

from haddon.domain.canonical import sha256_hex
from haddon.domain.micros import Commodity, Micros

from repo_helpers import load_fixture, lock_books

MEDIA = "application/pdf"


def document(tag: str, **overrides) -> dict:
    """A document submission for a fictional supplier invoice.  No real company, no real format."""
    return {"sha256": sha256_hex(tag), "filename": f"{tag}.pdf", "media_type": MEDIA, "size_bytes": 18_432,
            "blob_path": f"/var/haddon/blobs/{tag}.pdf",
            "reason": "the supplier's invoice for this cost, filed with the books", **overrides}


def register(api, body: dict, *, key: str | None = None):
    return api.post("/documents", key=key or str(uuid.uuid4()), json=body)


def file_against(api, entry_id: str, document_id: str, *, key: str | None = None, **overrides):
    body = {"document_id": document_id, "locator": "page 1, the total box",
            "caption": "supplier invoice 24267 for the May desk rental",
            "reason": "this is the document the booking follows", **overrides}
    return api.post(f"/entries/{entry_id}/documents", key=key or str(uuid.uuid4()), json=body)


def accounts_of(api) -> dict[str, str]:
    return {a["path"]: a["id"] for a in api.json("/accounts?limit=200")["items"]}


def write_entry(api, *, dt: str = "2026-05-28", amount: str = "169420000") -> str:
    """A two-leg entry through the ordinary journal route, so the thing a document is filed against is real."""
    decimal = Micros.parse(amount).to_decimal_string(Commodity("USD", 2))
    body = {"dt": dt, "narration": "Desk rental for May", "reason": "an ordinary cost, booked",
            "legs": [{"account": "expenses:office", "commodity": "USD", "amount": decimal},
                     {"account": "assets:cash", "commodity": "USD", "amount": f"-{decimal}"}]}
    response = api.post("/entries", key=str(uuid.uuid4()), json=body)
    assert response.status_code == 201, response.text
    return response.json()["entry"]["id"]


@pytest.fixture
def entry_id(api_small) -> str:
    return write_entry(api_small)


# Registering a document
def test_a_document_is_registered_and_comes_back_whole(api_small):
    response = register(api_small, document("invoice-24267"))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    doc = body["document"]
    assert doc["sha256"] == sha256_hex("invoice-24267")
    assert doc["filename"] == "invoice-24267.pdf" and doc["media_type"] == MEDIA
    # Issue 0007: the path the bytes were registered under is recorded but not published.  A reader gets
    # whether the bytes can be opened here, and the address to open them at.
    assert doc["size_bytes"] == 18_432 and "blob_path" not in doc
    assert doc["storage"] == "stored" and doc["external_ref"] is None
    assert doc["links"]["content"].endswith(f"/documents/{doc['id']}/content")
    assert api_small.scalar("SELECT blob_path FROM documents WHERE id=?",
                            (doc["id"],)) == "/var/haddon/blobs/invoice-24267.pdf"
    assert doc["filing_count"] == 0 and body["filings"] == []
    assert doc["links"]["self"].endswith(f"/documents/{doc['id']}")
    # It is in the store the next reader sees, not only in the reply.
    assert api_small.scalar("SELECT count(*) FROM documents WHERE id=?", (doc["id"],)) == 1
    assert api_small.json(doc["links"]["self"])["document"]["id"] == doc["id"]


def test_registering_records_who_did_it_and_why(api_small):
    """A file appearing in the books with no reason is a file nobody can later account for."""
    from conftest import API_ACTOR

    body = register(api_small, document("contract-a")).json()
    doc = body["document"]
    assert doc["created_by"] == API_ACTOR and doc["created_at"]
    # Issue 0005: the reason lives on the audit action, the only record of authority.
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='document.registered' AND subject_id=?",
                            (doc["id"],)) == 1
    assert api_small.scalar("SELECT reason FROM events WHERE kind='document.registered' AND subject_id=?",
                            (doc["id"],)) == "the supplier's invoice for this cost, filed with the books"
    assert api_small.scalar("SELECT created_by FROM events WHERE kind='document.registered' AND subject_id=?",
                            (doc["id"],)) == API_ACTOR


def test_the_same_bytes_twice_are_the_same_document(api_small):
    """Two entries resting on one invoice register it twice in the ordinary course of events.  A refusal would
    only teach callers to look first."""
    first = register(api_small, document("invoice-24267"))
    second = register(api_small, document("invoice-24267", filename="renamed-copy.pdf"))
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    # The stored row keeps the name it was first registered under, and no second row appeared.
    assert second.json()["document"]["filename"] == "invoice-24267.pdf"
    assert api_small.scalar("SELECT count(*) FROM documents") == 1
    # No second audit action either: nothing happened, so nothing is recorded.
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='document.registered'") == 1


def test_the_same_hash_with_a_different_length_is_refused(api_small):
    """Identical bytes cannot be a different number of bytes long, so one of the two claims is wrong and the
    ledger says so instead of keeping the first and ignoring the second."""
    register(api_small, document("invoice-24267"))
    response = register(api_small, document("invoice-24267", size_bytes=99))
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "immutable"
    assert error["details"][0]["registered_size_bytes"] == 18_432
    assert error["details"][0]["submitted_size_bytes"] == 99


def test_two_different_documents_are_two_rows(api_small):
    a = register(api_small, document("invoice-a")).json()["document"]
    b = register(api_small, document("invoice-b")).json()["document"]
    assert a["id"] != b["id"] and a["sha256"] != b["sha256"]
    assert api_small.scalar("SELECT count(*) FROM documents") == 2


@pytest.mark.parametrize("body,path", [
    (document("x", sha256="not-a-hash"), "sha256"),
    (document("x", filename=""), "filename"),
    (document("x", reason=""), "reason"),
    (document("x", blob_path=""), "blob_path"),
])
def test_a_malformed_registration_is_422_naming_the_field(api_small, body, path):
    response = register(api_small, body)
    assert response.status_code == 422
    assert any(path in d.get("path", "") for d in response.json()["error"]["details"]), response.text
    assert api_small.scalar("SELECT count(*) FROM documents") == 0


def test_size_bytes_as_a_json_number_is_the_one_number_allowed(api_small):
    """The amount contract refuses JSON numbers on ``*_micros`` keys; a byte count is not an amount and stays a
    plain integer, which is what ``documents.size_bytes`` has always been."""
    assert register(api_small, document("sized", size_bytes=0)).status_code == 201
    assert api_small.scalar("SELECT size_bytes FROM documents") == 0


# Filing it against an entry
def test_a_document_filed_against_an_entry_is_reachable_from_it(api_small, entry_id):
    """From the entry, through provenance, to the file, using the existing evidence chain."""
    doc = register(api_small, document("invoice-24267")).json()["document"]
    response = file_against(api_small, entry_id, doc["id"], page=2)
    assert response.status_code == 201, response.text
    filing = response.json()["filing"]
    assert filing["entry_id"] == entry_id and filing["document_id"] == doc["id"]
    assert filing["page"] == 2 and filing["locator"] == "page 1, the total box"
    assert filing["caption"] == "supplier invoice 24267 for the May desk rental"
    assert filing["document"]["sha256"] == doc["sha256"]

    provenance = api_small.json(f"/provenance?entry_id={entry_id}")
    assert [e["evidence_id"] for e in provenance["evidence"]] == [filing["id"]]
    assert provenance["evidence"][0]["document"]["filename"] == "invoice-24267.pdf"
    assert provenance["evidence"][0]["page"] == 2


def test_filing_is_recorded_as_an_audit_action_with_a_reason(api_small, entry_id):
    """One filing, one audit action, carrying the reason the caller gave.  Nothing else records it."""
    doc = register(api_small, document("invoice-24267")).json()["document"]
    filing = file_against(api_small, entry_id, doc["id"]).json()["filing"]
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='document.filed' AND subject_id=?",
                            (entry_id,)) == 1
    assert api_small.scalar("SELECT reason FROM events WHERE kind='document.filed' AND subject_id=?",
                            (entry_id,)) == "this is the document the booking follows"
    assert "proposal_id" not in filing and "decision_id" not in filing


def test_the_document_knows_what_cites_it(api_small, entry_id):
    doc = register(api_small, document("invoice-24267")).json()["document"]
    second = write_entry(api_small, dt="2026-06-01", amount="35580000")
    file_against(api_small, entry_id, doc["id"])
    file_against(api_small, second, doc["id"])
    body = api_small.json(f"/documents/{doc['id']}")
    assert body["document"]["filing_count"] == 2
    assert {f["subject_id"] for f in body["filings"]} == {entry_id, second}
    assert {f["subject_kind"] for f in body["filings"]} == {"entry"}
    assert body["created"] is False


def test_the_same_document_twice_in_the_same_place_is_refused(api_small, entry_id):
    doc = register(api_small, document("invoice-24267")).json()["document"]
    assert file_against(api_small, entry_id, doc["id"], page=1).status_code == 201
    response = file_against(api_small, entry_id, doc["id"], page=1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "immutable"
    assert api_small.scalar("SELECT count(*) FROM evidence_links WHERE entry_id=?", (entry_id,)) == 1


def test_the_same_document_at_a_different_place_is_a_second_citation(api_small, entry_id):
    """One invoice can say two things about one entry: the cost on page 1, the tax on page 2."""
    doc = register(api_small, document("invoice-24267")).json()["document"]
    assert file_against(api_small, entry_id, doc["id"], page=1).status_code == 201
    assert file_against(api_small, entry_id, doc["id"], page=2).status_code == 201
    assert file_against(api_small, entry_id, doc["id"], page=1,
                        locator="the tax summary line").status_code == 201
    assert api_small.scalar("SELECT count(*) FROM evidence_links WHERE entry_id=?", (entry_id,)) == 3


def test_filing_against_an_entry_or_a_document_that_is_not_here_is_404(api_small, entry_id):
    doc = register(api_small, document("invoice-24267")).json()["document"]
    assert file_against(api_small, "e-not-real", doc["id"]).status_code == 404
    assert file_against(api_small, entry_id, "sf-not-real").status_code == 404
    assert api_small.get("/documents/sf-not-real").status_code == 404
    assert api_small.scalar("SELECT count(*) FROM evidence_links") == 0


@pytest.mark.parametrize("field", ["locator", "caption", "reason"])
def test_filing_without_saying_what_or_why_is_refused(api_small, entry_id, field):
    """``locator`` is where in the document, ``caption`` is what it is, ``reason`` is why it belongs here.  An
    attachment with none of the three is a file somebody dropped next to an entry."""
    doc = register(api_small, document("invoice-24267")).json()["document"]
    response = file_against(api_small, entry_id, doc["id"], **{field: ""})
    assert response.status_code == 422
    assert any(field in d.get("path", "") for d in response.json()["error"]["details"]), response.text


# The closed-period decision
def test_a_late_document_can_be_filed_against_a_closed_month(api_small):
    """A supplier invoice arriving in June for a May cost is an ordinary event.

    If filing it meant reopening the month (a close record, a reason and a re-close) for something that changes
    no figure the month reports, in practice it would be filed nowhere.  Migration 0004 allows it.
    """
    entry = write_entry(api_small, dt="2026-05-28")
    doc = register(api_small, document("invoice-24267")).json()["document"]
    engine = api_small.engine()
    try:
        fx = load_fixture(engine)
        lock_books(fx, "2026-05-31")
    finally:
        engine.dispose()
    assert api_small.scalar("SELECT through FROM period_locks") == "2026-05-31"

    before = api_small.json(f"/entries/{entry}")
    balances_before = api_small.json("/reports/trial-balance?year=2026")["lines"]

    response = file_against(api_small, entry, doc["id"])
    assert response.status_code == 201, response.text
    assert api_small.json(f"/provenance?entry_id={entry}")["evidence"][0]["document_id"] == doc["id"]
    # Nothing the closed month reports moved, which is the argument for allowing it.  The event log does move,
    # since filing is an act and is recorded as one, so the comparison is of the entry, not the reply.
    after = api_small.json(f"/entries/{entry}")
    assert (after["entry"], after["version"]) == (before["entry"], before["version"])
    assert api_small.json("/reports/trial-balance?year=2026")["lines"] == balances_before


def test_a_closed_month_still_refuses_a_write_that_changes_a_figure(api_small):
    """The positive control for the test above: the lock is still a lock.

    Filing a document is allowed under it because an evidence link has no amount and no date.  Writing an entry
    into the closed month is refused, so 0004 relaxed the documentary case and not the period lock.
    """
    write_entry(api_small, dt="2026-05-10")
    engine = api_small.engine()
    try:
        lock_books(load_fixture(engine), "2026-05-31")
    finally:
        engine.dispose()
    response = api_small.post("/entries", key=str(uuid.uuid4()), json={
        "dt": "2026-05-20", "narration": "A cost inside the closed month", "reason": "should not be possible",
        "legs": [{"account": "expenses:office", "commodity": "USD", "amount": "1.00"},
                 {"account": "assets:cash", "commodity": "USD", "amount": "-1.00"}]})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "period_locked"


# Reading them back
def test_documents_list_and_can_be_asked_for_by_hash(api_small):
    """"Do we already hold this file?" is the question an agent asks before re-reading a directory."""
    a = register(api_small, document("invoice-a")).json()["document"]
    register(api_small, document("invoice-b"))
    listing = api_small.json("/documents")
    assert len(listing["items"]) == 2
    by_hash = api_small.json(f"/documents?sha256={a['sha256']}")
    assert [d["id"] for d in by_hash["items"]] == [a["id"]]
    assert api_small.json(f"/documents?sha256={sha256_hex('never-seen')}")["items"] == []


def test_the_statement_loader_and_this_door_share_one_store(api_small, entry_id):
    """Not a second attachment table: a statement's own document is listed here beside the invoices.

    The filings read shows what each is doing: the statement's links, and the invoice's entry.
    """
    from statement_helpers import CASH, load

    engine = api_small.engine()
    try:
        statement = load(load_fixture(engine), account=CASH, start="2026-03-01", end="2026-03-31",
                         opening="0", closing="0", rows=(), tag="mar-usd")
    finally:
        engine.dispose()
    invoice = register(api_small, document("invoice-24267")).json()["document"]
    file_against(api_small, entry_id, invoice["id"])

    listed = {d["id"]: d for d in api_small.json("/documents")["items"]}
    assert set(listed) == {statement.document_id, invoice["id"]}
    statement_filings = api_small.json(f"/documents/{statement.document_id}")["filings"]
    assert {f["subject_kind"] for f in statement_filings} == {"statement"}
    assert [f["subject_kind"] for f in api_small.json(f"/documents/{invoice['id']}")["filings"]] == ["entry"]


def test_a_registration_replays_under_the_same_key(api_small):
    """The same intent key with the same body returns the first answer, not a second row."""
    first = register(api_small, document("invoice-24267"), key="doc-1")
    again = register(api_small, document("invoice-24267"), key="doc-1")
    assert first.status_code == again.status_code == 201
    assert again.headers["Idempotency-Replayed"] == "true"
    assert again.json()["document"]["id"] == first.json()["document"]["id"]
    assert api_small.scalar("SELECT count(*) FROM documents") == 1
