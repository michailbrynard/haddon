"""Chunk D1: the REST spine.

Not the endpoints but the rules that hold across endpoints, each a way a bookkeeping API quietly goes wrong:

* Entity addressing.  One file is one entity.  A credential is scoped.  An id that exists in another entity's
  file must be a plain 404 here, because "not found" and "not yours" leaking into each other is how an API tells
  you what else exists.
* The error envelope.  A refusal from any layer arrives as its own code, status and details, never as a 500.
  An unexpected exception arrives as a 500 that says nothing.
* Idempotency.  The same key with the same request replays; with a different request it is a conflict.  A
  failed attempt burns nothing, because a client that cannot safely retry will retry unsafely.
* Pagination.  Cursors are opaque, signed, and bound to a snapshot.  A page never silently mixes states.
* The wire.  Amounts are strings in both directions, with no exception anywhere in any response body.

Every claim below is driven through the real ASGI app against a real migrated database holding the real
ExampleInc import.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from haddon.api import cursors
from haddon.api.config import CursorKey, CursorKeyring
from haddon.api.idempotency import KEY_HEADER, REPLAYED_HEADER
from haddon.domain.errors import HaddonError
from haddon.repository import provisioning as pv
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction

from conftest import API_ACTOR, CURSOR_KEYS, OTHER_TOKEN, SECOND_ACTOR, SECOND_TOKEN, build_api

#: Measured at 0.5 s or more per test, or needing a shared fixture that costs that much to build (chunk E3).
#: The fast tier (`make test-fast`) skips these; the commit gate (`make test-gate`) does not.
pytestmark = pytest.mark.slow

#: The representative write for the spine tests, chosen because it needs no knowledge of the book: a body that
#: does not depend on the chart, a refusal when the same path is written twice, and rows in a known set of tables
#: (see ``test_all_writes_go_through_the_repository_and_leave_the_triggers_standing``).
ACCOUNT = {"path": "expenses:spine-under-test", "type": "expense", "commodities": ["USD"], "is_cash": False}


def account(**overrides) -> dict:
    return {**ACCOUNT, **overrides}


def walk_all(api, path: str, *, limit: int) -> list[str]:
    """Every id of a collection, walked through its own cursors."""
    out, cursor, pages = [], None, 0
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        page = api.json(f"{path}?limit={limit}{suffix}")
        out.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        pages += 1
        assert pages < 200
        if cursor is None:
            return out


def err(response) -> dict:
    body = response.json()
    assert "error" in body, body
    return body["error"]


# The published contract
def test_openapi_publishes_every_endpoint_the_chunk_serves(api_inc):
    doc = api_inc.client.get("/openapi.json").json()
    served = {path.replace("{entity_id}", "E").replace("{account_id}", "I").replace("{entry_id}", "I")
              .replace("{snapshot_id}", "I").replace("{export_id}", "I")
              .replace("{statement_id}", "I").replace("{line_id}", "I")
              .replace("{document_id}", "I").replace("{record_id}", "I").replace("{close_id}", "I")
              for path in doc["paths"]}
    assert served == {
        "/healthz", "/readyz", "/v1/entities", "/v1/entities/E/capabilities",
        # the entity itself: the collection creates one (`POST /v1/entities`), and `links.self` reads it back
        "/v1/entities/E",
        "/v1/entities/E/accounts", "/v1/entities/E/accounts/I", "/v1/entities/E/accounts/I/register",
        # issue 0006: the chart's lifecycle is two commands, and a posted entry is restated as well as withdrawn
        "/v1/entities/E/accounts/I/archive", "/v1/entities/E/entries/I/correct",
        # the 0006 review: restore is archiving's inverse, so an archive can be taken back
        "/v1/entities/E/accounts/I/restore",
        "/v1/entities/E/entries", "/v1/entities/E/entries/I", "/v1/entities/E/entries/I/events",
        # a converted set of entries arrives as one refusable unit rather than as a loop of single posts
        "/v1/entities/E/batches",
        "/v1/entities/E/reports/trial-balance", "/v1/entities/E/reports/balance-sheet",
        "/v1/entities/E/reports/income-statement", "/v1/entities/E/reports/composition",
        # issue 0008 section B: the shaped statements the interface renders and the PDF draws from
        "/v1/entities/E/reports/statements",
        # chunk G5: the same two statements as the document somebody sends somebody else
        "/v1/entities/E/reports/statements.pdf",
        "/v1/entities/E/events", "/v1/entities/E/snapshots/I", "/v1/entities/E/exports/I",
        # running an export over the wire, and fetching the journal files it published
        "/v1/entities/E/exports", "/v1/entities/E/exports/I/artifacts/{artifact}",
        # issue 0005: the close workflow.  What it would refuse, the close, the reopen, and (0005 review) the
        # reader that gets a completed close back to the snapshot it was recorded against
        "/v1/entities/E/close-checks", "/v1/entities/E/close", "/v1/entities/E/reopen",
        "/v1/entities/E/closes/I",
        # chunk F1: the generic statement input, its read-back and the provenance read
        "/v1/entities/E/statements", "/v1/entities/E/statements/I", "/v1/entities/E/statements/I/lines",
        "/v1/entities/E/provenance",
        # issue 0004: the four things that can be said about a statement line, and the replacement of a
        # statement loaded wrongly
        "/v1/entities/E/lines/I", "/v1/entities/E/lines/I/book", "/v1/entities/E/lines/I/link",
        "/v1/entities/E/lines/I/ignore", "/v1/entities/E/lines/I/reexplain",
        "/v1/entities/E/statements/I/replace",
        # chunk G4: the reviewing surface.  Lines over a date range rather than one document, the
        # bank-against-books comparison at a date, and disagreeing with a posted entry
        "/v1/entities/E/lines", "/v1/entities/E/reconciliation",
        "/v1/entities/E/entries/I/reverse",
        # a document that is not a bank statement can enter the ledger and be filed against an entry
        "/v1/entities/E/documents", "/v1/entities/E/documents/I", "/v1/entities/E/entries/I/documents",
        # issue 0007: the bytes themselves, verified and served, so "evidence" is something a reviewer can open
        "/v1/entities/E/documents/I/content",
        # "booked, awaiting a document": the state, its two endings, and the query it exists for
        "/v1/entities/E/entries/I/awaiting-document", "/v1/entities/E/entries/I/awaiting-document/withdraw",
        "/v1/entities/E/provisional",
        # recoverable tax that was not recovered, its resolution, and the query with its totals
        "/v1/entities/E/entries/I/unrecovered-tax", "/v1/entities/E/unrecovered-tax",
        "/v1/entities/E/unrecovered-tax/I", "/v1/entities/E/unrecovered-tax/I/resolve"}
    # `POST /entries` and `POST /documents` share their paths with the lists, so they add a verb, not a path.
    # The twenty-three mutating verbs: creating an entity; closing and reopening a period (issue 0005); adding,
    # archiving and restoring an account (issue 0006 and its review); posting, correcting (issue 0006) and
    # reversing an entry; posting a batch, applied whole or refused whole; loading a statement and replacing one
    # loaded wrongly; the four things that can be said about a line (issue 0004); registering a document and
    # filing it against an entry (docs/design/decisions.md); opening and withdrawing "booked, awaiting a
    # document" (filing the document is what meets it, so there is no third); recording and resolving
    # unrecovered tax; and running an export.  Every read stays a GET, and a verb added without a decision to
    # add it fails here.
    mutating = {(p, v) for p, ops in doc["paths"].items() for v in ops if v in {"post", "put", "patch", "delete"}}
    assert mutating == {# the one not relative to an entity: a database file has to exist before there is an
                        # entity to address, so the creation is on the collection
                        ("/v1/entities", "post"),
                        ("/v1/entities/{entity_id}/accounts", "post"),
                        ("/v1/entities/{entity_id}/accounts/{account_id}/archive", "post"),
                        ("/v1/entities/{entity_id}/accounts/{account_id}/restore", "post"),
                        ("/v1/entities/{entity_id}/entries/{entry_id}/correct", "post"),
                        ("/v1/entities/{entity_id}/close", "post"),
                        ("/v1/entities/{entity_id}/reopen", "post"),
                        ("/v1/entities/{entity_id}/entries", "post"),
                        ("/v1/entities/{entity_id}/batches", "post"),
                        ("/v1/entities/{entity_id}/statements", "post"),
                        ("/v1/entities/{entity_id}/lines/{line_id}/book", "post"),
                        ("/v1/entities/{entity_id}/lines/{line_id}/link", "post"),
                        ("/v1/entities/{entity_id}/lines/{line_id}/ignore", "post"),
                        ("/v1/entities/{entity_id}/lines/{line_id}/reexplain", "post"),
                        ("/v1/entities/{entity_id}/entries/{entry_id}/reverse", "post"),
                        ("/v1/entities/{entity_id}/statements/{statement_id}/replace", "post"),
                        ("/v1/entities/{entity_id}/documents", "post"),
                        ("/v1/entities/{entity_id}/entries/{entry_id}/documents", "post"),
                        ("/v1/entities/{entity_id}/entries/{entry_id}/awaiting-document", "post"),
                        ("/v1/entities/{entity_id}/entries/{entry_id}/awaiting-document/withdraw", "post"),
                        ("/v1/entities/{entity_id}/entries/{entry_id}/unrecovered-tax", "post"),
                        ("/v1/entities/{entity_id}/unrecovered-tax/{record_id}/resolve", "post"),
                        ("/v1/entities/{entity_id}/exports", "post")}


def test_openapi_documents_amounts_as_strings_and_never_as_numbers(api_inc):
    schemas = api_inc.client.get("/openapi.json").json()["components"]["schemas"]
    micros_fields = [(name, field, spec) for name, model in schemas.items()
                     for field, spec in model.get("properties", {}).items() if field.endswith("_micros")]
    assert micros_fields, "no amount field is published at all"
    for name, field, spec in micros_fields:
        flat = json.dumps(spec)
        assert '"number"' not in flat and '"integer"' not in flat, f"{name}.{field} is documented as a number"
        assert '"string"' in flat, f"{name}.{field} is not documented as a string"


def test_openapi_publishes_the_limits_the_service_actually_enforces(api_inc):
    doc = api_inc.client.get("/openapi.json").json()
    description = doc["info"]["description"]
    caps = api_inc.json("/capabilities")["limits"]
    assert caps["default_limit"] == api_inc.config.default_limit == 50
    assert caps["max_limit"] == api_inc.config.max_limit == 200
    assert (caps["event_default_limit"], caps["event_max_limit"]) == (100, 500)
    for number in (caps["max_limit"], caps["event_max_limit"]):
        assert str(number) in description
    # The published maximum is the enforced one.
    assert api_inc.get(f"/accounts?limit={caps['max_limit']}").status_code == 200
    over = api_inc.get(f"/accounts?limit={caps['max_limit'] + 1}")
    assert over.status_code == 422 and err(over)["details"][0]["maximum"] == caps["max_limit"]


def test_health_is_unauthenticated_and_readiness_covers_migrations_writer_verifier_and_cursor_keys(api_inc):
    assert api_inc.client.get("/healthz").json() == {"status": "ok", "service": "haddon", "api_version": "v1"}
    ready = api_inc.client.get("/readyz")
    assert ready.status_code == 200, ready.text
    names = {c["name"].split(":")[0] for c in ready.json()["checks"]}
    assert names == {"migrations", "writer", "verifier", "cursors"}
    assert all(c["ok"] for c in ready.json()["checks"]), ready.json()
    verifier = next(c for c in ready.json()["checks"] if c["name"] == "verifier")
    assert "1.52.1" in verifier["detail"]  # the pinned build, not whatever hledger is on PATH


def test_readyz_reports_not_ready_when_a_configured_file_is_not_migrated(fast_dir, api_inc):
    empty = fast_dir / "never-migrated.db"
    empty.write_bytes(b"")
    _, client = build_api({"missing": empty})
    with client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert any(not c["ok"] for c in body["checks"])


# Credentials and entity addressing
@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer ", "Basic abc", "token-under-test",
                                    "Bearer not-a-real-token"])
def test_a_request_without_a_usable_credential_is_401(api_inc, header):
    headers = {} if header is None else {"Authorization": header}
    response = api_inc.client.get(f"{api_inc.base}/accounts", headers=headers)
    assert response.status_code == 401
    assert err(response)["code"] == "unauthenticated"


def test_a_credential_scoped_to_another_entity_is_403_not_404(api_inc):
    response = api_inc.get("/accounts", token=OTHER_TOKEN)
    assert response.status_code == 403
    detail = err(response)["details"][0]
    assert (err(response)["code"], detail["reason"]) == ("forbidden", "entity_not_in_scope")
    # positive control: the same path with the right credential
    assert api_inc.get("/accounts").status_code == 200


def test_an_entity_this_service_does_not_serve_is_404(api_inc):
    response = api_inc.client.get("/v1/entities/no-such-entity/accounts", headers=api_inc.headers)
    assert response.status_code == 404 and err(response)["code"] == "not_found"


def test_the_entity_listing_shows_only_what_the_credential_is_scoped_to(api_two_entities):
    inc, bv = api_two_entities
    visible = [e["entity_id"] for e in inc.json("/v1/entities")["items"]]
    assert visible == [inc.entity_id]  # the process serves two; this credential sees one
    assert inc.get(f"/v1/entities/{bv.entity_id}/accounts").status_code == 403


def test_the_all_entities_scope_is_a_value_a_credential_holds_not_a_missing_check(api_two_entities):
    """``"*"`` means every entity this service is configured with, now and after one is added.

    The other way to implement "may touch everything" is no check at all, and the two look the same from
    outside until the day a scope is meant to bite.  Here one process serves two entities: the wildcard
    credential reaches and sees both, and the enumerated one beside it still reaches exactly what it names.
    """
    from haddon.api.config import ALL_ENTITIES, Credential
    from haddon.api.security import Principal, authenticate

    inc, bv = api_two_entities
    everything = Credential("t", "agent:everything", (ALL_ENTITIES,))
    assert everything.may_touch(inc.entity_id) and everything.may_touch("an-entity-created-tomorrow")
    principal = Principal(actor=everything.actor, entities=everything.entities)
    assert principal.visible([inc.entity_id, bv.entity_id]) == [inc.entity_id, bv.entity_id]
    principal.require("an-entity-created-tomorrow")  # does not raise: the scope is not a list to be added to

    config = inc.config.__class__(entities=dict(inc.config.entities), credentials=(everything,))
    assert authenticate(config, "Bearer t").entities == (ALL_ENTITIES,)
    # The enumerated scope beside it is unaffected: a wildcard credential existing does not widen another.
    only_inc = Principal(actor="agent:one-book", entities=(inc.entity_id,))
    assert only_inc.visible([inc.entity_id, bv.entity_id]) == [inc.entity_id]
    with pytest.raises(HaddonError) as raised:
        only_inc.require(bv.entity_id)
    assert raised.value.code == "forbidden"


def ids(api, table: str) -> list[str]:
    con = api.raw()
    try:
        return [r[0] for r in con.execute(f"SELECT id FROM {table} ORDER BY id")]
    finally:
        con.close()


def only_in(this, other, table: str) -> str:
    """An id present in ``this`` entity's file and absent from ``other``'s."""
    unique = sorted(set(ids(this, table)) - set(ids(other, table)))
    assert unique, f"{table}: the two entities share every id; the isolation test cannot be built"
    return unique[-1]


def test_an_id_is_resolved_inside_its_own_entity_even_when_the_id_exists_elsewhere(api_two_entities):
    """The importer numbers entries per entity, so both files hold ``e-2025-000001``.  Asking Inc for it must
    return Inc's entry: resolution is within the entity, not by id alone."""
    inc, bv = api_two_entities
    shared = "e-2025-000001"
    assert inc.scalar("SELECT count(*) FROM entries WHERE id=?", (shared,)) == 1
    assert bv.scalar("SELECT count(*) FROM entries WHERE id=?", (shared,)) == 1
    mine = inc.json(f"/entries/{shared}")["entry"]
    assert mine["narration"] == inc.scalar("SELECT narration FROM entries WHERE id=?", (shared,))
    assert [p["amount_micros"] for p in mine["postings"]] == \
        [str(a) for (a,) in _amounts(inc, shared)]
    assert mine["postings"][0]["commodity"] == "USD"  # Inc's book, not the B.V.'s EUR one


def _amounts(api, entry_id):
    con = api.raw()
    try:
        return con.execute("SELECT amount_micros FROM postings WHERE entry_id=? ORDER BY seq", (entry_id,)).fetchall()
    finally:
        con.close()


def test_an_id_that_belongs_only_to_another_entity_is_not_found_here(api_two_entities):
    """The leak this prevents: answering "that exists, but not for you" for a real id of another entity."""
    inc, bv = api_two_entities
    bv_account = only_in(bv, inc, "accounts")        # the B.V. chart has accounts Inc does not
    inc_entry = only_in(inc, bv, "entries")          # Inc's books run further than the B.V.'s
    missing_here = inc.get(f"/accounts/{bv_account}")
    assert missing_here.status_code == 404 and err(missing_here)["code"] == "not_found"
    missing_there = bv.get(f"/entries/{inc_entry}", token=SECOND_TOKEN)
    assert missing_there.status_code == 404 and err(missing_there)["code"] == "not_found"
    # Positive controls: each id resolves for the entity that owns it.
    assert inc.get(f"/entries/{inc_entry}").status_code == 200
    assert bv.get(f"/accounts/{bv_account}", token=SECOND_TOKEN).status_code == 200


def test_a_file_holding_a_different_entity_is_refused_rather_than_served(fast_dir, imported_inc):
    """One file is one entity: a configuration that points the wrong id at a file must not serve it."""
    import shutil
    path = fast_dir / "misconfigured.db"
    shutil.copy(imported_inc.path, path)
    _, client = build_api({"not-the-entity-in-this-file": path})
    with client:
        response = client.get("/v1/entities/not-the-entity-in-this-file/accounts",
                              headers={"Authorization": "Bearer token-under-test"})
        listing = client.get("/v1/entities", headers={"Authorization": "Bearer token-under-test"})
    assert response.status_code == 500 and err(response)["code"] == "internal_error"
    assert "example-inc" not in response.text  # and it does not name what it actually found
    assert listing.json()["items"] == []


# The error envelope
def test_a_domain_refusal_keeps_its_own_code_status_and_details(api_inc):
    """A 422 from the reports layer must not arrive as a 500 with the reason thrown away."""
    response = api_inc.get("/reports/income-statement?from=2025-12-31&to=2025-01-01")
    assert response.status_code == 422
    body = err(response)
    assert body["code"] == "validation"
    assert body["details"][0]["reason"] == "range_order"
    assert body["details"][0]["start"] == "2025-12-31" and body["details"][0]["end"] == "2025-01-01"
    assert body["request_id"].startswith("req-") and body["retryable"] is False


def test_an_unexpected_exception_is_a_500_that_leaks_nothing(api_inc, monkeypatch):
    from fastapi.testclient import TestClient

    from haddon.api.routes import accounts as accounts_route

    secret = "ORA-600 /var/lib/haddon/secret-path.db token=hunter2"

    def boom(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(accounts_route.views, "account", boom)
    with TestClient(api_inc.client.app, raise_server_exceptions=False) as client:
        response = client.get(f"{api_inc.base}/accounts", headers=api_inc.headers)
    assert response.status_code == 500
    body = err(response)
    assert body["code"] == "internal_error"
    assert secret not in response.text and "RuntimeError" not in response.text
    assert body["request_id"].startswith("req-")


def test_every_refusal_uses_one_envelope_shape(api_inc):
    cases = [(api_inc.client.get(f"{api_inc.base}/accounts"), 401),
             (api_inc.get("/accounts", token=OTHER_TOKEN), 403),
             (api_inc.get("/accounts/nope"), 404),
             (api_inc.get("/reports/income-statement?from=2025-12-31&to=2025-01-01"), 422),
             (api_inc.get("/events?since=10000000"), 409),
             (api_inc.client.get("/v1/entities/x/y/z", headers=api_inc.headers), 404),
             (api_inc.client.request("DELETE", f"{api_inc.base}/accounts",
                                     headers={**api_inc.headers, KEY_HEADER: "envelope-405"}), 405)]
    for response, status in cases:
        assert response.status_code == status, (response.url, response.status_code)
        body = err(response)
        assert set(body) >= {"code", "message", "retryable", "details"}
        assert isinstance(body["details"], list)


def test_a_verb_a_path_does_not_serve_is_a_client_error_and_not_this_service_faulting(api_inc):
    """405 is the caller's mistake, so it says so: ``method_not_allowed``, not ``internal_error``.

    Starlette raises its own ``HTTPException`` for a routed path addressed with the wrong verb.  Rendering that
    with a 500's code would tell the caller this service had broken, when they used the wrong verb and there is
    nothing to retry or report.
    """
    response = api_inc.client.request("DELETE", f"{api_inc.base}/accounts",
                                      headers={**api_inc.headers, KEY_HEADER: "wrong-verb"})
    assert response.status_code == 405
    body = err(response)
    assert body["code"] == "method_not_allowed" and body["retryable"] is False
    # The methods it does serve, in the header the HTTP spec asks for and in the body a JSON client reads.
    assert "GET" in response.headers["Allow"] and "POST" in response.headers["Allow"]
    detail = body["details"][0]
    assert detail["method"] == "DELETE" and detail["path"].endswith("/accounts")
    assert set(detail["allowed"]) == {m.strip() for m in response.headers["Allow"].split(",")}
    assert "GET" in detail["allowed"]


def test_the_allowed_methods_are_the_whole_set_and_not_the_first_route_that_matched(api_inc):
    """``/accounts`` is served by two routes.  Starlette's own ``Allow`` names only the first one whose path
    matched, which would tell a caller ``POST`` is unavailable on the path that takes it.  The set is unioned
    over the published routes instead.

    ``/healthz`` is the other end of the same rule: one route, one method.  The reviewer bundle's catch-all,
    which claims every path the API did not, must not add its own ``GET`` to either answer.
    """
    accounts = api_inc.client.request("DELETE", f"{api_inc.base}/accounts",
                                      headers={**api_inc.headers, KEY_HEADER: "all-verbs"})
    assert sorted(err(accounts)["details"][0]["allowed"]) == ["GET", "POST"]
    health = api_inc.client.request("POST", "/healthz")   # outside /v1: no idempotency key is involved
    assert health.status_code == 405
    assert err(health)["code"] == "method_not_allowed" and err(health)["details"][0]["allowed"] == ["GET"]


def test_a_path_that_is_not_served_at_all_is_still_a_plain_404(api_inc):
    """The status-to-code mapping must not turn "no such path" into "wrong verb", whatever verb asks.

    The reviewer bundle's fallback pattern matches every path, so Starlette finds a path match and raises 405
    for a write to a path nothing serves.  But there is no resource, which is what a ``GET`` to the same path
    already answers.  It would also be a 405 with no methods to name in ``Allow``, which
    ``test_every_405_carries_the_complete_allow_it_promises`` forbids.
    """
    for i, method in enumerate(("GET", "POST", "PUT", "PATCH", "DELETE")):
        response = api_inc.client.request(method, "/v1/entities/x/y/z",
                                          headers={**api_inc.headers, KEY_HEADER: f"no-such-path-{i}"})
        assert response.status_code == 404, (method, response.status_code, response.text)
        assert err(response)["code"] == "not_found", method
        assert "Allow" not in response.headers, method
    # Outside the API prefixes the fallback does answer a browser navigation, but a navigation is not a
    # resource, so a write there is still a path this service does not serve.
    assert api_inc.client.request("POST", "/not-a-path-this-service-serves").status_code == 404


def test_every_405_carries_the_complete_allow_it_promises(api_inc):
    """RFC 9110 15.5.6 makes ``Allow`` mandatory on a 405, and the union is what makes it complete.

    ``/openapi.json`` is why ``include_in_schema`` is the wrong test for "is this a fallback": it is a real
    route this app serves and does not publish in its own schema.  Skipping every unpublished route would leave
    its ``Allow`` empty, so what is skipped is the fallback itself, by the mark it carries.
    """
    cases = [(f"{api_inc.base}/accounts", ["GET", "POST"]), ("/healthz", ["GET"]),
             ("/openapi.json", ["GET", "HEAD"])]
    for i, (path, expected) in enumerate(cases):
        response = api_inc.client.request("DELETE", path, headers={**api_inc.headers, KEY_HEADER: f"allow-{i}"})
        assert response.status_code == 405, (path, response.status_code, response.text)
        assert sorted(err(response)["details"][0]["allowed"]) == expected, path
        assert sorted(m.strip() for m in response.headers["Allow"].split(",")) == expected, path


def test_the_reviewer_bundle_is_left_out_of_allow_without_being_published(api_inc):
    """The fallback must not reach ``Allow``, and the fix must not put it in the contract to keep it out.

    Marking the endpoint is what excludes it.  Making it ``include_in_schema=True`` would do the same job and
    publish a UI route as part of the API.
    """
    paths = set(api_inc.client.get("/openapi.json").json()["paths"])
    assert not [p for p in paths if "{path" in p], paths
    assert "/healthz" in paths and f"/v1/entities/{{entity_id}}/accounts" in paths
    # The fallback serves GET on any unclaimed path, and that GET is still not evidence about /healthz.
    assert err(api_inc.client.request("PUT", "/healthz"))["details"][0]["allowed"] == ["GET"]


# Idempotency
def request_rows(api, key: str) -> list:
    con = api.raw()
    try:
        return con.execute("SELECT caller, method, path, http_status FROM requests WHERE intent_key=?", (key,)).fetchall()
    finally:
        con.close()


def written(api, path: str = ACCOUNT["path"]) -> int:
    return api.scalar("SELECT count(*) FROM accounts WHERE path=?", (path,))


def test_the_same_key_and_the_same_request_replays_the_stored_original(api_inc):
    first = api_inc.post("/accounts", json=ACCOUNT, key="intent-1")
    assert first.status_code == 201 and REPLAYED_HEADER not in first.headers
    second = api_inc.post("/accounts", json=ACCOUNT, key="intent-1")
    assert second.status_code == 201
    assert second.headers[REPLAYED_HEADER] == "true"
    assert second.json() == first.json()          # the stored original body, byte for byte
    assert written(api_inc) == 1                  # and no second account
    assert len(request_rows(api_inc, "intent-1")) == 1


def test_a_reserialised_but_identical_body_still_replays(api_inc):
    """Clients re-serialise on retry; key ordering and whitespace are not a change of intent."""
    api_inc.post("/accounts", json=ACCOUNT, key="intent-2")
    reordered = dict(reversed(list(ACCOUNT.items())))
    again = api_inc.client.post(f"{api_inc.base}/accounts",
                                content=json.dumps(reordered, indent=4).encode(),
                                headers={**api_inc.headers, KEY_HEADER: "intent-2",
                                         "Content-Type": "application/json"})
    assert again.status_code == 201 and again.headers[REPLAYED_HEADER] == "true"
    assert written(api_inc) == 1


def test_the_same_key_with_a_different_request_is_409_idempotency_conflict(api_inc):
    api_inc.post("/accounts", json=ACCOUNT, key="intent-3")
    for changed in (account(path="expenses:spine-other"), account(type="asset"), account(is_cash=True)):
        response = api_inc.post("/accounts", json=changed, key="intent-3")
        assert response.status_code == 409, changed
        assert err(response)["code"] == "idempotency_conflict"
    assert written(api_inc) == 1


def test_the_key_is_scoped_to_the_caller(api_inc):
    """Two credentials are two callers; one cannot replay or block the other's intent key."""
    first = api_inc.post("/accounts", json=ACCOUNT, key="shared-key")
    # A different write: the chart refuses the same path twice before idempotency is consulted, so the second
    # caller writes its own account rather than colliding on the first caller's.
    second = api_inc.post("/accounts", json=account(path="expenses:spine-second-caller"),
                          key="shared-key", token=SECOND_TOKEN)
    assert (first.status_code, second.status_code) == (201, 201)
    assert REPLAYED_HEADER not in second.headers
    assert sorted(r[0] for r in request_rows(api_inc, "shared-key")) == [API_ACTOR, SECOND_ACTOR]
    assert written(api_inc) == 1 and written(api_inc, "expenses:spine-second-caller") == 1


def test_a_mutating_request_without_the_header_is_refused_before_it_reaches_the_writer(api_inc):
    response = api_inc.post("/accounts", json=ACCOUNT)
    assert response.status_code == 422
    assert err(response)["details"][0]["reason"] == "idempotency_key_required"
    assert written(api_inc) == 0
    assert api_inc.scalar("SELECT count(*) FROM requests") == 0


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_idempotency_gate_is_ahead_of_routing_so_no_mutating_route_can_miss_it(api_inc, verb):
    """The gate is a middleware, not a per-route dependency, so it answers before routing does.

    A mutating verb on a read-only path therefore reports the missing key rather than 405.  That is what makes a
    mutating route added later covered by default instead of by remembering.

    The path must have no mutating route on it, or the second half proves nothing.  ``/capabilities`` is a
    report of what a credential may do and could not sensibly take a write.
    """
    response = api_inc.client.request(verb, f"{api_inc.base}/capabilities", headers=api_inc.headers)
    assert response.status_code == 422, (verb, response.status_code)
    assert err(response)["details"][0]["reason"] == "idempotency_key_required"
    # With the key supplied the request is routed normally, and there is no such route.
    routed = api_inc.client.request(verb, f"{api_inc.base}/capabilities",
                                    headers={**api_inc.headers, KEY_HEADER: "k"})
    assert routed.status_code == 405


def test_the_request_result_commits_with_the_mutation_and_not_without_it(api_inc):
    """Both rows or neither: a refused write leaves no request row, and does not burn the caller's key."""
    failing = api_inc.post("/accounts", json=account(type="not-an-account-type"), key="intent-4")
    assert failing.status_code == 422
    assert request_rows(api_inc, "intent-4") == []
    assert written(api_inc) == 0

    ok = api_inc.post("/accounts", json=ACCOUNT, key="intent-4")  # the same key still works
    assert ok.status_code == 201
    rows = request_rows(api_inc, "intent-4")
    assert rows == [(API_ACTOR, "POST", f"{api_inc.base}/accounts", 201)]
    assert written(api_inc) == 1
    # The stored body is the one that was sent back.
    stored = json.loads(api_inc.scalar("SELECT response FROM requests WHERE intent_key=?", ("intent-4",)))
    assert stored == ok.json()


def test_the_key_binds_the_path_so_one_key_cannot_carry_two_intents(api_inc, api_two_entities):
    """The hash binds method, path, body, caller and entity; changing the entity changes the intent."""
    inc, bv = api_two_entities
    inc.post("/accounts", json=ACCOUNT, key="cross")
    # Same caller, same body, different entity: a new intent in a different file, not a replay.
    assert inc.scalar("SELECT count(*) FROM requests WHERE intent_key='cross'") == 1
    assert bv.scalar("SELECT count(*) FROM requests WHERE intent_key='cross'") == 0


# Pagination and cursors
def test_a_cursor_is_opaque_and_tamper_evident(api_inc):
    page = api_inc.json("/accounts?limit=5")
    token = page["next_cursor"]
    assert token is not None
    # It is not readable as an id, an offset or JSON.
    with pytest.raises(Exception):
        json.loads(token)
    assert "account" not in token and "limit" not in token
    payload = cursors.decode(token, CURSOR_KEYS)
    assert set(payload) >= {"e", "k", "q", "s", "w", "a", "p", "kid"}
    # Edited by anyone but this service, it is refused rather than honoured.
    forged = cursors.encode({k: v for k, v in payload.items() if k not in ("v", "kid")}
                            | {"a": ["1970-01-01T00:00:00Z", ""]}, CursorKeyring.of("not-the-secret"))
    response = api_inc.get(f"/accounts?limit=5&cursor={forged}")
    assert response.status_code == 422 and err(response)["details"][0]["reason"] == "cursor_invalid"
    for junk in ("", "abc", "abc.def", token[:-2], token.replace(".", ".x")):
        assert api_inc.get(f"/accounts?limit=5&cursor={junk}").status_code == 422


def test_a_cursor_cannot_be_carried_to_another_query_or_another_entity(api_inc, api_two_entities):
    page = api_inc.json("/accounts?limit=5")
    token = page["next_cursor"]
    # different filters -> different binding
    response = api_inc.get(f"/accounts?limit=5&type=expense&cursor={token}")
    assert response.status_code == 422 and err(response)["details"][0]["reason"] == "cursor_mismatch"
    # different collection
    assert api_inc.get(f"/entries?limit=5&cursor={token}").status_code == 422
    # different entity, same secret
    inc, bv = api_two_entities
    inc_cursor = inc.json("/accounts?limit=5")["next_cursor"]
    assert bv.get(f"/accounts?limit=5&cursor={inc_cursor}", token=SECOND_TOKEN).status_code in (403, 422)


def test_pages_do_not_mix_and_cover_the_collection_exactly_once(api_inc):
    everything = [a["id"] for a in api_inc.json("/accounts?limit=200")["items"]]
    assert len(everything) == api_inc.scalar("SELECT count(*) FROM accounts")
    walked, cursor, pages = [], None, 0
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        page = api_inc.json(f"/accounts?limit=7{suffix}")
        walked.extend(a["id"] for a in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 100
    assert walked == everything
    assert len(set(walked)) == len(walked)
    assert pages > 1


def test_a_row_appended_mid_listing_does_not_appear_and_does_not_shift_the_pages(api_inc):
    """Pagination survives a concurrent append: it neither refuses it nor absorbs it.

    The row is written with a ``created_at`` far beyond anything in the book, so it sorts unambiguously after the
    watermark page one recorded.  It must be invisible to the walk in progress and visible to the next one.
    """
    from haddon.repository import entries as en

    everything_in_order = walk_all(api_inc, "/entries", limit=200)
    first = api_inc.json("/entries?limit=100")
    assert first["next_cursor"] is not None

    accounts = [a["id"] for a in api_inc.json("/accounts?type=expense&limit=2")["items"]]
    engine = api_inc.engine()
    try:
        with write_transaction(engine) as conn:
            ctx = WriteCtx.open(conn, "agent:concurrent-writer", now="2099-01-01T00:00:00Z")
            appended = en.post_entry(ctx, dt="2025-07-01", narration="written while a listing was in flight",
                                     origin="manual",
                                     legs=[en.Leg(accounts[0], "USD", "10000"),
                                           en.Leg(accounts[1], "USD", "-10000")])
    finally:
        engine.dispose()

    walked, cursor, pages = [e["id"] for e in first["items"]], first["next_cursor"], 1
    while cursor is not None:                              # walk to the very end: the new row sorts last
        page = api_inc.json(f"/entries?limit=100&cursor={cursor}")
        walked.extend(e["id"] for e in page["items"])
        cursor = page["next_cursor"]
        pages += 1
        assert pages < 100
    assert appended.id not in walked                       # the in-flight walk does not absorb it
    assert walked == everything_in_order                   # and nothing it already showed shifted
    assert appended.id in [e["id"] for e in api_inc.json("/entries?limit=200&status=posted"
                                                         "&from=2025-07-01&to=2025-07-01")["items"]]


def test_a_cursor_whose_snapshot_is_gone_is_409_cursor_expired_with_a_resync_link(api_inc):
    """A chart page is only meaningful at one state: archive an account and the in-flight cursor is dead."""
    first = api_inc.json("/accounts?limit=5")
    cursor = first["next_cursor"]
    assert api_inc.get(f"/accounts?limit=5&cursor={cursor}").status_code == 200  # positive control

    target = first["items"][0]
    engine = api_inc.engine()
    try:
        with write_transaction(engine) as conn:
            ctx = WriteCtx.open(conn, "agent:another-writer")
            pv.archive_account(ctx, account_id=target["id"], expected_version=target["version"])
    finally:
        engine.dispose()

    response = api_inc.get(f"/accounts?limit=5&cursor={cursor}")
    assert response.status_code == 409
    body = err(response)
    assert body["code"] == "cursor_expired"
    assert body["details"][0]["reason"] == "snapshot_changed"
    assert body["next_action"] == {"action": "resync", "url": f"{api_inc.base}/accounts"}
    # The resync link works.
    assert api_inc.get(body["next_action"]["url"]).status_code == 200


def test_a_cursor_outlives_the_process_that_issued_it(api_inc):
    """What a configured signing key buys, and why it is configuration rather than a default.

    A key invented per process makes every open cursor invalid the moment the service restarts (a deploy, a
    crash, a systemd ``Restart=always``), and the caller sees an unexplained ``cursor_invalid`` in the middle of
    a walk it did nothing wrong in.  A second app over the same files and the same configured keyring is that
    restart.
    """
    token = api_inc.json("/accounts?limit=5")["next_cursor"]
    assert token is not None
    _, restarted = build_api({api_inc.entity_id: api_inc.path})
    with restarted as client:
        response = client.get(f"{api_inc.base}/accounts?limit=5&cursor={token}", headers=api_inc.headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"], "the restarted process served the page rather than refusing the cursor"


def test_without_a_configured_key_a_restart_does_invalidate_every_open_cursor(api_inc):
    """Without a configured keyring, cursors stop working at a restart for no visible reason.

    Kept as a test so the cost of not configuring one is written down.

    ``/readyz`` is where a deployment finds this out before its callers do."""
    _, first = build_api({api_inc.entity_id: api_inc.path}, cursor_keys=CursorKeyring.ephemeral())
    with first as client:
        token = client.get(f"{api_inc.base}/accounts?limit=5", headers=api_inc.headers).json()["next_cursor"]
    _, second = build_api({api_inc.entity_id: api_inc.path}, cursor_keys=CursorKeyring.ephemeral())
    with second as client:
        response = client.get(f"{api_inc.base}/accounts?limit=5&cursor={token}", headers=api_inc.headers)
        ready = client.get("/readyz")
    assert response.status_code == 422 and err(response)["details"][0]["reason"] == "cursor_invalid"
    assert ready.status_code == 503
    assert next(c for c in ready.json()["checks"] if c["name"] == "cursors")["ok"] is False


def test_a_key_is_rotated_by_prepending_it_and_the_cursors_the_old_one_signed_keep_working(api_inc):
    """Rotation without a flag day: the new key signs, both keys verify, and the old key is dropped later.

    Dropping it is what makes retiring a key mean something: a cursor naming a key this service no longer holds
    is refused rather than honoured on the strength of its own claim about which key signed it.
    """
    old, new = CursorKey("2026-06", "the-old-secret"), CursorKey("2026-09", "the-new-secret")
    entities = {api_inc.entity_id: api_inc.path}

    _, before = build_api(entities, cursor_keys=CursorKeyring((old,)))
    with before as client:
        signed_by_old = client.get(f"{api_inc.base}/accounts?limit=5", headers=api_inc.headers).json()["next_cursor"]
    assert cursors.decode(signed_by_old, CursorKeyring((old,)))["kid"] == "2026-06"

    _, rotated = build_api(entities, cursor_keys=CursorKeyring((new, old)))
    with rotated as client:
        carried = client.get(f"{api_inc.base}/accounts?limit=5&cursor={signed_by_old}", headers=api_inc.headers)
        fresh = client.get(f"{api_inc.base}/accounts?limit=5", headers=api_inc.headers).json()["next_cursor"]
    assert carried.status_code == 200, carried.text
    assert cursors.decode(fresh, CursorKeyring((new,)))["kid"] == "2026-09", "the newest key signs"

    _, retired = build_api(entities, cursor_keys=CursorKeyring((new,)))
    with retired as client:
        gone = client.get(f"{api_inc.base}/accounts?limit=5&cursor={signed_by_old}", headers=api_inc.headers)
        still_good = client.get(f"{api_inc.base}/accounts?limit=5&cursor={fresh}", headers=api_inc.headers)
    assert gone.status_code == 422 and err(gone)["details"][0]["reason"] == "cursor_invalid"
    assert still_good.status_code == 200, still_good.text


@pytest.mark.parametrize("raw, path", [
    ({"cursor_keys": []}, "cursor_keys"),
    ({"cursor_keys": "a-secret"}, "cursor_keys"),
    ({"cursor_keys": [{"secret": "s"}]}, "cursor_keys.0.id"),
    ({"cursor_keys": [{"id": "k"}]}, "cursor_keys.0.secret"),
    ({"cursor_keys": [{"id": "k", "secret": "a"}, {"id": "k", "secret": "b"}]}, "cursor_keys.1.id"),
    ({"cursor_keys": [{"id": "k", "secret": "a"}], "cursor_secret": "b"}, "cursor_keys"),
    ({"cursor_secret": ""}, "cursor_secret"),
])
def test_a_keyring_that_does_not_say_what_it_means_is_refused_at_startup(tmp_path, raw, path):
    """Loudly, and naming the field.  A keyring that quietly failed to parse would leave the service on the
    key it invents for itself: working today, and invalidating every cursor at the next restart."""
    from haddon.api.config import config_from_dict

    with pytest.raises(HaddonError) as raised:
        config_from_dict({"entities": {"e": str(tmp_path / "e.db")}, **raw})
    assert raised.value.code == "validation"
    assert raised.value.details[0]["path"] == path, raised.value.message


def test_the_configured_keyring_is_read_in_the_order_it_is_written(tmp_path):
    from haddon.api.config import config_from_dict

    entities = {"e": str(tmp_path / "e.db")}
    keyring = config_from_dict({"entities": entities,
                                "cursor_keys": [{"id": "new", "secret": "n"},
                                                {"id": "old", "secret": "o"}]}).cursor_keys
    assert [k.id for k in keyring.keys] == ["new", "old"] and keyring.active.id == "new"
    assert keyring.durable and keyring.by_id("old").secret == "o" and keyring.by_id("gone") is None
    # The one-key spelling is the same thing, and an absent one is the key the service invents for itself.
    assert config_from_dict({"entities": entities, "cursor_secret": "s"}).cursor_keys.durable
    assert not config_from_dict({"entities": entities}).cursor_keys.durable


def test_a_cursor_payload_cannot_quietly_shadow_the_envelope_fields(api_inc):
    """``kind`` is ``k`` and the key id is ``kid``.  A payload carrying either of the envelope's own names would
    be signed with one key and verified against whatever it claimed instead."""
    with pytest.raises(ValueError):
        cursors.encode({"kid": "someone-elses-key", "e": "x"}, CURSOR_KEYS)
    with pytest.raises(ValueError):
        cursors.encode({"v": 99}, CURSOR_KEYS)


def test_limits_are_defaulted_bounded_and_refused_rather_than_clamped(api_inc):
    assert len(api_inc.json("/accounts")["items"]) == 50          # the published default
    assert len(api_inc.json("/accounts?limit=3")["items"]) == 3
    for bad in ("0", "-1", "201", "abc", "1.5"):
        assert api_inc.get(f"/accounts?limit={bad}").status_code == 422, bad
    assert api_inc.get("/events?limit=500").status_code == 200     # events have their own maximum
    assert api_inc.get("/events?limit=501").status_code == 422


def test_every_list_reports_the_snapshot_it_was_read_at(api_inc):
    water = api_inc.scalar("SELECT coalesce(max(seq), 0) FROM events")
    for path in ("/accounts", "/entries", "/statements"):
        assert api_inc.json(path)["snapshot_seq"] == water, path


# Events
def test_events_are_ordered_and_carry_a_continuation_point(api_inc):
    page = api_inc.json("/events?limit=2")
    assert [e["seq"] for e in page["items"]] == sorted(e["seq"] for e in page["items"])
    assert page["next_since"] == page["items"][-1]["seq"]
    assert page["high_water"] == api_inc.scalar("SELECT max(seq) FROM events")
    following = api_inc.json(f"/events?since={page['next_since']}")
    assert all(e["seq"] > page["next_since"] for e in following["items"])


def test_a_sequence_this_log_does_not_hold_is_409_event_gap_with_a_resync_url(api_inc):
    water = api_inc.scalar("SELECT max(seq) FROM events")
    assert api_inc.get(f"/events?since={water}").status_code == 200      # positive control
    response = api_inc.get(f"/events?since={water + 1}")
    assert response.status_code == 409
    body = err(response)
    assert body["code"] == "event_gap"
    assert body["details"][0]["reason"] == "beyond_high_water"
    assert body["next_action"] == {"action": "resync", "url": f"{api_inc.base}/events?since=0"}
    assert api_inc.get(body["next_action"]["url"]).status_code == 200


# The wire
def walk(value, path="$"):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, value


def test_no_response_body_anywhere_carries_an_amount_as_a_number(api_inc):
    api_inc.post("/accounts", json=ACCOUNT, key="wire")
    account = api_inc.json("/accounts?limit=1")["items"][0]["id"]
    entry = api_inc.scalar("SELECT id FROM entries ORDER BY id LIMIT 1")
    paths = ["/capabilities", "/accounts", f"/accounts/{account}",
             f"/accounts/{account}/register?from=2025-01-01&to=2025-12-31",
             "/entries?limit=5", f"/entries/{entry}", f"/entries/{entry}/events", "/events",
             "/reports/trial-balance?as_of=2025-12-31", "/reports/balance-sheet?as_of=2025-12-31",
             "/reports/income-statement?from=2025-01-01&to=2025-12-31",
             "/reports/statements?from=2025-01-01&to=2025-12-31", "/statements", "/v1/entities"]
    seen = 0
    for path in paths:
        body = api_inc.json(path)
        for where, value in walk(body, path):
            assert not isinstance(value, float), f"{where} is a float: {value!r}"
            if where.endswith("_micros") or ".opening_micros." in where or ".totals." in where \
                    or ".net_micros." in where:
                # An absent amount is ``null``, never a number: the shaped statements use null for a subtotal a
                # single-account group does not have and for a comparative that is not shown.  What is refused
                # is a JSON number, which is what this sweep exists to catch.
                assert value is None or isinstance(value, str), f"{where} is {type(value).__name__}: {value!r}"
                seen += 1
    assert seen > 50, "the sweep did not actually reach any amounts"


@pytest.mark.parametrize("payload,reason", [
    ({"amount_micros": -12000000}, "string_required"),
    ({"amount_micros": 12.5}, "string_required"),
    ({"legs": [{"amount_micros": 1}]}, "string_required"),
    ({"amount_micros": "012"}, "not_integer_string"),
    ({"amount_micros": "-0"}, "not_integer_string"),
    ({"rate": 1.5}, "float_rejected"),
    ({"nested": {"deep": [0.1]}}, "float_rejected"),
])
def test_a_body_carrying_a_json_number_amount_is_refused(api_inc, payload, reason):
    response = api_inc.post("/accounts", json={**ACCOUNT, **payload}, key=f"num-{reason}-{len(str(payload))}")
    assert response.status_code == 422
    assert err(response)["details"][0]["reason"] == reason
    assert written(api_inc) == 0
    # Positive control: the same body without the number is accepted, so the refusal is about the number.
    ok = api_inc.post("/accounts", json=ACCOUNT, key="num-ok")
    assert ok.status_code == 201


def test_the_actor_reaches_the_record_verbatim_and_is_never_parsed(api_inc):
    api_inc.post("/accounts", json=ACCOUNT, key="actor")
    assert api_inc.scalar("SELECT created_by FROM accounts WHERE path=?", (ACCOUNT["path"],)) == API_ACTOR
    assert api_inc.scalar("SELECT created_by FROM account_commodities WHERE account_id="
                          "(SELECT id FROM accounts WHERE path=?)", (ACCOUNT["path"],)) == API_ACTOR
    assert api_inc.json("/capabilities")["actor_recorded_as"] == API_ACTOR


def test_all_writes_go_through_the_repository_and_leave_the_triggers_standing(api_inc):
    """The API layer issues no raw SQL writes: the only tables it grows are the ones the repository writes."""
    before = table_counts_of(api_inc)
    api_inc.post("/accounts", json=ACCOUNT, key="through-repo")
    after = table_counts_of(api_inc)
    grew = {name for name in after if after[name] > before[name]}
    # ``POST /accounts`` is the ``create_account`` command (issue 0006), so it grows the chart's two tables, the
    # request record and the audit log.
    assert grew == {"accounts", "account_commodities", "requests", "events"}
    # The row is exactly what the repository would have written.
    with make_reader(api_inc) as conn:
        row = conn.execute(sa.select(t.accounts).where(t.accounts.c.path == ACCOUNT["path"])).first()
    assert row.created_by == API_ACTOR and row.entity_id == api_inc.entity_id


def table_counts_of(api) -> dict[str, int]:
    con = api.raw()
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


def make_reader(api):
    engine = api.engine()

    class _Reader:
        def __enter__(self):
            self.conn = engine.connect()
            return self.conn

        def __exit__(self, *exc):
            self.conn.close()
            engine.dispose()
            return False

    return _Reader()


# Reads must not take the write lock
def test_a_read_does_not_take_the_write_lock(api_inc):
    """Reads are served on a deferred engine, not the writer's.

    The writer's every transaction begins ``BEGIN IMMEDIATE``.  Serving a GET on it would take an exclusive
    lock, so one open reader would block every other request and a slow report would block the writer.  A
    held-open reader must not stop either."""
    from haddon.db.connection import make_engine

    handle = api_inc.client.app.state.haddon.registry.handle(api_inc.entity_id)
    assert handle.read_engine is not handle.engine, "reads are being served on the writer's engine"

    writer = make_engine(api_inc.path, busy_timeout_ms=200)   # fails fast rather than hanging on the lock
    try:
        with handle.reader() as conn:
            conn.execute(sa.select(sa.func.count()).select_from(t.entries)).scalar_one()
            # The request-shaped read is open and holds no write lock, so a writer can still start.
            with write_transaction(writer) as write_conn:
                write_conn.execute(sa.select(sa.func.count()).select_from(t.entries)).scalar_one()
    finally:
        writer.dispose()

    # The same holds through the app: a held-open reader blocks neither a read nor a write.
    reader_engine = api_inc.read_engine()
    held = reader_engine.connect()
    try:
        held.execute(sa.select(sa.func.count()).select_from(t.entries)).scalar_one()
        assert api_inc.get("/accounts?limit=5").status_code == 200
        assert api_inc.post("/accounts", json=ACCOUNT, key="under-reader").status_code == 201
    finally:
        held.close()
        reader_engine.dispose()


def test_concurrent_reads_are_served_in_parallel(api_inc):
    """Eight readers at once against one SQLite file: with an exclusive lock on every read this deadlocks or
    times out; with a deferred one it simply works."""
    import threading

    failures: list[str] = []

    def hammer():
        try:
            for _ in range(5):
                response = api_inc.get("/reports/trial-balance?as_of=2025-12-31")
                if response.status_code != 200:
                    failures.append(f"{response.status_code}: {response.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            failures.append(repr(exc))

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a reader never finished"
    assert failures == []
