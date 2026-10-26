"""Nothing a published command can reach is a place the published commands cannot leave (0006 review).

Phase 4's gate is that an agent can maintain a normal period using the published commands alone, including
recovery from validation errors.  That is a property of the command surface as a graph: a surface where one
successful call strands the entity fails it however well each command behaves on its own.

``archive_account`` is the case that motivated it.  Archiving an account that still carries a balance, a posted
entry or an unbooked statement line is accepted, and afterwards the entry cannot be reversed or corrected, the
line cannot be booked, and the close never becomes eligible: each refuses ``account_archived``.  Without
``restore_account`` the only exit would be editing the database by hand.

So the tests are of two kinds:

* :func:`test_no_published_write_has_a_state_it_cannot_leave` is the invariant, stated over the derived command
  surface.  It requires every published write to be either additive or paired with a published inverse, so a
  future command with the same shape fails here before anybody drives it.
* The rest are the repro, driven over HTTP end to end: archive a load-bearing account, confirm each operation
  is refused, then recover using nothing but published calls.
"""

from __future__ import annotations

import uuid

import pytest

from haddon.api import surface
from haddon.api.app import create_app
from haddon.domain.canonical import sha256_hex

CASH = "assets:cash"
OFFICE = "expenses:office"


@pytest.fixture(scope="module")
def app():
    return create_app()


def key() -> str:
    return uuid.uuid4().hex


def cash_version(a) -> int:
    """The cash account's version, read the way a caller reads it.

    ``GET /accounts/{id}`` takes an id while the writes also take a path, so the list turns one into the other."""
    return next(row["version"] for row in a.json("/accounts")["items"] if row["path"] == CASH)


# The invariant, over the derived surface
#: Every published write that moves an entity between two states, and the published command that moves it back.
#: This is the pairing, not the check.  The check is that the surface contains no unpaired one.
REVERSIBLE = {
    "close_period": "reopen_period",
    "reopen_period": "close_period",
    "archive_account": "restore_account",
    "restore_account": "archive_account",
    # a posted entry is withdrawn or restated; both leave a mirror that is itself further correctable
    "reverse_entry": "correct_entry",
    "correct_entry": "reverse_entry",
    # a statement loaded wrongly is withdrawn by loading its replacement; what was said about a line is
    # superseded by saying something else about it
    "load_statement": "replace_statement",
    "replace_statement": "replace_statement",
    "book_line": "reexplain_line",
    "link_line": "reexplain_line",
    "ignore_line": "reexplain_line",
    "reexplain_line": "reexplain_line",
    "await_document": "withdraw_expectation",
    "withdraw_expectation": "await_document",
    "record_unrecovered_tax": "resolve_unrecovered_tax",
    "resolve_unrecovered_tax": "record_unrecovered_tax",
}

#: Writes that add a row and change no state anything else reads as a gate.  Nothing is stranded by one, but
#: they are named rather than defaulted, so a new command is an explicit decision here instead of silently
#: falling through the check.
#: ``post_batch`` writes a set of ordinary posted entries, each reversible and correctable on its own.  A batch is
#: a unit of submission, not a unit the books afterwards hold as one thing.
#: ``run_export`` changes nothing in the books.  It reads a range, renders it and writes an immutable record that
#: it did, so there is no state for a later command to be refused by; re-running it replaces the export.
ADDITIVE = {"create_account", "post_entry", "post_batch", "register_document", "file_document", "run_export"}


def test_no_published_write_has_a_state_it_cannot_leave(app):
    """Every published command is either additive or has a published inverse.  No third kind.

    The 0006 review found a third kind: ``archive_account`` changed a flag that four other commands read as a
    refusal, and nothing changed it back.
    """
    _, writes = surface.published(app)
    published = {w.command for w in writes}
    unaccounted = published - set(REVERSIBLE) - ADDITIVE
    assert not unaccounted, (
        f"{sorted(unaccounted)} is published but is neither additive nor paired with an inverse. If it strands "
        f"the entity in a state no published command leaves, it fails Phase 4's gate; if it does not, say so by "
        f"naming it in ADDITIVE.")
    for command in sorted(published & set(REVERSIBLE)):
        inverse = REVERSIBLE[command]
        assert inverse in published, f"{command} is published but its inverse {inverse} is not"


# The repro, over HTTP end to end
@pytest.fixture
def load_bearing(api_small):
    """An entity whose cash account carries a balance, a posted entry and a statement with an unbooked line."""
    a = api_small
    entry = a.post("/entries", key=key(), json={
        "dt": "2026-03-05", "narration": "March rent", "reason": "the March rent went out on the 5th",
        "legs": [{"account": CASH, "commodity": "USD", "amount": "-100.00"},
                 {"account": OFFICE, "commodity": "USD", "amount": "100.00"}]})
    assert entry.status_code == 201, entry.text[:400]
    entry_id = entry.json()["entry"]["id"]

    statement = a.post("/statements", key=key(), json={
        "account": CASH, "commodity": "USD", "period_start": "2026-03-01", "period_end": "2026-03-31",
        "opening": "0", "closing": "-150.00", "reason": "the March statement arrived",
        "locator": "page 1, 'Closing balance'", "page": 1,
        "document": {"sha256": sha256_hex("recoverable-2026-03"), "filename": "statement-2026-03.csv",
                     "media_type": "text/csv", "size_bytes": 2048,
                     "blob_path": "/var/haddon/blobs/statement-2026-03.csv"},
        "lines": [{"dt": "2026-03-05", "description": "March rent", "amount": "-100.00", "currency": "USD"},
                  {"dt": "2026-03-09", "description": "account fee", "amount": "-50.00", "currency": "USD"}]})
    assert statement.status_code == 201, statement.text[:400]
    statement_id = statement.json()["statement"]["statement_id"]
    served = a.json(f"/statements/{statement_id}/lines")["items"]
    unbooked = next(ln for ln in served if ln["description"] == "account fee")
    return {"entry_id": entry_id, "unbooked_line": unbooked["line_id"],
            "account_version": cash_version(a)}


def _archive(a, version):
    return a.post(f"/accounts/{CASH}/archive", key=key(),
                  json={"reason": "the bank account was closed", "expected_version": version})


def test_archiving_a_load_bearing_account_is_still_accepted(api_small, load_bearing):
    """Archiving a load-bearing account is accepted.  Refusing it would not establish the invariant, because a
    zero-balance account's posted entries still could never be reversed (see ``decisions.md``)."""
    assert _archive(api_small, load_bearing["account_version"]).status_code == 201


def test_an_archived_account_still_refuses_every_new_booking(api_small, load_bearing):
    """What archiving means is not relaxed: it still refuses every new booking."""
    a = api_small
    assert _archive(a, load_bearing["account_version"]).status_code == 201
    entry_id, line_id = load_bearing["entry_id"], load_bearing["unbooked_line"]

    for label, response in [
        ("reverse", a.post(f"/entries/{entry_id}/reverse", key=key(), json={"reason": "miscoded"})),
        ("correct", a.post(f"/entries/{entry_id}/correct", key=key(), json={
            "reason": "the amount was wrong",
            "replacement": {"dt": "2026-03-05", "narration": "March rent",
                            "legs": [{"account": CASH, "commodity": "USD", "amount": "-90.00"},
                                     {"account": OFFICE, "commodity": "USD", "amount": "90.00"}]}})),
        ("book", a.post(f"/lines/{line_id}/book", key=key(), json={
            "narration": "account fee", "reason": "the fee is ours",
            "legs": [{"account": OFFICE, "commodity": "USD", "amount": "50.00"}]})),
        ("post", a.post("/entries", key=key(), json={
            "dt": "2026-03-10", "narration": "new", "reason": "new work",
            "legs": [{"account": CASH, "commodity": "USD", "amount": "-1.00"},
                     {"account": OFFICE, "commodity": "USD", "amount": "1.00"}]})),
    ]:
        assert response.status_code == 422, (label, response.status_code, response.text[:300])
        assert response.json()["error"]["code"] == "account_archived", (label, response.text[:300])


def test_everything_the_archive_blocked_is_reachable_again_after_a_restore(api_small, load_bearing):
    """Nothing but published calls, from the stranded state back out.

    Without ``restore_account`` every assertion after the restore fails, so this is a regression test for the
    finding rather than for the fix.
    """
    a = api_small
    version = load_bearing["account_version"]
    entry_id, line_id = load_bearing["entry_id"], load_bearing["unbooked_line"]
    assert _archive(a, version).status_code == 201

    restored = a.post(f"/accounts/{CASH}/restore", key=key(),
                      json={"reason": "it was archived before March was finished",
                            "expected_version": version + 1})
    assert restored.status_code == 201, restored.text[:400]
    assert restored.json()["account"]["archived"] is False
    assert restored.json()["version"] == version + 2

    assert a.post(f"/entries/{entry_id}/reverse", key=key(),
                  json={"reason": "miscoded"}).status_code == 201
    assert a.post(f"/lines/{line_id}/book", key=key(), json={
        "narration": "account fee", "reason": "the fee is ours",
        "legs": [{"account": OFFICE, "commodity": "USD", "amount": "50.00"}]}).status_code == 201
    # The account can be retired again, which is the state the caller wanted all along.
    assert _archive(a, version + 2).status_code == 201


def test_a_restore_is_refused_on_an_account_that_is_not_archived(api_small):
    """The inverse is as narrow as the command: it is not a way to bump a version or write an audit action."""
    a = api_small
    version = cash_version(a)
    refused = a.post(f"/accounts/{CASH}/restore", key=key(),
                     json={"reason": "it is not archived", "expected_version": version})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "immutable"


def test_a_restore_takes_the_version_the_caller_read(api_small, load_bearing):
    """Same concurrency answer archiving gives: two callers acting on two readings of the chart is a race."""
    a = api_small
    version = load_bearing["account_version"]
    assert _archive(a, version).status_code == 201
    stale = a.post(f"/accounts/{CASH}/restore", key=key(),
                   json={"reason": "restoring from a stale read", "expected_version": version})
    assert stale.status_code in (409, 412), stale.text[:300]
    assert stale.json()["error"]["code"] == "stale_version"
    assert a.scalar("SELECT archived FROM accounts WHERE path=?", (CASH,)) == 1


def test_a_restore_records_why_the_account_came_back(api_small, load_bearing):
    """The audit action is what makes a chart that shows an account retired and then not readable."""
    a = api_small
    version = load_bearing["account_version"]
    assert _archive(a, version).status_code == 201
    reason = "March was not finished when the account was retired"
    assert a.post(f"/accounts/{CASH}/restore", key=key(),
                  json={"reason": reason, "expected_version": version + 1}).status_code == 201
    kind, subject, recorded = a.row("SELECT kind, subject_id, reason FROM events ORDER BY seq DESC LIMIT 1")
    assert kind == "account.restored"
    assert subject == a.scalar("SELECT id FROM accounts WHERE path=?", (CASH,))
    assert recorded == reason

    empty = a.post(f"/accounts/{CASH}/restore", key=key(),
                   json={"reason": "   ", "expected_version": version + 2})
    assert empty.status_code == 422
