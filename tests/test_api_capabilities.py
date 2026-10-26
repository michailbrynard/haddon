"""Discovery: what ``/capabilities`` says this service can do, and why it cannot drift from what it does.

Issue 0006 (and issue 0008 section A) asks for capabilities derived from the surface the process actually serves
rather than kept in a list beside it.  A hand-kept list goes wrong in both directions, advertising routes that
were deleted and missing routes that were added, and nothing notices because nothing compares the two.

So the tests here are comparisons, not assertions about a literal:

* the derived reads and writes equal the entity-scoped paths in the OpenAPI document, which is what a client
  generates from;
* every mutating route names its command (``operation_id``), so the published command, the OpenAPI operation and
  the application handler are one string rather than three that agree today;
* every mutating route declares a response model for the status it answers with, which is what
  ``idempotency.run`` validates the reply against.

Reads are compared as well as writes.  Issue 0008 section A named that failure: a read route that silently
disappears from discovery is a capability a caller is told it does not have.
"""

from __future__ import annotations

import uuid

import pytest

from haddon.api import surface
from haddon.api.app import create_app
from haddon.api.idempotency import MUTATING


ENTITY_PREFIX = "/v1/entities/{entity_id}"


@pytest.fixture(scope="module")
def app():
    return create_app()


def openapi_of(app):
    return app.openapi()


# The derived surface is the served surface
def test_the_derived_surface_equals_the_openapi_document(app):
    """The comparison a hand-written list could never make.

    Both sides are generated, so this is not two copies agreeing.  ``surface.published`` walks the mounted
    routers and FastAPI builds the document from the same routes, so a discrepancy means one has stopped seeing
    a route the other does.
    """
    reads, writes = surface.published(app)
    derived = {(w.method, w.path) for w in writes} | {(r.method, r.path) for r in reads}
    documented = {(verb.upper(), path[len(ENTITY_PREFIX):])
                  for path, ops in openapi_of(app)["paths"].items() if path.startswith(ENTITY_PREFIX)
                  for verb in ops}
    assert derived == documented


def test_every_write_names_the_command_it_runs(app):
    """A write is a command and its name is its ``operation_id``.

    Without one FastAPI generates an identifier from the function and the path
    (``create_account_v1_entities__entity_id__accounts_post``).  That would be published to agents as the
    command's name and would change whenever the function was renamed.
    """
    _, writes = surface.published(app)
    assert writes, "no write is published at all, which means the walk found nothing"
    for write in writes:
        assert write.command, f"{write.method} {write.path} publishes no command name"
        assert write.command.isidentifier(), write.command
        assert "_v1_entities_" not in write.command, f"{write.command} is FastAPI's generated id, not a name"
    assert len(set(w.command for w in writes)) == len(writes), "two routes publish the same command name"


#: The commands issue 0006 publishes: the journal core's and the reconciliation extension's.  The review and
#: local-tax writes (``register_document``, ``file_document``, ``await_document``, ``withdraw_expectation``,
#: ``record_unrecovered_tax``, ``resolve_unrecovered_tax``) are named and discoverable, but their handlers are
#: repository functions.  Issues 0007 and 0008 own those surfaces.
CORE_COMMANDS = ("initialize_entity", "create_account", "archive_account", "restore_account", "post_entry",
                 "post_batch", "correct_entry", "reverse_entry", "close_period", "reopen_period")
RECONCILIATION_COMMANDS = ("load_statement", "replace_statement", "book_line", "link_line", "ignore_line",
                           "reexplain_line")


def test_every_command_0006_publishes_is_a_function_in_a_command_layer(app):
    """The published name is the handler.

    Issue 0006 asks for commands "backed by the same application handlers regardless of transport".  This checks
    that the name an agent reads resolves to one."""
    from haddon import commands, reconciliation_commands

    for name in CORE_COMMANDS:
        assert callable(getattr(commands, name, None)), f"haddon.commands has no {name}"
    for name in RECONCILIATION_COMMANDS:
        assert callable(getattr(reconciliation_commands, name, None)), f"the extension has no {name}"

    _, writes = surface.published(app)
    _, service_writes = surface.service_published(app)
    published = {w.command for w in writes}
    # ``initialize_entity`` is the one command not relative to an entity (a database file has to exist before
    # there is an entity to address), so it is published on the entity collection rather than inside one.
    assert {w.command for w in service_writes} == {"initialize_entity"}
    assert set(CORE_COMMANDS) - {"initialize_entity"} <= published
    assert set(RECONCILIATION_COMMANDS) <= published


def test_every_write_declares_the_response_model_its_reply_is_checked_against(app):
    """Issue 0006, the fourth gap.

    A write returns a ``JSONResponse``, which FastAPI does not validate.  ``idempotency.run`` reads the model off
    the route it is serving, so a route that declares none would silently go unchecked."""
    routes = surface.entity_routes(app)
    for route in routes:
        for method in sorted(route.methods or ()):
            if method not in MUTATING:
                continue
            declared = {status for status, spec in (route.responses or {}).items()
                        if isinstance(spec, dict) and spec.get("model") is not None and status < 400}
            assert route.status_code in declared, \
                f"{method} {route.path} declares no response model for {route.status_code}"


def test_the_published_reads_are_every_read_this_service_serves(app):
    """Pinned by name, because a derived-equals-derived check alone would not notice both sides losing the same
    route."""
    reads, _ = surface.published(app)
    assert [r.path for r in reads] == [
        # the entity itself: `links.self` on every entity response points here
        "", "/accounts", "/accounts/{account_id}", "/accounts/{account_id}/register", "/capabilities",
        "/close-checks", "/closes/{close_id}", "/documents", "/documents/{document_id}",
        "/documents/{document_id}/content", "/entries",
        "/entries/{entry_id}", "/entries/{entry_id}/events", "/events", "/exports/{export_id}",
        # the files the export published (chunk C2a)
        "/exports/{export_id}/artifacts/{artifact}", "/lines",
        "/lines/{line_id}", "/provenance", "/provisional", "/reconciliation", "/reports/balance-sheet",
        "/reports/composition", "/reports/income-statement", "/reports/statements", "/reports/statements.pdf",
        "/reports/trial-balance", "/snapshots/{snapshot_id}", "/statements", "/statements/{statement_id}",
        "/statements/{statement_id}/lines", "/unrecovered-tax", "/unrecovered-tax/{record_id}"]


def test_the_published_commands_are_the_journal_loop_and_its_extensions(app):
    """The whole published write vocabulary, named once (issue 0006's "desired surface")."""
    _, writes = surface.published(app)
    assert sorted(w.command for w in writes) == [
        "archive_account", "await_document", "book_line", "close_period", "correct_entry", "create_account",
        "file_document", "ignore_line", "link_line", "load_statement", "post_batch", "post_entry",
        "record_unrecovered_tax", "reexplain_line", "register_document", "reopen_period", "replace_statement",
        "resolve_unrecovered_tax", "restore_account", "reverse_entry", "run_export", "withdraw_expectation"]


def test_the_service_level_surface_is_the_entity_collection(app):
    """The other half of discovery, pinned by name for the same reason the entity reads are.

    Every check above is relative to ``{entity_id}``, so without this one a write could be added to ``/v1`` and
    be covered by nothing.
    """
    reads, writes = surface.service_published(app)
    assert [r.path for r in reads] == ["/v1/entities"]
    assert [(w.method, w.path, w.command) for w in writes] == [("POST", "/v1/entities", "initialize_entity")]


def test_every_service_level_write_names_its_command_and_declares_its_reply(app):
    """The same two guarantees the entity writes get: a name that is the handler's, and a checked reply."""
    _, writes = surface.service_published(app)
    for write in writes:
        assert write.command and write.command.isidentifier(), write
        assert "_v1_entities" not in write.command, f"{write.command} is FastAPI's generated id, not a name"
    for route in surface.service_routes(app):
        for method in sorted(route.methods or ()):
            if method not in MUTATING:
                continue
            declared = {status for status, spec in (route.responses or {}).items()
                        if isinstance(spec, dict) and spec.get("model") is not None and status < 400}
            assert route.status_code in declared, \
                f"{method} {route.path} declares no response model for {route.status_code}"


# What the route answers with
def test_capabilities_publishes_the_commands_it_derives(api_small):
    body = api_small.json("/capabilities")
    base = f"/v1/entities/{api_small.entity_id}"
    names = {c["name"] for c in body["commands"]}
    assert {"post_entry", "correct_entry", "create_account", "archive_account", "close_period"} <= names
    for command in body["commands"]:
        assert command["path"].startswith(base)
        assert f"{command['method']} {command['path']}" in body["writes"]
    assert len(body["commands"]) == len(body["writes"])


def test_capabilities_states_the_amount_contract_in_both_directions(api_small):
    """Decimal in, micros out (issue 0006).  Published, because a caller that guesses either way is wrong."""
    contract = api_small.json("/capabilities")["amount_contract"]
    assert "decimal" in contract["inbound"] and contract["inbound_example"] == "-12.00"
    assert "micro" in contract["outbound"] and contract["outbound_example"] == "-12000000"
    assert "JSON numbers" in contract["refused"]


#: One amount per clause of the published ``amount_contract["refused"]``, and the words in that sentence which
#: promise it is refused.  Each of these was once accepted with a 201 while the contract claimed to refuse it
#: (0006 review).
REFUSED_AMOUNTS = [
    ("0012.00", "leading zeros"),
    ("-0.00", "negative zero"),
    ("-0", "negative zero"),
    ("12.34\n", "whitespace"),
    ("\u0661\u0662.\u0663\u0664", "ASCII digits"),   # Arabic-Indic 12.34: \d matches it, [0-9] does not
    ("+12.34", "'+'"),
    (12.34, "floats"),
    (1234, "JSON numbers"),
]


@pytest.mark.parametrize("amount,clause", REFUSED_AMOUNTS)
def test_every_amount_the_contract_says_is_refused_is_refused(api_small, amount, clause):
    """Checks the published contract against the parser, not the parser against a hard-coded literal.

    The 0006 review found the two had drifted: the published sentence claimed refusals the parser did not make,
    so a caller reading ``/capabilities`` was told the ledger would catch input it silently accepted.
    Arabic-Indic ``١٢.٣٤`` becoming 12.34 is the dangerous one: no error, no rounding, a different number than
    was written.
    """
    contract = api_small.json("/capabilities")["amount_contract"]["refused"]
    assert clause in contract, f"the published contract no longer promises to refuse {clause}"
    response = api_small.post("/entries", key=str(uuid.uuid4()), json={
        "dt": "2026-03-04", "narration": "an amount the contract refuses", "reason": "the contract is the test",
        "legs": [{"account": "expenses:office", "commodity": "USD", "amount": amount},
                 {"account": "assets:cash", "commodity": "USD", "amount": "-12.34"}]})
    assert response.status_code == 422, (amount, response.status_code, response.text[:300])
    assert response.json()["error"]["code"] == "validation", response.text[:300]


def test_the_contract_still_accepts_the_one_spelling_each_amount_has(api_small):
    """The positive control the refusals need: canonical is not "stricter until nothing gets in"."""
    response = api_small.post("/entries", key=str(uuid.uuid4()), json={
        "dt": "2026-03-04", "narration": "the canonical spellings", "reason": "the positive control",
        "legs": [{"account": "expenses:office", "commodity": "USD", "amount": "12.34"},
                 {"account": "assets:cash", "commodity": "USD", "amount": "-12.34"}]})
    assert response.status_code == 201, response.text[:300]
    amounts = sorted(leg["amount_micros"] for leg in response.json()["entry"]["postings"])
    assert amounts == ["-12340000", "12340000"]


def test_every_published_read_answers_something_other_than_404(api_small):
    """A published capability that answers "not found" is a lie about the service.

    Each read is called with a plausible-but-absent id where it takes one, so 404 is expected for exactly those
    and 200/422 for the rest.  What this catches is a path the service does not serve at all, which FastAPI
    answers with 404 and an empty body.
    """
    reads, _ = surface.published(api_small.client.app)
    for read in reads:
        path = read.path.replace("{account_id}", "acct-none").replace("{entry_id}", "e-none") \
            .replace("{close_id}", "cr-none").replace("{document_id}", "doc-none") \
            .replace("{export_id}", "ex-none").replace("{line_id}", "sl-none") \
            .replace("{snapshot_id}", "sn-none").replace("{statement_id}", "st-none") \
            .replace("{record_id}", "ut-none")
        response = api_small.get(path)
        if "{" in read.path or path != read.path:
            assert response.status_code in (404, 422), (path, response.status_code)
            continue
        assert response.status_code in (200, 422), (path, response.status_code, response.text)


def test_a_write_whose_reply_breaks_its_published_schema_is_refused_and_rolls_back(api_small, monkeypatch):
    """The regression test for the fourth gap, driven by breaking the reply on purpose.

    The mismatch is a 500 and the write is rolled back, because the check runs inside the transaction.  A
    committed effect whose recorded reply does not match the contract should be unreachable, not merely
    reported.
    """
    from haddon.api.routes import accounts

    real = accounts._chart_change
    monkeypatch.setattr(accounts, "_chart_change",
                        lambda ctx, er, change: {**real(ctx, er, change), "undocumented": True})
    before = api_small.scalar("SELECT count(*) FROM accounts")
    response = api_small.post("/accounts", key=str(uuid.uuid4()),
                              json={"path": "expenses:drift", "type": "expense", "commodities": ["USD"]})
    assert response.status_code == 500, response.text
    assert "ChartChangeResponse" in response.json()["error"]["message"]
    assert api_small.scalar("SELECT count(*) FROM accounts") == before


def test_the_reconciliation_commands_are_absent_when_the_extension_is_off(fast_dir):
    from haddon import extensions

    with extensions.without(extensions.RECONCILIATION):
        off = create_app()
    _, writes = surface.published(off)
    names = {w.command for w in writes}
    assert not names & {"load_statement", "replace_statement", "book_line", "link_line", "ignore_line",
                        "reexplain_line"}
    assert {"post_entry", "post_batch", "correct_entry", "close_period"} <= names
