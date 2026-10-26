"""``POST /entries``: the books can state something a bank statement does not.

The importer loads a book once, and booking a statement line builds an entry around the line, which is
always the cash leg.  An invoice raised, a payroll accrued or a withholding reclassified needs neither, so
it comes in here.  These tests check that this adds a way in without adding a second posting path.

Most of them are refusals, deliberately.  A manual entry is the least constrained thing this API does, since
the caller names every leg, and naming every leg buys no exemption from any rule.  It balances or it does not
post, it is in an open period or it does not post, and a closed month refuses it exactly as it refuses a
booked line.  The last group checks that a refusal leaves nothing behind.
"""

from __future__ import annotations

import uuid

import pytest

from haddon import commands
from haddon.domain.errors import HaddonError
from haddon.repository.entries import get_entry
from repo_helpers import AGENT, load_fixture, lock_books, m

REASON = "the April invoice was raised on the 1st; the receipt is a separate line"


def post(api, body: dict, *, key: str | None = None):
    return api.post("/entries", key=key or str(uuid.uuid4()), json=body)


def leg(api, path: str, amount: str, **kw) -> dict:
    """One public command leg: an account path and a decimal amount in whole units."""
    return {"account": path, "commodity": "USD", "amount": amount, **kw}


def accrual(api, **overrides) -> dict:
    """A two-leg sales accrual: a receivable raised against income, with no cash anywhere in it."""
    return {"dt": "2026-04-01", "narration": "Invoice 0041 — services, April",
            "reason": REASON,
            "legs": [leg(api, "assets:receivables", "7316.67"),
                     leg(api, "income:sales", "-7316.67")], **overrides}


# what it writes
def test_an_accrual_with_no_bank_line_behind_it_posts(api_small):
    """The whole point: an economic event on a date, and no cash row on that date."""
    response = post(api_small, accrual(api_small))
    assert response.status_code == 201, response.text
    body = response.json()
    entry = body["entry"]
    assert entry["status"] == "posted"
    assert entry["dt"] == "2026-04-01"
    assert entry["origin"] == "manual"
    assert entry["narration"] == "Invoice 0041 — services, April"
    assert [p["amount_micros"] for p in entry["postings"]] == ["7316670000", "-7316670000"]
    assert body["links"]["reverse"].endswith(f"/entries/{entry['id']}/reverse")

    # and it is in the books the next reader sees, not only in the reply
    read_back = api_small.json(f"/entries/{entry['id']}")["entry"]
    assert read_back["id"] == entry["id"] and read_back["status"] == "posted"


def test_a_stable_account_id_remains_an_advanced_input_option(api_small):
    accounts = {a["path"]: a["id"] for a in api_small.json("/accounts?limit=200")["items"]}
    body = accrual(api_small)
    body["legs"][0]["account"] = accounts["assets:receivables"]
    response = post(api_small, body)
    assert response.status_code == 201, response.text
    assert response.json()["entry"]["postings"][0]["account_id"] == accounts["assets:receivables"]


def test_it_moves_the_balances_and_not_only_the_entry_table(api_small):
    """An entry that does not reach a report is not a booking.  Checked against the trial balance, which is
    computed from the postings rather than from anything this route returned."""
    def balances() -> dict[str, int]:
        report = api_small.json("/reports/trial-balance?year=2026")
        return {r["account_path"]: int(r["amount_micros"]) for r in report["lines"]}

    before = balances()
    post(api_small, accrual(api_small))
    after = balances()
    assert after["assets:receivables"] - before.get("assets:receivables", 0) == 7316670000
    assert after["income:sales"] - before.get("income:sales", 0) == -7316670000


def test_four_legs_post_as_one_entry(api_small):
    """A payroll accrual is gross, withholding, a net payable and, in a real chart, more besides.  A ledger that
    could only book pairs would have to split it into entries that are each individually a lie."""
    body = {"dt": "2026-01-31", "narration": "Payroll January", "reason": "the month's salary run",
            "origin": "payroll",
            "legs": [leg(api_small, "expenses:office", "7416.67"),
                     leg(api_small, "assets:receivables", "100.00"),
                     leg(api_small, "liabilities:payables", "-1640.00"),
                     leg(api_small, "assets:cash", "-5876.67")]}
    response = post(api_small, body)
    assert response.status_code == 201, response.text
    entry = response.json()["entry"]
    assert len(entry["postings"]) == 4
    assert [p["seq"] for p in entry["postings"]] == [1, 2, 3, 4]
    assert sum(int(p["amount_micros"]) for p in entry["postings"]) == 0
    assert entry["origin"] == "payroll"


def test_the_note_fields_survive_the_round_trip(api_small):
    """``code``/``comment``/``tags`` are what the export renders; an entry written here must be exportable the
    same as an imported one."""
    body = accrual(api_small, code="0041", comment="raised on the 1st, collected 27 April",
                   tags=[["invoice", "0041"], ["quarter", "2T"]])
    entry = post(api_small, body).json()["entry"]
    assert entry["code"] == "0041"
    assert entry["comment"] == "raised on the 1st, collected 27 April"
    assert entry["tags"] == [["invoice", "0041"], ["quarter", "2T"]]


def test_any_origin_the_ledger_declares_may_be_named(api_small):
    for origin in ("manual", "agent", "invoice", "payroll", "adjustment", "source"):
        entry = post(api_small, accrual(api_small, origin=origin)).json()["entry"]
        assert entry["origin"] == origin


# what it records
def test_the_entry_records_one_direct_audit_action_and_no_approval_chain(api_small):
    """Issue 0003: the durable result and one intelligible audit action, not a synthetic proposal/decision/
    application chain.  The action carries the reason, the affected entry and the hash of the command input."""
    body = post(api_small, accrual(api_small)).json()
    entry_id = body["entry"]["id"]
    assert body["audit_seq"] > 0
    for dead in ("proposals", "decisions", "applications"):
        assert api_small.scalar("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (dead,)) == 0

    events = api_small.json(f"/entries/{entry_id}/events")["items"]
    assert [e["kind"] for e in events] == ["entry.posted"]
    assert events[0]["reason"] == REASON
    assert events[0]["after"]["action"] == "entry.posted"
    assert events[0]["after"]["result"]["entry_id"] == entry_id
    assert events[0]["after"]["input_hash"]
    request_id = api_small.scalar("SELECT request_id FROM events WHERE seq=?", (body["audit_seq"],))
    assert request_id and api_small.scalar("SELECT count(*) FROM requests WHERE id=?", (request_id,)) == 1


def test_the_entry_records_who_wrote_it(api_small):
    from conftest import API_ACTOR

    entry = post(api_small, accrual(api_small)).json()["entry"]
    assert entry["created_by"] == API_ACTOR
    assert api_small.scalar("SELECT created_by FROM events ORDER BY seq DESC LIMIT 1") == API_ACTOR


def test_the_narration_and_the_reason_are_kept_apart(api_small):
    """The narration is what the entry reads as in every report; the reason is why somebody wrote it.  Collapsing
    the two would put an explanation into the books' own prose, or lose it entirely."""
    entry = post(api_small, accrual(api_small)).json()["entry"]
    assert entry["narration"] == "Invoice 0041 — services, April"
    assert REASON not in entry["narration"]
    assert api_small.scalar("SELECT reason FROM events ORDER BY seq DESC LIMIT 1") == REASON


def test_a_posted_manual_entry_is_reversed_like_any_other(api_small):
    """It joins up with the correction path rather than standing outside it: what this route writes is an
    ordinary posted entry, not a second class of one."""
    entry_id = post(api_small, accrual(api_small)).json()["entry"]["id"]
    response = api_small.post(f"/entries/{entry_id}/reverse", key=str(uuid.uuid4()),
                              json={"reason": "the invoice was cancelled"})
    assert response.status_code == 201, response.text
    mirror = response.json()["entry"]
    assert [p["amount_micros"] for p in mirror["postings"]] == ["-7316670000", "7316670000"]
    assert api_small.json(f"/entries/{entry_id}")["entry"]["status"] == "posted"


def test_nothing_about_it_is_a_statement_line(api_small):
    """General, not bank-specific: no statement line, no statement and no allocation are involved, which is the
    difference between this and booking a line."""
    before = api_small.scalar("SELECT count(*) FROM statement_lines")
    post(api_small, accrual(api_small))
    assert api_small.scalar("SELECT count(*) FROM statement_lines") == before
    assert api_small.scalar("SELECT count(*) FROM allocations") == 0


# what it refuses
def test_an_unbalanced_entry_is_refused(api_small):
    body = accrual(api_small, legs=[leg(api_small, "assets:receivables", "7316.67"),
                                    leg(api_small, "income:sales", "-6000.00")])
    response = post(api_small, body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "unbalanced"
    assert error["details"][0]["commodity"] == "USD"


def test_a_closed_period_refuses_it_and_says_how_to_get_through(api_small):
    """The same refusal, with the same ``reopen`` remedy, that a classification into a closed month gets."""
    assert post(api_small, accrual(api_small, dt="2026-01-15")).status_code == 201
    engine = api_small.engine()
    try:
        lock_books(load_fixture(engine), "2026-03-31")
    finally:
        engine.dispose()
    response = post(api_small, accrual(api_small, dt="2026-02-01"))
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "period_locked"
    assert error["details"][0]["locked_through"] == "2026-03-31"
    assert error["details"][0]["remedy"] == "reopen"
    # and the month after the lock is still writable, so the lock is a boundary rather than a stop
    assert post(api_small, accrual(api_small, dt="2026-04-01")).status_code == 201


def test_a_single_leg_is_not_an_entry(api_small):
    response = post(api_small, accrual(api_small, legs=[leg(api_small, "assets:receivables", "0")]))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"


def test_an_unknown_account_is_refused_by_name(api_small):
    body = accrual(api_small, legs=[{"account": "not:a:real:account", "commodity": "USD",
                                     "amount": "7316.67"},
                                    leg(api_small, "income:sales", "-7316.67")])
    response = post(api_small, body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "account_unknown"
    assert error["details"][0]["id"] == "not:a:real:account"


def test_a_reason_is_required(api_small):
    body = accrual(api_small)
    del body["reason"]
    assert post(api_small, body).status_code == 422
    assert post(api_small, accrual(api_small, reason="   ")).status_code == 422


def test_a_reserved_origin_is_not_the_callers_to_claim(api_small):
    """``reversal`` and ``opening`` are assigned by the service.  A caller that could write either could make a
    correction or a year's opening appear that nothing actually performed."""
    for origin in ("reversal", "opening"):
        response = post(api_small, accrual(api_small, origin=origin))
        assert response.status_code == 422, origin
        assert response.json()["error"]["details"][0]["reason"] == "reserved_origin"


def test_a_json_number_in_an_amount_is_refused_as_the_amount_contract(api_small):
    body = accrual(api_small)
    body["legs"][0]["amount"] = 7316.67
    response = post(api_small, body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation"
    assert error["details"][0]["reason"] == "string_required"
    assert error["details"][0]["path"] == "body.legs[0].amount"


# a refusal leaves nothing
@pytest.mark.parametrize("kind", ["unbalanced", "unknown_commodity", "unknown_account"])
def test_a_refused_entry_leaves_no_entry_and_no_audit_action_behind(api_small, kind):
    """A refusal must not leave an audit action claiming success for something that never happened."""
    bodies = {
        "unbalanced": accrual(api_small, legs=[leg(api_small, "assets:receivables", "1.00"),
                                               leg(api_small, "income:sales", "-9.00")]),
        "unknown_commodity": accrual(api_small, legs=[dict(leg(api_small, "assets:receivables", "1.00"),
                                                           commodity="GBP"),
                                                      leg(api_small, "income:sales", "-1.00")]),
        "unknown_account": accrual(api_small, legs=[{"account": "not:a:real:account", "commodity": "USD",
                                                     "amount": "1.00"},
                                                    leg(api_small, "income:sales", "-1.00")]),
    }
    before = (api_small.scalar("SELECT count(*) FROM entries"),
              api_small.scalar("SELECT count(*) FROM postings"),
              api_small.scalar("SELECT count(*) FROM events"))
    assert post(api_small, bodies[kind]).status_code in (409, 422)
    assert (api_small.scalar("SELECT count(*) FROM entries"),
            api_small.scalar("SELECT count(*) FROM postings"),
            api_small.scalar("SELECT count(*) FROM events")) == before


def test_a_retry_with_the_same_key_replays_rather_than_posting_twice(api_small):
    """Two identical accruals is the failure this one guards: the amounts are plausible and the duplicate is
    invisible in any total."""
    key = str(uuid.uuid4())
    body = accrual(api_small)
    first = post(api_small, body, key=key)
    second = post(api_small, body, key=key)
    assert first.status_code == second.status_code == 201
    assert first.json()["entry"]["id"] == second.json()["entry"]["id"]
    assert second.headers["Idempotency-Replayed"] == "true"
    assert api_small.scalar("SELECT count(*) FROM entries WHERE narration=?",
                            ("Invoice 0041 — services, April",)) == 1


def test_two_identical_entries_on_the_same_day_are_both_writable(api_small):
    """Different intents, two keys, two entries.  A genuine repeat (the same supplier, the same amount, the
    same day) is an ordinary thing in a ledger and must not be refused as a duplicate."""
    first = post(api_small, accrual(api_small))
    second = post(api_small, accrual(api_small))
    assert first.status_code == second.status_code == 201
    assert first.json()["entry"]["id"] != second.json()["entry"]["id"]


# the repository, directly
def test_the_repository_posts_and_seals_in_one_transaction(fx):
    with fx.tx(AGENT) as ctx:
        written = commands.post_entry(
            ctx, dt="2026-04-01", narration="Invoice 0041", reason=REASON,
            legs=[commands.LegSpec("assets:receivables", "USD", m("7316670000")),
                  commands.LegSpec("income:sales", "USD", m("-7316670000"))])
        assert written.entry.status == "posted"
    with fx.reader() as conn:
        entry = get_entry(conn, fx.entity_id, written.entry.id)
    assert entry.status == "posted" and entry.dt == "2026-04-01"
    assert len(entry.legs) == 2


def test_the_repository_refuses_a_locked_period(fx):
    with fx.tx(AGENT) as ctx:
        commands.post_entry(
            ctx, dt="2026-01-15", narration="seed", reason="seed",
            legs=[commands.LegSpec("assets:receivables", "USD", m("1000000")),
                  commands.LegSpec("income:sales", "USD", m("-1000000"))])
    lock_books(fx, "2026-03-31")
    with pytest.raises(HaddonError) as exc:
        with fx.tx(AGENT) as ctx:
            commands.post_entry(
                ctx, dt="2026-02-01", narration="Late accrual", reason=REASON,
                legs=[commands.LegSpec("assets:receivables", "USD", m("1000000")),
                      commands.LegSpec("income:sales", "USD", m("-1000000"))])
    assert exc.value.code == "period_locked"


def test_the_repository_refuses_a_commodity_the_account_may_not_hold(fx):
    """Nothing about naming every leg yourself exempts the entry from the chart's own currency bindings."""
    with pytest.raises(HaddonError) as exc:
        with fx.tx(AGENT) as ctx:
            commands.post_entry(
                ctx, dt="2026-04-01", narration="Wrong currency", reason=REASON,
                legs=[commands.LegSpec("assets:receivables", "JPY", m("1000000")),
                      commands.LegSpec("income:sales", "JPY", m("-1000000"))])
    assert exc.value.code == "validation"
    assert exc.value.details[0]["reason"] == "commodity_not_allowed"
