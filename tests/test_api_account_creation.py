"""``POST /accounts``: a caller can extend the chart, and the route is strict about what it accepts.

The route goes through ``provisioning.add_account``, the same function every provisioned account comes through,
so what it writes is not a second class of account.

Most tests here are about what it refuses, because the chart is authored data: an account is permanent, every
report afterwards carries it, and the only way back out is to archive it.  So a type must be given rather than
guessed from the path, a commodity must be declared, ``is_cash`` must be asked for rather than assumed, and a path
already in the chart is a refusal rather than a twin.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest


def post(api, body: dict, *, key: str | None = None):
    return api.post("/accounts", key=key or str(uuid.uuid4()), json=body)


def minimal(**overrides) -> dict:
    return {"path": "expenses:professional-services:audit", "type": "expense", "commodities": ["USD"], **overrides}


# What it writes
def test_an_account_is_written_and_comes_back_ready_to_book_to(api_small):
    """The reply is the whole account, so the caller can post to it without re-reading the chart."""
    response = post(api_small, minimal())
    assert response.status_code == 201, response.text
    account = response.json()["account"]
    assert account["path"] == "expenses:professional-services:audit"
    assert account["type"] == "expense"
    assert account["commodities"] == ["USD"]
    assert account["archived"] is False
    assert account["id"] and account["version"] == 1
    assert account["links"]["self"].endswith(f"/accounts/{account['id']}")

    # It is in the chart the next reader sees, not only in the reply.
    chart = api_small.json("/accounts")["items"]
    assert account["id"] in {a["id"] for a in chart}


def test_the_account_records_who_wrote_it_and_when(api_small):
    created = post(api_small, minimal()).json()["account"]
    from conftest import API_ACTOR

    assert api_small.scalar("SELECT created_by FROM accounts WHERE id=?", (created["id"],)) == API_ACTOR
    assert api_small.scalar("SELECT created_at FROM accounts WHERE id=?", (created["id"],))


def test_adding_an_account_writes_one_audit_action(api_small):
    """``create_account`` writes one audit action, like every other command (issue 0006).

    Without it, "who extended the chart, and when" would be answerable only from ``created_by`` on the row, and
    "why" not at all.
    """
    before = api_small.scalar("SELECT count(*) FROM events")
    body = post(api_small, minimal()).json()
    assert api_small.scalar("SELECT count(*) FROM events") == before + 1
    kind, subject, reason = api_small.row(
        "SELECT kind, subject_id, reason FROM events ORDER BY seq DESC LIMIT 1")
    assert (kind, subject) == ("account.created", body["account"]["id"])
    assert reason == "add an account to the chart"  # the command's own sentence, since none was given
    assert body["audit_seq"] == api_small.scalar("SELECT max(seq) FROM events")


def test_a_reason_is_optional_and_kept_verbatim_when_it_is_given(api_small):
    """Optional, because adding an account is structure rather than a claim about what happened.  When given,
    it is kept exactly as written, like every other reason."""
    said = "the auditor's fee has no home in this chart and the 2026 engagement letter is signed"
    post(api_small, minimal(reason=said))
    assert api_small.scalar("SELECT reason FROM events ORDER BY seq DESC LIMIT 1") == said


def test_several_commodities_may_be_declared_at_once(api_small):
    account = post(api_small, minimal(commodities=["USD", "EUR"])).json()["account"]
    assert sorted(account["commodities"]) == ["EUR", "USD"]


def test_a_code_and_a_tax_code_are_kept_when_given(api_small):
    account = post(api_small, minimal(code="6230", tax_code="es-pgc-623")).json()["account"]
    assert account["code"] == "6230" and account["tax_code"] == "es-pgc-623"


def test_an_account_may_be_written_on_either_side_of_any_statement(api_small):
    """Five types, and the route takes all of them: this ledger is not a chart of one company's accounts."""
    for i, kind in enumerate(("asset", "liability", "equity", "income", "expense")):
        response = post(api_small, minimal(path=f"{kind}-side:new-{i}", type=kind))
        assert response.status_code == 201, response.text
        assert response.json()["account"]["type"] == kind


# What it refuses
def test_a_type_is_required_because_no_path_implies_one(api_small):
    """``assets:...`` usually is an asset, and a chart is free to put a contra-asset there instead.  Guessing
    puts an account on the wrong side of a statement in every report that follows."""
    body = minimal()
    del body["type"]
    response = post(api_small, body)
    assert response.status_code == 422
    assert any(d["path"] == "type" for d in response.json()["error"]["details"])


def test_a_type_that_is_not_one_of_the_five_is_refused_by_name(api_small):
    response = post(api_small, minimal(type="revenue"))
    assert response.status_code == 422
    detail = response.json()["error"]["details"][0]
    assert detail["reason"] == "enum" and detail["value"] == "revenue"


def test_at_least_one_commodity_must_be_declared(api_small):
    assert post(api_small, minimal(commodities=[])).status_code == 422


def test_a_commodity_the_entity_does_not_have_is_refused_rather_than_created(api_small):
    response = post(api_small, minimal(commodities=["XBT"]))
    assert response.status_code == 422
    detail = response.json()["error"]["details"][0]
    assert detail["reason"] == "commodity_unknown" and detail["value"] == ["XBT"]


def test_a_path_already_in_the_chart_is_refused(api_small):
    """Two accounts that print identically in every report is not a state worth being able to reach."""
    assert post(api_small, minimal()).status_code == 201
    response = post(api_small, minimal())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "immutable"


def test_a_blank_path_is_refused(api_small):
    assert post(api_small, minimal(path="   ")).status_code == 422


def test_an_unknown_field_is_refused_rather_than_dropped(api_small):
    """``extra="forbid"``: a caller that sent ``"cash": true`` meaning ``is_cash`` must be told, not quietly
    given a non-cash account."""
    response = post(api_small, minimal(cash=True))
    assert response.status_code == 422


def test_a_write_without_an_idempotency_key_is_refused(api_small):
    response = api_small.client.post(f"{api_small.base}/accounts", json=minimal(), headers=api_small.headers)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "idempotency_key_required"


def test_the_same_key_replays_rather_than_writing_a_second_account(api_small):
    key = str(uuid.uuid4())
    first = post(api_small, minimal(), key=key)
    second = post(api_small, minimal(), key=key)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert api_small.scalar("SELECT count(*) FROM accounts WHERE path=?", (minimal()["path"],)) == 1


def test_the_same_key_carrying_a_different_account_is_a_conflict(api_small):
    key = str(uuid.uuid4())
    assert post(api_small, minimal(), key=key).status_code == 201
    clash = post(api_small, minimal(path="expenses:professional-services:legal"), key=key)
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "idempotency_conflict"


# is_cash
def test_is_cash_defaults_to_false(api_small):
    """A cash account is one a bank statement is reconciled against.  That is a claim about the real world, and
    it is not something to acquire by leaving a field out while booking a line."""
    account = post(api_small, minimal()).json()["account"]
    assert account["is_cash"] is False


def test_a_cash_account_can_still_be_asked_for_explicitly(api_small):
    account = post(api_small, minimal(path="assets:cash:second-bank", type="asset", is_cash=True)).json()["account"]
    assert account["is_cash"] is True


# It is published
def test_the_route_is_in_the_capabilities_a_caller_reads(api_small):
    """Capabilities is how an agent finds out what it may do.  A write that is not listed there is a write
    nothing will ever call."""
    writes = api_small.json("/capabilities")["writes"]
    assert f"POST /v1/entities/{api_small.entity_id}/accounts" in writes


def test_the_route_is_in_the_published_openapi_document(api_small):
    paths = api_small.get("/openapi.json").json()["paths"]
    body = paths["/v1/entities/{entity_id}/accounts"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert set(schema["required"]) == {"path", "type", "commodities"}
    assert schema["properties"]["is_cash"]["default"] is False


# Scoping
def test_a_credential_scoped_elsewhere_cannot_write_into_this_chart(api_small):
    from conftest import OTHER_TOKEN

    response = api_small.post("/accounts", key=str(uuid.uuid4()), json=minimal(), token=OTHER_TOKEN)
    assert response.status_code in (403, 404)
    assert api_small.scalar("SELECT count(*) FROM accounts WHERE path=?", (minimal()["path"],)) == 0


# The export stays valid
@pytest.mark.slow
def test_an_account_written_through_the_api_is_declared_in_the_export_and_passes_check_strict(api_inc, tmp_path):
    """``hledger check --strict`` fails on any undeclared account, so an account this route writes must be
    declared by the export or every later export breaks.

    It is, because ``accounts.journal`` is rendered from the whole chart in the store (``exporter``:
    ``sorted(a.path for a in accounts.values())``), not from the accounts that carry a posting.  A brand-new
    account with nothing booked to it is declared like any other, and the strict check accepts a declaration with
    no transactions.
    """
    from haddon.exporting.exporter import export_book
    from haddon.importing.hledger import Hledger

    path = "expenses:professional-services:audit"
    assert post(api_inc, {"path": path, "type": "expense", "commodities": ["USD"]}).status_code == 201

    result = export_book(api_inc.path, tmp_path / "out", year=2025)
    ledger = Path(result.ledger_dir)
    assert f"account {path}" in (ledger / "accounts.journal").read_text(encoding="utf-8")

    Hledger().check_strict(ledger / "2025.journal")  # raises on any undeclared account
    assert path in Hledger().declared_accounts(ledger / "2025.journal")


# Archiving: the other half
def archive(api, account_id: str, *, reason: str = "the 2025 engagement is over and this account is not reused",
            version: int = 1, key: str | None = None):
    return api.post(f"/accounts/{account_id}/archive", key=key or str(uuid.uuid4()),
                    json={"reason": reason, "expected_version": version})


def test_an_account_is_archived_and_the_reason_is_the_record(api_small):
    """Archiving has a route and records its reason (issue 0006).

    Unlike creation, it requires a reason.  Retiring an account is a decision about the books rather than
    structure, and "why did this stop being used?" is what a reviewer asks about it years later.
    """
    created = post(api_small, minimal()).json()["account"]
    response = archive(api_small, created["id"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["account"]["archived"] is True and body["account"]["version"] == 2
    kind, subject, reason = api_small.row("SELECT kind, subject_id, reason FROM events ORDER BY seq DESC LIMIT 1")
    assert (kind, subject) == ("account.archived", created["id"])
    assert reason.startswith("the 2025 engagement is over")
    assert body["audit_seq"] == api_small.scalar("SELECT max(seq) FROM events")


def test_an_account_is_archived_by_its_path_as_well_as_its_id(api_small):
    """Every other place an account is named in the command surface takes a path; this one does too."""
    created = post(api_small, minimal()).json()["account"]
    assert archive(api_small, created["path"]).status_code == 201
    assert api_small.scalar("SELECT archived FROM accounts WHERE id=?", (created["id"],)) == 1


def test_archiving_needs_a_reason(api_small):
    created = post(api_small, minimal()).json()["account"]
    response = api_small.post(f"/accounts/{created['id']}/archive", key=str(uuid.uuid4()),
                              json={"expected_version": 1})
    assert response.status_code == 422
    assert api_small.scalar("SELECT archived FROM accounts WHERE id=?", (created["id"],)) == 0


def test_archiving_from_a_stale_reading_of_the_chart_is_refused(api_small):
    """The same concurrency answer a close gives: the second caller acted on a chart it never saw."""
    created = post(api_small, minimal()).json()["account"]
    assert archive(api_small, created["id"], version=1).status_code == 201
    again = archive(api_small, created["id"], version=1)
    assert again.status_code in (409, 412)
    assert again.json()["error"]["code"] in ("stale_version", "immutable")


def test_an_archived_account_refuses_a_new_posting_and_keeps_its_old_ones(api_small):
    """Archiving is not deletion and does not reach backwards.

    What is already posted stays posted and stays in every report that covers its date.  Only a new leg is
    refused.  A test that checked the refusal alone would pass against an implementation that deleted the
    account's history.
    """
    account = post(api_small, minimal()).json()["account"]
    entry = api_small.post("/entries", key=str(uuid.uuid4()), json={
        "dt": "2026-03-04", "narration": "Audit fee, 2025 engagement",
        "reason": "the auditor invoiced the 2025 engagement",
        "legs": [{"account": account["path"], "commodity": "USD", "amount": "1200.00"},
                 {"account": "assets:cash", "commodity": "USD", "amount": "-1200.00"}]})
    assert entry.status_code == 201, entry.text
    assert archive(api_small, account["id"]).status_code == 201

    refused = api_small.post("/entries", key=str(uuid.uuid4()), json={
        "dt": "2026-03-05", "narration": "Another audit fee", "reason": "a second invoice arrived",
        "legs": [{"account": account["path"], "commodity": "USD", "amount": "50.00"},
                 {"account": "assets:cash", "commodity": "USD", "amount": "-50.00"}]})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "account_archived"

    # The entry already posted to it is untouched and still on the account's register.
    assert api_small.json(f"/entries/{entry.json()['entry']['id']}")["entry"]["status"] == "posted"
    register = api_small.json(f"/accounts/{account['id']}/register")
    assert [line["entry_id"] for line in register["items"]] == [entry.json()["entry"]["id"]]
