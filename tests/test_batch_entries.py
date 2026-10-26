"""``POST /batches``: many entries posted as one unit, applied whole or refused whole.

One entry per call is the right shape for a reviewer booking what they are looking at and the wrong one for a
conversion.  An agent handed a year of somebody's journal converts it outside the ledger (``decisions.md``,
"Chunk F1 -- the generic statement input") and then has to get over a thousand entries in.  Through a loop of
single posts it can afterwards show only that each call answered 201.  A dropped entry, a retried one that
landed twice, a connection lost two thirds of the way through: each leaves an append-only ledger that is
internally consistent and wrong, and nothing in it disagrees.

So the batch carries the caller's own assertion of what it contains (a count, and per-commodity debits and
credits) and is refused whole when what was posted does not match it.  The tests are mostly refusals, and each
is compared against a full table-count snapshot taken before it.  The claim checked is not "refused" but
"refused, and no trace of it anywhere".

There is no reader, format or per-source logic in these tests, because there is none in the ledger: what
arrives is already normalised entries, exactly as ``POST /entries`` takes them.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import API_ACTOR, table_counts
from haddon.commands import MAX_BATCH_ENTRIES
from repo_helpers import load_fixture, lock_books

REASON = "the 2026 opening journal, converted from the old system and loaded as one batch"

CASH = "assets:cash"
OFFICE = "expenses:office"
RECEIVABLES = "assets:receivables"
SALES = "income:sales"
CASH_EUR = "assets:cash-eur"


def leg(account: str, amount: str, commodity: str = "USD") -> dict:
    return {"account": account, "commodity": commodity, "amount": amount}


def entry(dt: str, narration: str, amount: str, *, debit: str = OFFICE, credit: str = CASH,
          commodity: str = "USD", **overrides) -> dict:
    return {"dt": dt, "narration": narration,
            "legs": [leg(debit, amount, commodity), leg(credit, f"-{amount}", commodity)], **overrides}


#: Three ordinary costs: a batch small enough to read and large enough to have a total.
ENTRIES = [entry("2026-02-02", "Desk rental, February", "1200.00"),
           entry("2026-02-09", "Stationery", "48.50"),
           entry("2026-02-27", "Desk rental, March, paid early", "1200.00")]


def totals(*pairs) -> list[dict]:
    return [{"commodity": code, "debits_micros": debits, "credits_micros": credits}
            for code, debits, credits in pairs]


def batch(entries=None, *, count: int | None = None, asserted=None, reason: str = REASON) -> dict:
    """The submission, with everything asserted correctly unless a test says otherwise."""
    entries = ENTRIES if entries is None else entries
    declared = totals(("USD", "2448500000", "2448500000")) if asserted is None else asserted
    return {"reason": reason, "entries": entries,
            "asserted": {"count": len(entries) if count is None else count, "totals": declared}}


def post(api, body: dict, *, key: str | None = None):
    return api.post("/batches", key=key or str(uuid.uuid4()), json=body)


def err(response) -> dict:
    return response.json()["error"]


@pytest.fixture
def unchanged(api_small):
    """A full table-count snapshot, compared again at the end of the test.

    "It was refused" is a much weaker claim than "it was refused and wrote nothing anywhere"."""
    before = table_counts(api_small.path)
    yield before
    assert table_counts(api_small.path) == before


# What it writes
def test_a_batch_posts_every_entry_it_carries(api_small):
    response = post(api_small, batch())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["count"] == 3 and len(body["entry_ids"]) == 3
    assert body["totals"] == totals(("USD", "2448500000", "2448500000"))
    # They are ordinary entries the next reader sees, in submission order.
    read_back = [api_small.json(f"/entries/{entry_id}")["entry"] for entry_id in body["entry_ids"]]
    assert [e["narration"] for e in read_back] == [e["narration"] for e in ENTRIES]
    assert {e["status"] for e in read_back} == {"posted"}


def test_the_whole_batch_is_one_operations_row(api_small):
    """One batch is one durable job, not three, so "was the whole thing loaded?" is answerable afterwards and
    not only at the moment the calls were made."""
    body = post(api_small, batch()).json()
    row = api_small.row("SELECT kind, state, counts, result FROM operations WHERE id=?", (body["operation_id"],))
    assert row is not None, "the batch recorded no operation"
    kind, state, counts, result = row
    assert (kind, state) == ("batch", "succeeded")
    assert '"entries": 3' in counts and '"postings": 6' in counts
    assert all(entry_id in result for entry_id in body["entry_ids"])
    assert api_small.scalar("SELECT count(*) FROM operations") == 1


def test_it_moves_the_balances_and_not_only_the_entry_table(api_small):
    def balances() -> dict[str, int]:
        return {r["account_path"]: int(r["amount_micros"])
                for r in api_small.json("/reports/trial-balance?year=2026")["lines"]}

    before = balances()
    post(api_small, batch())
    after = balances()
    assert after[OFFICE] - before.get(OFFICE, 0) == 2448500000
    assert after[CASH] - before.get(CASH, 0) == -2448500000


def test_every_entry_keeps_its_own_audit_action_and_the_batch_keeps_one_of_its_own(api_small):
    """A batch does not swallow the per-entry record: each entry is still a booking somebody is answerable for,
    and the batch is the separate fact that they arrived together."""
    before = api_small.scalar("SELECT count(*) FROM events WHERE kind='entry.posted'")
    body = post(api_small, batch()).json()
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='entry.posted'") == before + 3
    seq, subject_kind, subject_id, reason = api_small.row(
        "SELECT seq, subject_kind, subject_id, reason FROM events WHERE kind='batch.posted'")
    assert (subject_kind, subject_id, reason) == ("operation", body["operation_id"], REASON)
    assert body["audit_seq"] == seq


def test_the_actor_is_the_token_and_there_is_no_field_for_one(api_small):
    """Every write here records the credential's actor; a batch is not the place a caller starts naming its own."""
    post(api_small, batch())
    assert api_small.scalar("SELECT count(*) FROM entries WHERE created_by<>?", (API_ACTOR,)) == 0
    refused = post(api_small, {**batch(), "actor": "agent:somebody-else"})
    assert refused.status_code == 422 and err(refused)["code"] == "validation"


def test_an_entry_may_carry_its_own_reason_and_otherwise_inherits_the_batch_s(api_small):
    entries = [entry("2026-02-02", "Desk rental, February", "1200.00"),
               entry("2026-02-09", "Stationery", "48.50", reason="bought out of petty cash, receipt attached"),
               entry("2026-02-27", "Desk rental, March, paid early", "1200.00")]
    body = post(api_small, batch(entries)).json()
    reasons = [api_small.scalar("SELECT reason FROM events WHERE kind='entry.posted' AND subject_id=?", (eid,))
               for eid in body["entry_ids"]]
    assert reasons == [REASON, "bought out of petty cash, receipt attached", REASON]


def test_a_batch_spanning_two_commodities_declares_both(api_small):
    entries = [entry("2026-02-02", "Desk rental, February", "1200.00"),
               entry("2026-02-03", "A euro cost", "300.00", credit=CASH_EUR, commodity="EUR")]
    declared = totals(("EUR", "300000000", "300000000"), ("USD", "1200000000", "1200000000"))
    response = post(api_small, batch(entries, asserted=declared))
    assert response.status_code == 201, response.text
    assert response.json()["totals"] == declared


def test_a_retry_under_the_same_key_replays_rather_than_posting_twice(api_small):
    key = str(uuid.uuid4())
    first = post(api_small, batch(), key=key)
    assert first.status_code == 201, first.text
    again = post(api_small, batch(), key=key)
    assert again.status_code == 201 and again.headers.get("Idempotency-Replayed") == "true"
    assert again.json() == first.json()
    assert api_small.scalar("SELECT count(*) FROM entries") == 3
    assert api_small.scalar("SELECT count(*) FROM operations") == 1


def test_the_maximum_batch_size_is_published_where_it_is_enforced(api_small):
    assert api_small.json("/capabilities")["limits"]["max_batch_entries"] == MAX_BATCH_ENTRIES


def test_the_command_is_discoverable_by_name(api_small):
    commands = {c["name"]: c for c in api_small.json("/capabilities")["commands"]}
    assert commands["post_batch"]["method"] == "POST"
    assert commands["post_batch"]["path"].endswith("/batches")


# What it refuses
def test_a_count_that_disagrees_with_the_submission_is_refused(api_small, unchanged):
    """The cheapest assertion and the one that catches a truncated request body."""
    response = post(api_small, batch(count=4))
    assert response.status_code == 422, response.text
    error = err(response)
    assert error["code"] == "validation"
    assert error["details"][0]["reason"] == "batch_count_does_not_match"
    assert "4" in error["message"] and "3" in error["message"]


def test_totals_that_disagree_name_both_numbers(api_small, unchanged):
    """The assertion that catches a dropped or duplicated entry: one entry short of the caller's own sum."""
    response = post(api_small, batch(ENTRIES[:2], count=2))
    assert response.status_code == 422, response.text
    error = err(response)
    assert error["code"] == "unbalanced"
    detail = error["details"][0]
    assert detail["commodity"] == "USD"
    assert detail["asserted_debits_micros"] == "2448500000"
    assert detail["actual_debits_micros"] == "1248500000"
    assert detail["asserted_credits_micros"] == "2448500000"
    assert detail["actual_credits_micros"] == "1248500000"


def test_one_side_transposed_is_refused_even_though_the_batch_itself_balances(api_small, unchanged):
    """Why both sides are asked for.  Every entry balances, so the ledger's own arithmetic has nothing to say
    here.  The caller computed the two sides from its source and one of them disagrees."""
    response = post(api_small, batch(asserted=totals(("USD", "2448500000", "2484500000"))))
    assert response.status_code == 422
    detail = err(response)["details"][0]
    assert detail["asserted_credits_micros"] == "2484500000"
    assert detail["actual_credits_micros"] == "2448500000"


def test_a_commodity_the_batch_touches_and_the_assertion_does_not_is_refused(api_small, unchanged):
    """A conversion that quietly dropped a currency is what an asserted total exists to catch, so the check is
    over both sets rather than over the declared one."""
    entries = [entry("2026-02-02", "Desk rental, February", "1200.00"),
               entry("2026-02-03", "A euro cost", "300.00", credit=CASH_EUR, commodity="EUR")]
    response = post(api_small, batch(entries, asserted=totals(("USD", "1200000000", "1200000000"))))
    assert response.status_code == 422
    detail = err(response)["details"][0]
    assert detail["commodity"] == "EUR"
    assert detail["asserted_debits_micros"] is None
    assert detail["actual_debits_micros"] == "300000000"


def test_a_commodity_the_assertion_declares_and_the_batch_does_not_is_refused(api_small, unchanged):
    response = post(api_small, batch(asserted=totals(("EUR", "1000000", "1000000"),
                                                     ("USD", "2448500000", "2448500000"))))
    assert response.status_code == 422
    detail = err(response)["details"][0]
    assert detail["commodity"] == "EUR" and detail["actual_debits_micros"] is None


def test_one_invalid_entry_refuses_the_whole_batch(api_small, unchanged):
    """The entries before it are good and none of them survives.  That is what "one unit" means, and the
    difference between this and a loop of single posts."""
    broken = dict(ENTRIES[1], legs=[leg(OFFICE, "48.50"), leg(CASH, "-40.00")])
    response = post(api_small, batch([ENTRIES[0], broken, ENTRIES[2]]))
    assert response.status_code == 422, response.text
    assert err(response)["code"] == "unbalanced"


def test_an_unknown_account_in_the_last_entry_refuses_the_whole_batch(api_small, unchanged):
    entries = ENTRIES[:2] + [entry("2026-02-27", "To nowhere", "1200.00", debit="not:a:real:account")]
    response = post(api_small, batch(entries))
    assert response.status_code == 422
    error = err(response)
    assert error["code"] == "account_unknown"
    assert error["details"][0]["id"] == "not:a:real:account"


def test_a_refusal_says_which_entry_of_the_batch_it_is_about(api_small, unchanged):
    """``post_entry``'s refusals are phrased for somebody who sent one entry: ``legs[0].account``, ``dt``,
    ``id``.  That is no use across nine hundred entries; a conversion told only "no account
    'not:a:real:account' in this entity" would have to bisect its own submission.  So the refusal keeps its
    code, message and details and gains the entry's place in the batch."""
    entries = ENTRIES[:2] + [entry("2026-02-27", "To nowhere", "1200.00", debit="not:a:real:account",
                                   id="e-to-nowhere")]
    error = err(post(api_small, batch(entries)))
    assert error["code"] == "account_unknown"
    detail = error["details"][0]
    assert detail["batch_index"] == 2
    assert detail["entry_id"] == "e-to-nowhere"
    assert (detail["entry_dt"], detail["entry_narration"]) == ("2026-02-27", "To nowhere")
    # The path is relative to the entry, and the original detail is all still there.
    assert detail["path"] == "entries[2].legs[0].account"
    assert detail["id"] == "not:a:real:account"
    assert "no account 'not:a:real:account' in this entity" in error["message"]
    assert error["message"].startswith("entries[2] (2026-02-27 'To nowhere')")


def test_every_per_entry_rule_locates_itself_the_same_way(api_small, unchanged):
    """Not only the unknown account: the batch carries no rules of its own, so whatever ``post_entry`` refuses
    is what a caller is told, and all of it has to say where."""
    unbalanced = dict(ENTRIES[1], legs=[leg(OFFICE, "48.50"), leg(CASH, "-40.00")])
    impossible_date = dict(ENTRIES[1], dt="2026-13-01")
    for name, broken, code in (("unbalanced", unbalanced, "unbalanced"),
                               ("date", impossible_date, "validation")):
        error = err(post(api_small, batch([ENTRIES[0], broken, ENTRIES[2]])))
        assert error["code"] == code, name
        assert error["details"][0]["batch_index"] == 1, name
        assert error["message"].startswith("entries[1] ("), name


def test_an_entry_with_no_id_of_its_own_is_still_located(api_small, unchanged):
    """``entry_id`` is the caller's id and there may not be one.  The index, the date and the narration are
    always there, and between them they name a row in whatever the batch was converted from."""
    broken = dict(ENTRIES[2], legs=[leg("not:a:real:account", "1200.00"), leg(CASH, "-1200.00")])
    detail = err(post(api_small, batch([ENTRIES[0], ENTRIES[1], broken])))["details"][0]
    assert "entry_id" not in detail
    assert detail["batch_index"] == 2
    assert detail["entry_narration"] == "Desk rental, March, paid early"


def test_a_closed_period_refuses_the_batch_exactly_as_it_refuses_one_entry(api_small):
    """A batch buys no exemption from the period lock, and one entry inside the lock takes the batch with it."""
    assert post(api_small, batch([dict(e, dt="2026-01-05") for e in ENTRIES])).status_code == 201
    engine = api_small.engine()
    try:
        lock_books(load_fixture(engine), "2026-02-10")
    finally:
        engine.dispose()
    before = table_counts(api_small.path)
    response = post(api_small, batch())
    assert response.status_code == 409, response.text
    error = err(response)
    assert error["code"] == "period_locked"
    assert error["details"][0]["remedy"] == "reopen"
    assert table_counts(api_small.path) == before
    # The same batch after the lock posts, so the lock is a boundary rather than a stop.
    after_the_lock = [dict(e, dt="2026-03-01") for e in ENTRIES]
    assert post(api_small, batch(after_the_lock)).status_code == 201


def test_a_batch_over_the_published_maximum_is_refused_by_the_number_it_broke(api_small, unchanged):
    """Refused rather than attempted: a transaction held open for as long as a caller cared to make it is a
    timeout for everybody else writing, and a timeout says nothing about what was loaded."""
    one_too_many = [entry("2026-02-02", f"Cost {i}", "1.00") for i in range(MAX_BATCH_ENTRIES + 1)]
    response = post(api_small, batch(one_too_many, asserted=totals(("USD", "1001000000", "1001000000"))))
    assert response.status_code == 422, response.text[:400]
    error = err(response)
    assert error["details"][0]["reason"] == "batch_too_large"
    assert str(MAX_BATCH_ENTRIES) in error["message"]


def test_an_empty_batch_is_not_a_batch(api_small, unchanged):
    response = post(api_small, {"reason": REASON, "entries": [], "asserted": {"count": 0, "totals": []}})
    assert response.status_code == 422
    assert err(response)["code"] == "validation"


def test_a_batch_records_why_it_was_loaded(api_small, unchanged):
    body = batch()
    del body["reason"]
    assert post(api_small, body).status_code == 422
    assert post(api_small, batch(reason="   ")).status_code == 422


def test_a_failed_batch_burns_no_idempotency_key(api_small):
    """A client that cannot safely retry will retry unsafely.  The refused attempt leaves no request row, so the
    corrected submission goes through under the same key."""
    key = str(uuid.uuid4())
    assert post(api_small, batch(count=4), key=key).status_code == 422
    assert api_small.scalar("SELECT count(*) FROM requests") == 0
    assert post(api_small, batch(), key=key).status_code == 201


def test_it_writes_no_journal_mapping_and_nothing_bank_shaped(api_small):
    """A batch is not a journal import and not a statement.  ``journal_mappings`` is the hledger importer's
    full-fidelity record (the oracle's ``mapping_sha256`` check reads it), and a generic batch has no canonical
    source transaction to put in one."""
    post(api_small, batch())
    for table in ("journal_mappings", "statements", "statement_lines", "allocations", "documents"):
        assert api_small.scalar(f"SELECT count(*) FROM {table}") == 0
