"""Recoverable tax that was not recovered is a decision, and the books record it
(docs/design/decisions.md, "Recoverable tax that was not recovered").

A recoverable tax is sometimes deliberately not claimed, for a documented reason: the document on hand does not
support a claim, or no document exists yet.  The effect alone (a larger number in an expense or an asset) cannot
say what is not being reclaimed or why, and an amount sitting inside a fixed asset would have nothing explaining
it.  It is real money, often recoverable for years afterwards.

What these tests are about:

* It is a record, not a posting.  Nothing is booked; the arithmetic already happened when the entry was booked
  gross.  The entry, its version and every report are identical before and after.
* The total must not double-count.  A trigger allows one live record per entry and commodity, and the total is
  of everything in the state rather than of the page.
* Both endings exist and are different.  Recovered names the entry that recovered it; forgone names nothing, so
  its reason has to carry the whole explanation.
* Nothing here knows a country or a tax.  The deadline is a date the caller supplies; there is no VAT field and
  no rule that computes one.
"""

from __future__ import annotations

import uuid

import pytest

from repo_helpers import load_fixture, lock_books
from test_api_documents import write_entry

RECORD = {"commodity": "USD", "amount_micros": "312400000",
          "reason": "the receipt is a simplified one with the customer number printed as N/A, which does not "
                    "support a claim, so the tax is inside the asset"}


def record(api, entry_id: str, *, key: str | None = None, **overrides):
    return api.post(f"/entries/{entry_id}/unrecovered-tax", key=key or str(uuid.uuid4()),
                    json={**RECORD, **overrides})


def resolve(api, record_id: str, *, key: str | None = None, **body):
    return api.post(f"/unrecovered-tax/{record_id}/resolve", key=key or str(uuid.uuid4()), json=body)


def listed(api, query: str = "") -> dict:
    return api.json(f"/unrecovered-tax{query}")


@pytest.fixture
def entry_id(api_small) -> str:
    return write_entry(api_small, dt="2026-04-24", amount="2642650000")


# Recording it
def test_the_decision_is_recorded_beside_the_entry_and_is_queryable(api_small, entry_id):
    response = record(api_small, entry_id)
    assert response.status_code == 201, response.text
    row = response.json()["record"]
    assert row["entry_id"] == entry_id and row["state"] == "unrecovered"
    assert row["amount_micros"] == "312400000" and row["commodity"] == "USD"
    assert row["reason"].startswith("the receipt is a simplified one")
    assert row["recoverable_until"] is None and row["recovered_entry_id"] is None

    page = listed(api_small)
    assert [i["record"]["id"] for i in page["items"]] == [row["id"]]
    assert page["totals"] == {"USD": "312400000"}
    assert page["state"] == "unrecovered"
    assert page["items"][0]["entry"]["id"] == entry_id   # the entry is beside it, so the list reads on its own


def test_nothing_is_posted_and_no_figure_moves(api_small, entry_id):
    """The arithmetic already happened when the entry was booked gross.  The record changes no figure."""
    before = api_small.json(f"/entries/{entry_id}")
    balances = api_small.json("/reports/trial-balance?year=2026")["lines"]
    record(api_small, entry_id)
    after = api_small.json(f"/entries/{entry_id}")
    assert (after["entry"], after["version"]) == (before["entry"], before["version"])
    assert api_small.json("/reports/trial-balance?year=2026")["lines"] == balances


def test_it_can_name_the_leg_the_amount_is_hiding_inside(api_small, entry_id):
    """312.40 sitting in a fixed asset is invisible in a way that 312.40 in an expense is not."""
    entry = api_small.json(f"/entries/{entry_id}")["entry"]
    leg = next(p for p in entry["postings"] if int(p["amount_micros"]) > 0)
    row = record(api_small, entry_id, posting_id=leg["id"]).json()["record"]
    assert row["posting_id"] == leg["id"]
    assert api_small.json(f"/unrecovered-tax/{row['id']}")["record"]["posting_id"] == leg["id"]


def test_a_leg_of_another_entry_is_refused(api_small, entry_id):
    other = write_entry(api_small, dt="2026-04-25", amount="1000000")
    leg = api_small.json(f"/entries/{other}")["entry"]["postings"][0]["id"]
    response = record(api_small, entry_id, posting_id=leg)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "posting_not_of_entry"


def test_a_deadline_is_a_date_the_caller_supplies_not_a_rule_the_ledger_knows(api_small, entry_id):
    """The deadline makes it an answer to "what can we still reclaim, and by when" rather than a note.

    The ledger applies no regime rule.  It does not know that one country allows four years and another two, and
    must not learn.  It records the date it is given, and refuses one that is not a date.
    """
    row = record(api_small, entry_id, recoverable_until="2030-04-24").json()["record"]
    assert row["recoverable_until"] == "2030-04-24"
    bad = record(api_small, write_entry(api_small, dt="2026-04-26"), recoverable_until="four years")
    assert bad.status_code == 422
    assert any("recoverable_until" in d.get("path", "") for d in bad.json()["error"]["details"])


def test_which_tax_it_is_uses_the_code_the_chart_already_uses(api_small, entry_id):
    row = record(api_small, entry_id, tax_code="es-iva-soportado").json()["record"]
    assert row["tax_code"] == "es-iva-soportado"
    # ... and it is optional, because a one-tax entity has nothing to distinguish
    plain = record(api_small, write_entry(api_small, dt="2026-04-26"), amount_micros="1000000").json()["record"]
    assert plain["tax_code"] is None


def test_it_is_recorded_as_an_audit_action_with_a_reason_and_an_event(api_small, entry_id):
    """The audit action carries actor and reason; there is no proposal/decision pair (issue 0005)."""
    from conftest import API_ACTOR

    row = record(api_small, entry_id).json()["record"]
    assert row["created_by"] == API_ACTOR
    assert "decision_id" not in row
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='tax.unrecovered' AND subject_id=?",
                            (entry_id,)) == 1
    assert api_small.scalar("SELECT created_by FROM events WHERE kind='tax.unrecovered' AND subject_id=?",
                            (entry_id,)) == API_ACTOR
    assert api_small.scalar("SELECT reason FROM events WHERE kind='tax.unrecovered' AND subject_id=?",
                            (entry_id,)) == row["reason"]
    assert "tax.unrecovered" in [e["kind"] for e in api_small.json(f"/entries/{entry_id}/events")["items"]]


# What it refuses
def test_a_negative_or_zero_amount_is_refused(api_small, entry_id):
    """This is an amount not recovered.  A negative one would be a claim, and a claim is a posting."""
    for value in ("-312400000", "0"):
        response = record(api_small, entry_id, amount_micros=value)
        assert response.status_code == 422, value
        assert response.json()["error"]["details"][0]["reason"] == "positive_required"
    assert api_small.scalar("SELECT count(*) FROM unrecovered_tax") == 0


def test_an_amount_off_the_commodity_quantum_is_refused(api_small, entry_id):
    """The same rule every other amount column in this schema carries: 0.0045 USD is not an amount in USD."""
    response = record(api_small, entry_id, amount_micros="4500")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "precision"


def test_a_reason_is_required_because_the_reason_is_the_record(api_small, entry_id):
    response = record(api_small, entry_id, reason="")
    assert response.status_code == 422
    assert any("reason" in d.get("path", "") for d in response.json()["error"]["details"])


def test_a_commodity_these_books_do_not_hold_is_refused(api_small, entry_id):
    response = record(api_small, entry_id, commodity="XBT")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "commodity_unknown"


def test_a_second_live_record_for_the_same_entry_and_commodity_is_refused(api_small, entry_id):
    """A total that can count one amount twice is worse than none."""
    first = record(api_small, entry_id).json()["record"]
    again = record(api_small, entry_id)
    assert again.status_code == 409
    assert again.json()["error"]["details"][0]["record_id"] == first["id"]
    assert listed(api_small)["totals"] == {"USD": "312400000"}


def test_an_entry_this_entity_does_not_hold_is_404(api_small):
    assert record(api_small, "e-not-real").status_code == 404
    assert api_small.get("/unrecovered-tax/ut-not-real").status_code == 404
    assert resolve(api_small, "ut-not-real", outcome="forgone", reason="x").status_code == 404


# The two endings
def test_recovering_it_names_the_entry_that_did_it(api_small, entry_id):
    """The late-invoice case: the invoice turned up and the cost was re-booked, so the amount is claimed after all."""
    row = record(api_small, entry_id).json()["record"]
    recovery = write_entry(api_small, dt="2026-05-28", amount="312400000")
    response = resolve(api_small, row["id"], outcome="recovered", recovered_entry_id=recovery,
                       reason="the supplier reissued a full invoice and the tax was claimed on it")
    assert response.status_code == 201, response.text
    settled = response.json()["record"]
    assert settled["state"] == "recovered" and settled["recovered_entry_id"] == recovery
    assert settled["supersedes_id"] == row["id"]
    assert settled["amount_micros"] == row["amount_micros"]   # the chain carries the amount forward

    assert listed(api_small)["totals"] == {}          # nothing is unrecovered any more
    assert listed(api_small)["items"] == []
    recovered = listed(api_small, "?state=recovered")
    assert [i["record"]["id"] for i in recovered["items"]] == [settled["id"]]
    assert recovered["totals"] == {"USD": "312400000"}
    assert response.json()["links"]["recovered_by"].endswith(f"/entries/{recovery}")


def test_forgoing_it_is_a_different_ending_and_says_so(api_small, entry_id):
    """No better document is coming, or the window closed.  Without this the list only ever grows."""
    row = record(api_small, entry_id).json()["record"]
    response = resolve(api_small, row["id"], outcome="forgone",
                       reason="the four years to claim it have passed")
    assert response.status_code == 201, response.text
    settled = response.json()["record"]
    assert settled["state"] == "forgone" and settled["recovered_entry_id"] is None
    assert settled["reason"] == "the four years to claim it have passed"
    assert listed(api_small)["totals"] == {}
    assert [r["state"] for r in response.json()["history"]] == ["unrecovered", "forgone"]
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='tax.forgone' AND subject_id=?",
                            (entry_id,)) == 1


def test_a_recovery_must_name_a_different_entry(api_small, entry_id):
    """A recovery is its own economic event with its own legs; it is not the entry that failed to claim."""
    row = record(api_small, entry_id).json()["record"]
    no_entry = resolve(api_small, row["id"], outcome="recovered", reason="claimed")
    assert no_entry.status_code == 422
    assert no_entry.json()["error"]["details"][0]["path"] == "recovered_entry_id"
    same = resolve(api_small, row["id"], outcome="recovered", recovered_entry_id=entry_id, reason="claimed")
    assert same.status_code == 422
    assert same.json()["error"]["details"][0]["reason"] == "same_entry"


def test_an_entry_cannot_be_named_when_nothing_is_being_recovered(api_small, entry_id):
    row = record(api_small, entry_id).json()["record"]
    other = write_entry(api_small, dt="2026-05-28", amount="1000000")
    response = resolve(api_small, row["id"], outcome="forgone", recovered_entry_id=other, reason="lapsed")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "not_recovered"


def test_an_unknown_outcome_is_refused_by_name(api_small, entry_id):
    row = record(api_small, entry_id).json()["record"]
    response = resolve(api_small, row["id"], outcome="written-off", reason="x")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["value"] == "written-off"


def test_a_record_is_resolved_once(api_small, entry_id):
    row = record(api_small, entry_id).json()["record"]
    assert resolve(api_small, row["id"], outcome="forgone", reason="lapsed").status_code == 201
    again = resolve(api_small, row["id"], outcome="forgone", reason="lapsed again")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "immutable"


def test_the_same_amount_can_be_recorded_again_after_a_resolution(api_small, entry_id):
    """Word for word the same record, reopened after a resolution, is a new record and not a duplicate."""
    first = record(api_small, entry_id).json()["record"]
    resolve(api_small, first["id"], outcome="forgone", reason="lapsed")
    again = record(api_small, entry_id)
    assert again.status_code == 201, again.text
    assert listed(api_small)["totals"] == {"USD": "312400000"}
    assert [r["state"] for r in again.json()["history"]] == ["unrecovered", "forgone", "unrecovered"]


# The query
def test_the_total_is_of_the_state_and_not_of_the_page(api_small):
    """A total that changed as you paged would be worse than no total at all."""
    for i in range(3):
        entry = write_entry(api_small, dt=f"2026-04-0{i + 1}", amount="10000000")
        record(api_small, entry, amount_micros="1000000")
    page = api_small.json("/unrecovered-tax?limit=1")
    assert len(page["items"]) == 1 and page["next_cursor"] is not None
    assert page["totals"] == {"USD": "3000000"}


def test_the_list_narrows_by_the_entry_date(api_small):
    april = write_entry(api_small, dt="2026-04-24", amount="10000000")
    may = write_entry(api_small, dt="2026-05-28", amount="10000000")
    record(api_small, april, amount_micros="1000000")
    record(api_small, may, amount_micros="2000000")
    assert listed(api_small, "?from=2026-05-01&to=2026-05-31")["totals"] == {"USD": "2000000"}
    assert listed(api_small, "?from=2026-01-01&to=2026-12-31")["totals"] == {"USD": "3000000"}
    assert listed(api_small, "?from=2025-01-01&to=2025-12-31")["items"] == []


def test_an_unknown_state_filter_is_refused(api_small):
    response = api_small.get("/unrecovered-tax?state=pending")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "enum"


def test_a_closed_month_does_not_stop_the_decision_being_recorded(api_small):
    """Establishing that a tax was not recovered happens long after the month it was not recovered in."""
    entry = write_entry(api_small, dt="2026-04-24", amount="10000000")
    engine = api_small.engine()
    try:
        lock_books(load_fixture(engine), "2026-05-31")
    finally:
        engine.dispose()
    balances = api_small.json("/reports/trial-balance?year=2026")["lines"]
    assert record(api_small, entry, amount_micros="1000000").status_code == 201
    assert listed(api_small)["totals"] == {"USD": "1000000"}
    assert api_small.json("/reports/trial-balance?year=2026")["lines"] == balances


def test_the_record_is_never_edited_in_place(api_small, entry_id):
    row = record(api_small, entry_id).json()["record"]
    con = api_small.raw()
    try:
        with pytest.raises(Exception, match="immutable"):
            con.execute("UPDATE unrecovered_tax SET amount_micros=1 WHERE id=?", (row["id"],))
        with pytest.raises(Exception, match="immutable"):
            con.execute("DELETE FROM unrecovered_tax WHERE id=?", (row["id"],))
    finally:
        con.close()


def test_a_record_replays_under_the_same_key(api_small, entry_id):
    first = record(api_small, entry_id, key="ut-1")
    again = record(api_small, entry_id, key="ut-1")
    assert first.status_code == again.status_code == 201
    assert again.headers["Idempotency-Replayed"] == "true"
    assert api_small.scalar("SELECT count(*) FROM unrecovered_tax") == 1
