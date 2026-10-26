"""Chunk D1: the read endpoints, against the sample corpus books.

Every assertion here is checked against a second source of truth rather than against the API's own output: the
database directly, or the ``haddon.reports`` function the route is supposed to be a thin wrapper around.  An API
test that only agrees with itself proves the route ran, not that it answered.

The register gets the most attention because its answer is stateful.  A movement list is easy; a running
balance that is still correct on page three is not, and the failure is silent: a plausible column of numbers
that is wrong from the second page onwards.
"""

from __future__ import annotations

import json

import pytest

from haddon.domain.micros import Micros
from haddon.repository import closing, exports, requests
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction
from haddon.reports import income_statement, register, trial_balance

#: Measured at 0.5 s or more per test, or needing a shared fixture that costs that much to build (chunk E3).
#: The fast tier (`make test-fast`) skips these; the commit gate (`make test-gate`) does not.
pytestmark = pytest.mark.slow

#: The corpus book's year.  A journal is continuous (issue 0002), so every "the book" assertion names this range
#: explicitly.
START, END = "2025-01-01", "2025-12-31"


@pytest.fixture
def inc_conn(api_inc):
    """A reader on the same file the app is serving, for the independent comparison."""
    engine = api_inc.read_engine()
    with engine.connect() as conn:
        yield conn
    engine.dispose()


def m(text: str) -> Micros:
    return Micros.parse(text)


# Discovery
def test_the_entity_listing_matches_the_file_it_describes(api_inc):
    item = api_inc.json("/v1/entities")["items"][0]
    assert item["entity_id"] == api_inc.entity_id == "example-inc"
    assert item["legal_name"] == api_inc.scalar("SELECT name FROM entities")
    assert item["base_currency"] == api_inc.scalar("SELECT base_currency FROM entities") == "USD"
    assert item["commodities"] == ["USD"]
    assert item["lock_through"] is None and item["lock_version"] == 1


def test_capabilities_publishes_the_real_chart_and_currency_bindings(api_inc):
    caps = api_inc.json("/capabilities")
    assert [c["code"] for c in caps["commodities"]] == ["USD"]
    assert caps["commodities"][0]["precision"] == 2
    assert len(caps["account_commodities"]) == api_inc.scalar("SELECT count(*) FROM account_commodities")
    # Decimal in, micros out (issue 0006): both halves are published, because a caller that guesses either way
    # is wrong.
    assert caps["amount_contract"]["inbound"].startswith("exact decimal string")
    assert caps["amount_contract"]["outbound"].startswith("base-10 integer string")
    # This list is derived from the mounted routers (issue 0006), so a published path is literally the OpenAPI
    # path with the entity filled in.
    assert caps["writes"] == [f"POST {api_inc.base}/accounts",
                             # issue 0006: the chart's lifecycle.  Restore exists because archiving with no
                             # inverse would be a state nothing could leave (0006 review).
                             f"POST {api_inc.base}/accounts/{{account_id}}/archive",
                             f"POST {api_inc.base}/accounts/{{account_id}}/restore",
                             # a converted set of entries arrives as one unit, applied whole or refused whole
                             f"POST {api_inc.base}/batches",
                             # issue 0005: closing a period and reopening one are commands like any other
                             f"POST {api_inc.base}/close",
                             f"POST {api_inc.base}/documents",
                             f"POST {api_inc.base}/entries",
                             f"POST {api_inc.base}/entries/{{entry_id}}/awaiting-document",
                             f"POST {api_inc.base}/entries/{{entry_id}}/awaiting-document/withdraw",
                             # issue 0006: a posted entry is restated, not only withdrawn
                             f"POST {api_inc.base}/entries/{{entry_id}}/correct",
                             f"POST {api_inc.base}/entries/{{entry_id}}/documents",
                             f"POST {api_inc.base}/entries/{{entry_id}}/reverse",
                             f"POST {api_inc.base}/entries/{{entry_id}}/unrecovered-tax",
                             # the journal these figures come from, rendered and verified on demand
                             f"POST {api_inc.base}/exports",
                             # issue 0004: four things can be said about a statement line, in those words
                             f"POST {api_inc.base}/lines/{{line_id}}/book",
                             f"POST {api_inc.base}/lines/{{line_id}}/ignore",
                             f"POST {api_inc.base}/lines/{{line_id}}/link",
                             f"POST {api_inc.base}/lines/{{line_id}}/reexplain",
                             f"POST {api_inc.base}/reopen",
                             f"POST {api_inc.base}/statements",
                             f"POST {api_inc.base}/statements/{{statement_id}}/replace",
                             f"POST {api_inc.base}/unrecovered-tax/{{record_id}}/resolve"]
    # Every one of them names the command it runs (issue 0006).
    assert {c["name"] for c in caps["commands"]} >= {"create_account", "archive_account", "post_entry",
                                                     "correct_entry", "close_period", "load_statement"}


# The chart
def test_accounts_lists_the_whole_chart_with_its_allowed_commodities(api_inc):
    page = api_inc.json("/accounts?limit=200")
    assert len(page["items"]) == api_inc.scalar("SELECT count(*) FROM accounts") > 1
    by_id = {a["id"]: a for a in page["items"]}
    con = api_inc.raw()
    try:
        for aid, path, typ, cash in con.execute("SELECT id, path, type, is_cash FROM accounts"):
            assert by_id[aid]["path"] == path and by_id[aid]["type"] == typ
            assert by_id[aid]["is_cash"] is bool(cash)
        for aid, com in con.execute("SELECT account_id, commodity FROM account_commodities"):
            assert com in by_id[aid]["commodities"]
    finally:
        con.close()


@pytest.mark.parametrize("kind", ["asset", "liability", "equity", "income", "expense"])
def test_the_type_filter_selects_exactly_that_type(api_inc, kind):
    page = api_inc.json(f"/accounts?type={kind}&limit=200")
    expected = api_inc.scalar("SELECT count(*) FROM accounts WHERE type=?", (kind,))
    assert len(page["items"]) == expected > 0
    assert {a["type"] for a in page["items"]} == {kind}


def test_one_account_carries_its_version_as_an_etag(api_inc):
    aid = api_inc.scalar("SELECT id FROM accounts ORDER BY id LIMIT 1")
    response = api_inc.get(f"/accounts/{aid}")
    body = response.json()
    assert response.headers["ETag"] == f'"{body["version"]}"'
    assert body["version"] == api_inc.scalar("SELECT version FROM accounts WHERE id=?", (aid,))
    assert body["account"]["links"]["register"] == f"{api_inc.base}/accounts/{aid}/register"
    assert body["entity_id"] == api_inc.entity_id and body["legal_name"]


def test_an_unknown_account_is_404_on_both_the_item_and_its_register(api_inc):
    for path in ("/accounts/acct-not-real", f"/accounts/acct-not-real/register?from={START}&to={END}"):
        response = api_inc.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"


# The register
def busiest_account(api) -> str:
    return api.scalar("SELECT account_id FROM postings GROUP BY account_id ORDER BY count(*) DESC LIMIT 1")


def test_the_register_matches_the_reports_layer_line_for_line(api_inc, inc_conn):
    aid = busiest_account(api_inc)
    expected = register(inc_conn, api_inc.entity_id, account_id=aid, start=START, end=END)
    served = api_inc.json(f"/accounts/{aid}/register?from={START}&to={END}&limit=200")
    assert len(served["items"]) == min(len(expected.lines), 200) > 10
    for got, want in zip(served["items"], expected.lines, strict=False):
        assert (got["entry_id"], got["dt"], got["seq"]) == (want.entry_id, want.dt, want.seq)
        assert got["amount_micros"] == want.amount.render()
        assert got["running_micros"] == want.running.render()
        assert got["account_path"] == want.path


def test_running_balances_include_pre_page_history(api_inc, inc_conn):
    """The trap: a running column that restarts at the top of each page looks right and is wrong.

    Walked page by page, the running balance must be the same sequence the whole-range report produces, so page
    two's first running balance already contains everything on page one.
    """
    aid = busiest_account(api_inc)
    whole = register(inc_conn, api_inc.entity_id, account_id=aid, start=START, end=END)
    assert len(whole.lines) > 12, "this account is too small to paginate meaningfully"

    walked, cursor, pages = [], None, 0
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        page = api_inc.json(f"/accounts/{aid}/register?from={START}&to={END}&limit=5{suffix}")
        walked.extend(page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 200
    assert pages > 2
    assert [ln["running_micros"] for ln in walked] == [ln.running.render() for ln in whole.lines]
    # The running column is cumulative across the page boundary.
    for previous, current in zip(walked, walked[1:], strict=False):
        assert m(current["running_micros"]) == Micros(m(previous["running_micros"]).value
                                                      + m(current["amount_micros"]).value)
    assert m(walked[-1]["running_micros"]).render() != m(walked[4]["running_micros"]).render()

    # The same for a register that starts partway through the book: the history before the window counts too,
    # not just the history before the page.  A register recomputed from `from` would restart at zero here.
    midpoint = whole.lines[len(whole.lines) // 2].dt
    expected_window = [ln for ln in whole.lines if ln.dt >= midpoint]
    windowed, cursor = [], None
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        page = api_inc.json(f"/accounts/{aid}/register?from={midpoint}&to={END}&limit=5{suffix}")
        windowed.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [ln["running_micros"] for ln in windowed] == [ln.running.render() for ln in expected_window]
    assert m(windowed[0]["running_micros"]) != m(windowed[0]["amount_micros"]), \
        "the window's first running balance restarted instead of continuing the book"


def test_a_window_reports_the_balance_carried_into_it(api_inc, inc_conn):
    """`opening_micros` is what the window does not show: the book's position the instant before `from`."""
    aid = busiest_account(api_inc)
    whole = register(inc_conn, api_inc.entity_id, account_id=aid, start=START, end=END)
    midpoint = whole.lines[len(whole.lines) // 2].dt
    page = api_inc.json(f"/accounts/{aid}/register?from={midpoint}&to={END}&limit=200")
    before = [ln for ln in whole.lines if ln.dt < midpoint]
    assert before, "the midpoint has no history before it"
    for commodity, opening in page["opening_micros"].items():
        last = [ln for ln in before if ln.commodity == commodity][-1]
        assert opening == last.running.render()
    # The first line in the window continues from the opening; it does not restart.
    first = page["items"][0]
    assert m(first["running_micros"]) == Micros(m(page["opening_micros"][first["commodity"]]).value
                                                + m(first["amount_micros"]).value)
    assert first["dt"] >= midpoint


def test_the_whole_range_has_nothing_carried_into_it(api_inc):
    aid = busiest_account(api_inc)
    page = api_inc.json(f"/accounts/{aid}/register?from={START}&to={END}&limit=200")
    assert page["opening_micros"] == {}
    assert (page["start"], page["end"]) == (START, END)


# A register is a date-range window over one continuous journal (issue 0002).  A request that names no range is
# the whole journal, and there is no book year for a range to cross out of.


def test_the_commodity_filter_narrows_without_disturbing_the_running_balance(api_inc, inc_conn):
    aid = busiest_account(api_inc)
    both = api_inc.json(f"/accounts/{aid}/register?from={START}&to={END}&limit=200")
    usd = api_inc.json(f"/accounts/{aid}/register?from={START}&to={END}&commodity=USD&limit=200")
    assert {ln["commodity"] for ln in usd["items"]} == {"USD"}
    assert [ln["running_micros"] for ln in usd["items"]] == \
        [ln["running_micros"] for ln in both["items"] if ln["commodity"] == "USD"]
    assert api_inc.json(f"/accounts/{aid}/register?from={START}&to={END}&commodity=EUR&limit=200")["items"] == []


# Entries
def test_entries_list_matches_the_table_and_carries_full_legs(api_inc):
    page = api_inc.json("/entries?limit=200")
    assert len(page["items"]) == 200
    total = api_inc.scalar("SELECT count(*) FROM entries")
    assert total > len(page["items"])   # a full corpus book, so the page boundary this module tests is a real boundary
    first = page["items"][0]
    legs = api_inc.scalar("SELECT count(*) FROM postings WHERE entry_id=?", (first["id"],))
    assert len(first["postings"]) == legs >= 2
    assert sum(int(p["amount_micros"]) for p in first["postings"]) == 0   # every booked entry balances


@pytest.mark.parametrize("query,column,value", [
    ("status=posted", "status", "posted"),
    ("origin=opening", "origin", "opening"),
])
def test_entry_filters_select_exactly_what_they_name(api_inc, query, column, value):
    page = api_inc.json(f"/entries?{query}&limit=200")
    expected = api_inc.scalar(f"SELECT count(*) FROM entries WHERE {column}=?", (value,))
    assert len(page["items"]) == min(expected, 200) > 0
    assert {item[column] for item in page["items"]} == {value}


def test_the_date_window_and_the_account_filter_agree_with_the_table(api_inc):
    window = api_inc.json("/entries?from=2025-03-01&to=2025-03-31&limit=200")
    expected = api_inc.scalar("SELECT count(*) FROM entries WHERE dt>=? AND dt<=?", ("2025-03-01", "2025-03-31"))
    assert len(window["items"]) == min(expected, 200) > 0
    assert all("2025-03-01" <= e["dt"] <= "2025-03-31" for e in window["items"])

    aid = busiest_account(api_inc)
    on_account = api_inc.json(f"/entries?account={aid}&limit=200")
    for entry in on_account["items"]:
        assert aid in [p["account_id"] for p in entry["postings"]]


def test_one_entry_carries_its_version_as_an_etag_and_its_legs_in_order(api_inc):
    eid = api_inc.scalar("SELECT id FROM entries ORDER BY id LIMIT 1")
    response = api_inc.get(f"/entries/{eid}")
    body = response.json()
    assert response.headers["ETag"] == f'"{body["version"]}"'
    assert [p["seq"] for p in body["entry"]["postings"]] == sorted(p["seq"] for p in body["entry"]["postings"])
    con = api_inc.raw()
    try:
        stored = con.execute("SELECT amount_micros FROM postings WHERE entry_id=? ORDER BY seq", (eid,)).fetchall()
    finally:
        con.close()
    assert [p["amount_micros"] for p in body["entry"]["postings"]] == [str(a) for (a,) in stored]


def test_an_entry_carries_its_note_fields(api_inc):
    """E1: the read surface shows the transaction code, comment and tags, in the shapes hledger uses ('' and []
    for "none"), matching the columns row for row."""
    eid = api_inc.scalar("SELECT id FROM entries WHERE comment<>'' AND tags<>'[]' ORDER BY id LIMIT 1")
    body = api_inc.json(f"/entries/{eid}")["entry"]
    con = api_inc.raw()
    try:
        code, comment, tags = con.execute("SELECT code, comment, tags FROM entries WHERE id=?", (eid,)).fetchone()
    finally:
        con.close()
    assert (body["code"], body["comment"]) == (code, comment) and body["tags"] == json.loads(tags)
    assert body["comment"] and body["tags"] and body["code"] == ""
    plain = api_inc.scalar("SELECT id FROM entries WHERE comment='' ORDER BY id LIMIT 1")
    listed = {e["id"]: e for e in api_inc.json("/entries?limit=200")["items"]}
    if plain in listed:
        assert (listed[plain]["code"], listed[plain]["comment"], listed[plain]["tags"]) == ("", "", [])


def test_an_entrys_events_are_scoped_to_that_entry(api_inc):
    eid = api_inc.scalar("SELECT id FROM entries ORDER BY id LIMIT 1")
    body = api_inc.json(f"/entries/{eid}/events")
    assert body["entry_id"] == eid
    assert all(e["subject_id"] == eid for e in body["items"])
    assert body["high_water"] == api_inc.scalar("SELECT max(seq) FROM events")
    assert api_inc.get("/entries/e-nope/events").status_code == 404


# Reports
def test_the_trial_balance_matches_the_reports_layer_and_nets_to_zero(api_inc, inc_conn):
    served = api_inc.json(f"/reports/trial-balance?as_of={END}")
    expected = trial_balance(inc_conn, api_inc.entity_id, through=END)
    assert served["through"] == END
    assert len(served["lines"]) == len(expected.lines) > 40
    assert [(ln["account_id"], ln["commodity"], ln["amount_micros"]) for ln in served["lines"]] == \
        [(ln.account_id, ln.commodity, ln.amount.render()) for ln in expected.lines]
    assert served["totals"] == {"USD": "0"}   # a balanced book


def test_as_of_narrows_the_trial_balance_inside_the_range(api_inc, inc_conn):
    served = api_inc.json("/reports/trial-balance?as_of=2025-06-30")
    expected = trial_balance(inc_conn, api_inc.entity_id, through="2025-06-30")
    assert served["through"] == "2025-06-30"
    assert [ln["amount_micros"] for ln in served["lines"]] == [ln.amount.render() for ln in expected.lines]
    assert served["totals"] == {"USD": "0"}
    full = api_inc.json(f"/reports/trial-balance?as_of={END}")
    assert served["lines"] != full["lines"]   # it really did narrow


def test_the_balance_sheet_is_the_position_accounts_at_a_date(api_inc, inc_conn):
    served = api_inc.json(f"/reports/balance-sheet?as_of={END}")
    assert set(served["sections"]) == {"asset", "liability", "equity"}
    expected = trial_balance(inc_conn, api_inc.entity_id, through=END)
    for kind in served["sections"]:
        assert [ln["account_id"] for ln in served["sections"][kind]] == \
            [ln.account_id for ln in expected.of_type(kind)]
    # assets = liabilities + equity, so the three sections net to the profit folded out of the P&L
    stmt = api_inc.json(f"/reports/income-statement?from={START}&to={END}")
    assert int(served["totals"]["USD"]) + int(stmt["net_micros"]["USD"]) == 0


def test_the_income_statement_excludes_the_opening_entry(api_inc, inc_conn):
    served = api_inc.json(f"/reports/income-statement?from={START}&to={END}")
    expected = income_statement(inc_conn, api_inc.entity_id, start=START, end=END)
    assert [(ln["account_id"], ln["amount_micros"]) for ln in served["lines"]] == \
        [(ln.account_id, ln.amount.render()) for ln in expected.lines]
    assert served["net_micros"] == {"USD": expected.net["USD"].render()}
    assert {ln["type"] for ln in served["lines"]} <= {"income", "expense"}
    assert (served["start"], served["end"]) == (START, END)


def _rows(api, sql: str, params: tuple = ()) -> list[tuple]:
    con = api.raw()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def test_composition_names_the_journals_the_period_was_imported_from(api_inc):
    """The completeness answer, checked against ``journal_mappings`` rather than against itself.

    A reader cannot tell a quiet month from a half-loaded one by looking at the figures, so this is the one
    report whose job is to say where the figures came from.  The counts are compared with the mapping table the
    importer wrote, and the origins with the entries table, because a composition that agreed only with the
    route that produced it would prove nothing about the book.
    """
    served = api_inc.json(f"/reports/composition?from={START}&to={END}")
    assert served["report"] == "composition"
    assert (served["start"], served["end"]) == (START, END)

    expected_origins = dict(_rows(api_inc, "SELECT origin, count(*) FROM entries WHERE dt>=? AND dt<=? "
                                           "AND status='posted' GROUP BY origin", (START, END)))
    assert served["origins"] == expected_origins and expected_origins
    assert served["entry_count"] == sum(expected_origins.values())

    expected_journals = dict(_rows(
        api_inc, "SELECT m.journal_path, count(DISTINCT m.entry_id) FROM journal_mappings m "
                 "JOIN entries e ON e.id=m.entry_id WHERE e.dt>=? AND e.dt<=? AND e.status='posted' "
                 "GROUP BY m.journal_path", (START, END)))
    assert served["journals"] == expected_journals and expected_journals
    # these books were imported from journals, so every posted entry traces back to one
    assert sum(expected_journals.values()) == served["entry_count"]


def test_composition_narrows_to_the_period_and_reports_an_empty_one_as_empty(api_inc):
    narrowed = api_inc.json("/reports/composition?from=2025-01-01&to=2025-06-30")
    whole = api_inc.json(f"/reports/composition?from={START}&to={END}")
    assert narrowed["entry_count"] < whole["entry_count"]
    assert (narrowed["start"], narrowed["end"]) == ("2025-01-01", "2025-06-30")
    assert sum(narrowed["origins"].values()) == narrowed["entry_count"]
    counted = api_inc.scalar("SELECT count(*) FROM entries WHERE status='posted' "
                             "AND dt>='2025-01-01' AND dt<='2025-06-30'")
    assert narrowed["entry_count"] == counted


# A report is a date range over one continuous journal (issue 0002), so there is no year to require and no book
# to be missing.  An open range is the whole journal and is a normal request.


# A malformed write body
@pytest.mark.parametrize("body,path", [
    ({}, "through"), ({"through": "2026-03-31"}, "reason"),
    ({"through": "2026-03-31", "reason": "", "expected_version": 1}, "reason"),
    ({"through": "2026-03-31", "reason": "r", "expected_version": 1, "unexpected": 1}, "unexpected"),
    ({"through": "2026-03-31", "reason": "r", "expected_version": 1, "overrides": "not-a-list"}, "overrides"),
])
def test_a_malformed_submission_is_422_naming_the_field(api_inc, body, path):
    """The published schema refuses before anything runs, and the refusal names the field.

    ``POST /close`` has a required date, a required reason, a required version and a list, which between them
    exercise every shape of schema refusal.
    """
    response = api_inc.post("/close", json=body, key=f"bad-{path}-{len(body)}")
    assert response.status_code == 422
    assert any(path in d.get("path", "") for d in response.json()["error"]["details"]), response.text
    assert api_inc.scalar("SELECT count(*) FROM close_records") == 0


# Immutable artifacts
def _record_an_export(api):
    """Write the rows the export path writes, through the repository, so the routes have something real to read."""
    engine = api.engine()
    try:
        with write_transaction(engine) as conn:
            ctx = WriteCtx.open(conn, "agent:exporter")
            snap = closing.create_snapshot(ctx, through=END, state_hash="state-hash-1",
                                           manifest={"files": ["2025.journal"]}, report={"entries": 1},
                                           checks={"strict": "ok"})
            op = requests.create_operation(ctx, kind="export", intent={"range": {"start": START, "end": END}})
            exp = exports.record_export(ctx, snapshot_id=snap.id, operation_id=op.id, manifest_sha256="a" * 64,
                                        artifacts=[{"path": "2025.journal", "sha256": "b" * 64, "bytes": 4096}],
                                        verifier={"tool": "hledger", "version": "hledger 1.52.1, ...",
                                                  "result": "ok"})
        return snap, exp
    finally:
        engine.dispose()


def test_a_snapshot_and_its_export_read_back_exactly_as_written(api_inc):
    snap, exp = _record_an_export(api_inc)
    served = api_inc.json(f"/snapshots/{snap.id}")
    assert served["state_hash"] == "state-hash-1" and served["through"] == END
    assert served["manifest"] == {"files": ["2025.journal"]}
    assert served["checks"] == {"strict": "ok"}
    assert served["entity_id"] == api_inc.entity_id

    artifact = api_inc.json(f"/exports/{exp.id}")
    assert artifact["manifest_sha256"] == "a" * 64
    assert artifact["artifacts"] == [{"path": "2025.journal", "sha256": "b" * 64, "bytes": 4096}]
    assert artifact["verifier"]["result"] == "ok" and "1.52.1" in artifact["verifier"]["version"]
    assert artifact["links"]["snapshot"] == f"{api_inc.base}/snapshots/{snap.id}"
    assert api_inc.json(artifact["links"]["snapshot"])["id"] == snap.id


def test_an_unknown_snapshot_or_export_is_404(api_inc):
    assert api_inc.get("/snapshots/snap-nope").status_code == 404
    assert api_inc.get("/exports/exp-nope").status_code == 404
