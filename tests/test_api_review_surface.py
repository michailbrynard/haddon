"""The three things a reviewing surface needs from ``/v1`` (chunk G4).

``GET /lines`` reads a span of time rather than one document: every statement line over a date range, in the
order somebody reviewing a period reads it, with what has become of each line and the entries it was booked to.
Without it, reviewing a month is one request for the lines and one more per line.

``GET /reconciliation`` answers "does the bank agree with us, and by how much" at any date, not only where the
bank happened to print a balance.  That difference is the one number a reviewer acts on.

``POST /entries/{id}/reverse`` lets a caller disagree with something already posted, not only book, link or
ignore a line.
"""

from __future__ import annotations

import shutil
import uuid

import pytest

from haddon.api.config import ProxyAuth
from haddon.repository import entries as en
from haddon.repository import lines as ln

from conftest import build_api
from repo_helpers import AGENT, load_fixture
from statement_helpers import CASH, OFFICE, book, lines_of, load, row

pytestmark = pytest.mark.slow

ENTITY = "fernside-instruments"
TOKEN = "test-token-please-do-not-use-in-anger"
PROXY_USER = "reviewer"


@pytest.fixture
def review(provisioned_template, fast_dir):
    """Fernside with a March statement: one booked line, one ignored, one left unexplained."""
    from haddon.db.connection import make_engine

    path = fast_dir / "review.db"
    shutil.copy(provisioned_template, path)
    engine = make_engine(path, busy_timeout_ms=200)
    try:
        fx = load_fixture(engine)
        statement = load(fx, closing="-9000000", tag="review-mar",
                         rows=[row(f"2026-03-{d:02d}", "-3000000", reference=f"R-{d}") for d in (5, 12, 19)])
        rows = lines_of(fx, statement.id)
        booked = book(fx, rows[0].id)
        with fx.tx() as ctx:
            ignored = ln.ignore_line(ctx, line_id=rows[1].id, reason="a bank fee we account for annually")
    finally:
        engine.dispose()
    config, client = build_api({ENTITY: path}, proxy_auth=ProxyAuth(header="X-Haddon-User"))
    with client:
        yield type("Review", (), {"client": client, "path": path, "rows": rows, "statement": statement,
                                  "booked": booked, "ignored": ignored, "config": config})()


def base(path: str) -> str:
    return f"/v1/entities/{ENTITY}{path}"


def get(review, path: str, **params) -> dict:
    from conftest import API_TOKEN

    response = review.client.get(base(path), params=params, headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert response.status_code == 200, response.text
    return response.json()


def post(review, path: str, body: dict, *, user: str | None = None, token: bool = False):
    from conftest import API_TOKEN

    headers = {"Idempotency-Key": str(uuid.uuid4())}
    if token:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    if user is not None:
        headers["X-Haddon-User"] = user
    return review.client.post(base(path), json=body, headers=headers)


# GET /lines
def test_the_collection_reads_a_date_range_across_statements_in_the_order_a_month_is_read(review):
    body = get(review, "/lines", **{"from": "2026-03-01", "to": "2026-03-31"})
    assert [item["dt"] for item in body["items"]] == ["2026-03-05", "2026-03-12", "2026-03-19"]
    assert body["next_cursor"] is None


def test_a_range_that_excludes_a_line_excludes_it(review):
    body = get(review, "/lines", **{"from": "2026-03-10", "to": "2026-03-15"})
    assert [item["dt"] for item in body["items"]] == ["2026-03-12"]


def test_every_line_carries_its_status_and_the_entries_it_was_booked_to(review):
    items = {item["line_id"]: item for item in get(review, "/lines")["items"]}

    booked = items[review.rows[0].id]
    assert booked["status"] == ln.BOOKED_STATUS
    assert [e["id"] for e in booked["entries"]] == [review.booked.entry.id]
    assert booked["entries"][0]["status"] == "posted"
    assert {p["account_path"] for p in booked["entries"][0]["postings"]} == {CASH, OFFICE}

    ignored = items[review.rows[1].id]
    assert ignored["status"] == ln.IGNORED_STATUS and ignored["entries"] == []
    assert ignored["explanation"]["reason"] == "a bank fee we account for annually"

    unexplained = items[review.rows[2].id]
    assert unexplained["status"] == ln.UNEXPLAINED and unexplained["entries"] == []


def test_an_entry_that_has_already_been_mirrored_says_so(review):
    """Tells a surface whether disagreeing is still an option.  ``reverse_entry`` refuses a second mirror, so
    offering the choice again would only ever offer a refusal."""
    assert get(review, f"/lines/{review.rows[0].id}")["line"]["entries"][0]["reversed_by"] is None
    post(review, f"/entries/{review.booked.entry.id}/reverse",
         {"reason": "the memo names a client, not a supplier"}, token=True).raise_for_status()
    after = get(review, f"/lines/{review.rows[0].id}")["line"]["entries"][0]
    assert after["reversed_by"] is not None


def test_filtering_by_state_keeps_only_those_lines(review):
    body = get(review, "/lines", state=["unexplained"])
    assert [item["line_id"] for item in body["items"]] == [review.rows[2].id]


def test_several_states_may_be_asked_for_at_once(review):
    body = get(review, "/lines", state=["unexplained", "ignored"])
    assert {item["line_id"] for item in body["items"]} == {review.rows[1].id, review.rows[2].id}


def test_an_unknown_state_is_refused_by_name_rather_than_returning_nothing(review):
    """An empty page and "there is no such state" read identically to a caller, and only one is the caller's
    mistake.  So a typo is a 422 that names what it should have been."""
    from conftest import API_TOKEN

    response = review.client.get(base("/lines"), params={"state": "unexplaned"},
                                 headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert response.status_code == 422
    detail = response.json()["error"]["details"][0]
    assert detail["reason"] == "unknown_state" and ln.UNEXPLAINED in detail["expected"]


@pytest.fixture
def sparse(provisioned_template, fast_dir):
    """Twelve lines of which only the last two are left unexplained.

    ``state`` is applied after the page is selected, so with a small limit the early pages come back empty with
    ``next_cursor`` still set.  A client can misread that as "there are none", which is why this fixture is
    sparse rather than balanced.
    """
    from haddon.db.connection import make_engine

    path = fast_dir / "sparse.db"
    shutil.copy(provisioned_template, path)
    engine = make_engine(path, busy_timeout_ms=200)
    try:
        fx = load_fixture(engine)
        statement = load(fx, closing="-36000000", tag="sparse-mar",
                         rows=[row(f"2026-03-{d:02d}", "-3000000", reference=f"S-{d}") for d in range(1, 13)])
        rows = lines_of(fx, statement.id)
        for stored in rows[:10]:
            book(fx, stored.id)
    finally:
        engine.dispose()
    config, client = build_api({ENTITY: path})
    with client:
        yield type("Sparse", (), {"client": client, "path": path, "rows": rows, "config": config})()


def test_a_page_says_when_a_filter_ran_after_it_was_selected(sparse):
    """Every other collection signals "last page" with ``len(items) < limit``.  On this one that signal is
    wrong, hence the field."""
    assert get(sparse, "/lines", state=["unexplained"], limit=3)["filtered_after_page"] == ["state"]
    # Every filter the query itself applied: a short page here does mean the last one.
    assert get(sparse, "/lines", limit=3)["filtered_after_page"] == []
    assert get(sparse, "/lines", limit=3, **{"from": "2026-03-01"})["filtered_after_page"] == []


def test_a_client_that_stops_at_the_first_short_page_sees_none_of_the_lines_it_asked_for(sparse):
    """The first page of ``state=unexplained`` is empty and has a ``next_cursor``, because the three lines it
    selected were all booked."""
    first = get(sparse, "/lines", state=["unexplained"], limit=3)
    assert first["items"] == [] and first["next_cursor"] is not None
    assert first["filtered_after_page"] == ["state"]


def test_a_client_that_follows_the_cursor_to_the_end_sees_every_line_in_the_state(sparse):
    """Reading until ``next_cursor`` is null is the contract, and it is exact: no line is missed, none is
    served twice, and the walk ends."""
    seen: list[str] = []
    cursor, pages, short_with_more = None, 0, 0
    while True:
        params = {"state": ["unexplained"], "limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        body = get(sparse, "/lines", **params)
        pages += 1
        assert pages <= len(sparse.rows), "the walk did not terminate"
        if len(body["items"]) < 3 and body["next_cursor"] is not None:
            short_with_more += 1
        seen += [item["line_id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [stored.id for stored in sparse.rows[10:]]
    assert len(seen) == len(set(seen))
    assert short_with_more >= 1, "the fixture stopped exercising the short-page case"
    # The same set the caller would get by reading everything and filtering it themselves.
    whole = get(sparse, "/lines", limit=200)["items"]
    assert seen == [i["line_id"] for i in whole if i["status"] == ln.UNEXPLAINED]


def test_a_lines_cursor_is_bound_to_the_state_it_was_issued_under(sparse):
    """The filter section A documented is also the filter the cursor has to bind.

    ``state`` runs after the page rather than in the query, which makes it easy to leave out of the digest every
    other filter is in.  But a cursor is a position in one listing.  Honoured under a different ``state``, it
    carries a position measured against rows the caller is no longer asking for, and the page it returns belongs
    to neither listing.  ``from`` refuses this the same way.
    """
    from conftest import API_TOKEN

    def raw(**params):
        return sparse.client.get(base("/lines"), params=params,
                                 headers={"Authorization": f"Bearer {API_TOKEN}"})

    issued = get(sparse, "/lines", state=["unexplained"], limit=3)["next_cursor"]
    assert issued is not None
    for changed in ({"state": ["booked"]}, {"state": ["unexplained", "booked"]}, {}):
        response = raw(limit=3, cursor=issued, **changed)
        assert response.status_code == 422, (changed, response.status_code, response.text)
        detail = response.json()["error"]["details"][0]
        assert detail["reason"] == "cursor_mismatch", changed
    # Repeating the filter in another order is the same listing, not a new one.
    assert raw(limit=3, cursor=issued, state=["unexplained"]).status_code == 200
    two = get(sparse, "/lines", state=["unexplained", "booked"], limit=3)["next_cursor"]
    assert raw(limit=3, cursor=two, state=["booked", "unexplained"]).status_code == 200


def test_the_page_is_a_page_and_its_cursor_carries_on(review):
    first = get(review, "/lines", limit=2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = get(review, "/lines", limit=2, cursor=first["next_cursor"])
    assert [i["line_id"] for i in second["items"]] == [review.rows[2].id]


# GET /reconciliation
def test_the_reconciliation_compares_the_statement_arithmetic_against_the_books(review):
    """One line of three is booked, so the books hold 6.00 more than the bank: the two unbooked payments are
    money that left the account and that no entry has taken out of the ledger balance yet."""
    body = get(review, "/reconciliation", as_of="2026-03-31")
    account = next(a for a in body["accounts"] if a["account_path"] == CASH)
    assert account["statement_micros"] == "-9000000", "opening plus every loaded line to the date"
    assert account["ledger_micros"] == "-3000000", "only the one booked line is in the books"
    assert account["difference_micros"] == "6000000"
    assert account["agrees"] is False and body["agrees"] is False


def test_a_month_whose_lines_are_all_booked_agrees_to_the_cent(review):
    with _write(review) as ctx:
        ln.reexplain_line(ctx, line_id=review.rows[1].id, kind="booked", reason="it is ours after all",
                          legs=[en.Leg(_acct(ctx, OFFICE), "USD", "3000000")], narration="office supplies")
        ln.book_line(ctx, line_id=review.rows[2].id, legs=[en.Leg(_acct(ctx, OFFICE), "USD", "3000000")],
                     narration="office supplies", reason="card purchase")

    body = get(review, "/reconciliation", as_of="2026-03-31")
    account = next(a for a in body["accounts"] if a["account_path"] == CASH)
    assert account["difference_micros"] == "0" and account["agrees"] is True
    assert account["unexplained_lines"] == 0 and account["explained"] is True


def test_the_printed_closing_balance_is_reported_beside_the_derived_one_where_the_bank_gave_one(review):
    account = next(a for a in get(review, "/reconciliation", as_of="2026-03-31")["accounts"]
                   if a["account_path"] == CASH)
    assert account["asserted_micros"] == "-9000000", "the statement's own printed closing balance"
    assert account["statement_micros"] == account["asserted_micros"], "two sources, same answer"


def test_days_no_statement_covers_are_reported_rather_than_silently_assumed_empty(review):
    body = get(review, "/reconciliation", as_of="2026-04-30", **{"from": "2026-04-01"})
    account = next(a for a in body["accounts"] if a["account_path"] == CASH)
    assert account["uncovered"] == [{"start": "2026-04-01", "end": "2026-04-30"}]


def test_an_account_with_no_statement_at_all_reports_nothing_rather_than_zero(review):
    """``null`` and ``0.00`` are different claims.  Printing the second for the first would tell somebody their
    books agree with a statement nobody has loaded."""
    body = get(review, "/reconciliation", as_of="2026-03-31")
    other = [a for a in body["accounts"] if a["account_path"] != CASH]
    assert other, "the fixture has more than one cash account"
    assert all(a["statement_micros"] is None and a["difference_micros"] is None for a in other)


# POST /entries/{id}/reverse
def test_reversing_posts_an_exact_mirror_and_leaves_the_original_standing(review):
    response = post(review, f"/entries/{review.booked.entry.id}/reverse",
                    {"reason": "this is a client refund, not office spend"}, token=True)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["original_id"] == review.booked.entry.id and body["replacement_id"] is None

    original = get(review, f"/entries/{review.booked.entry.id}")["entry"]
    mirror = body["entry"]
    assert original["status"] == "posted", "nothing is edited and nothing is deleted"
    assert mirror["origin"] == "reversal"
    assert sorted((p["account_path"], p["amount_micros"]) for p in mirror["postings"]) == \
        sorted((p["account_path"], str(-int(p["amount_micros"]))) for p in original["postings"])


def test_a_reversal_without_a_reason_is_refused(review):
    response = post(review, f"/entries/{review.booked.entry.id}/reverse", {"reason": "  "}, token=True)
    assert response.status_code == 422


def test_a_draft_cannot_be_reversed_because_there_is_nothing_to_disagree_with(review):
    """Booking a line posts (issue 0003), so the draft here is written directly."""
    from haddon.repository import entries as en

    with _write(review) as ctx:
        draft = en.create_draft(ctx, dt="2026-03-05", narration="a draft", origin="manual",
                                legs=[en.Leg(_acct(ctx, CASH), "USD", "-1000000"),
                                      en.Leg(_acct(ctx, OFFICE), "USD", "1000000")])
    response = post(review, f"/entries/{draft.id}/reverse", {"reason": "wrong account"}, token=True)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["reason"] == "original_not_posted"


def test_a_reversal_still_needs_an_idempotency_key(review):
    from conftest import API_TOKEN

    response = review.client.post(base(f"/entries/{review.booked.entry.id}/reverse"),
                                  json={"reason": "wrong account"},
                                  headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert response.status_code == 422


# The proxy's authenticated user as a credential
def test_the_proxy_user_authenticates_and_is_recorded_as_the_actor(review):
    """A browser cannot present a bearer token, so the deployment says which header its proxy sets and the
    service turns that into an actor string like any other.  Nothing downstream can tell the two apart."""
    response = post(review, f"/entries/{review.booked.entry.id}/reverse",
                    {"reason": "the memo names a client"}, user=PROXY_USER)
    assert response.status_code == 201, response.text

    import sqlite3

    con = sqlite3.connect(review.path)
    try:
        actors = [r[0] for r in con.execute("SELECT created_by FROM events WHERE kind='entry.reversed'")]
    finally:
        con.close()
    assert actors == [f"ui:{PROXY_USER}"]


def test_a_bearer_token_still_wins_when_both_are_present(review):
    from conftest import API_ACTOR

    post(review, f"/entries/{review.booked.entry.id}/reverse", {"reason": "wrong account"},
         user=PROXY_USER, token=True).raise_for_status()
    import sqlite3

    con = sqlite3.connect(review.path)
    try:
        actors = [r[0] for r in con.execute("SELECT created_by FROM events WHERE kind='entry.reversed'")]
    finally:
        con.close()
    assert actors == [API_ACTOR]


def test_a_blank_proxy_user_is_attributed_rather_than_written_as_an_empty_actor(review):
    """No user is a misconfigured proxy, not a licence to write nobody: it is named ``ui:unknown``."""
    assert review.config.proxy_auth.actor_for("") == "ui:unknown"
    assert review.config.proxy_auth.actor_for(None) == "ui:unknown"


def test_without_a_configured_proxy_the_header_is_not_a_credential(review):
    """The header only authenticates where the configuration says a proxy sets it.  On a service reachable
    directly, a forged header is just a header."""
    config, client = build_api({ENTITY: review.path})
    assert config.proxy_auth is None
    with client:
        response = client.get(base("/lines"), headers={"X-Haddon-User": PROXY_USER})
    assert response.status_code == 401


# Plumbing
def _write(review):
    from haddon.db.connection import make_engine
    from haddon.repository.ctx import WriteCtx
    from haddon.repository.transaction import write_transaction

    import contextlib

    @contextlib.contextmanager
    def opened():
        engine = make_engine(review.path, busy_timeout_ms=200)
        try:
            with write_transaction(engine) as conn:
                yield WriteCtx.open(conn, AGENT)
        finally:
            engine.dispose()

    return opened()


def _acct(ctx, path: str) -> str:
    from haddon.repository.provisioning import all_accounts

    return next(a.id for a in all_accounts(ctx.conn, ENTITY).values() if a.path == path)


def test_correcting_a_booked_lines_entry_carries_the_line_over_the_api(review):
    """The line follows its entry onto the replacement, and reads as booked by it."""
    line_id = review.rows[0].id
    result = post(review, f"/entries/{review.booked.entry.id}/correct", {
        "reason": "split the purchase",
        "replacement": {"dt": "2026-03-05", "narration": "office supplies",
                        "legs": [{"account": CASH, "commodity": "USD", "amount": "-3.00"},
                                 {"account": OFFICE, "commodity": "USD", "amount": "2.00"},
                                 {"account": "income:sales", "commodity": "USD", "amount": "1.00"}]}},
        token=True)
    assert result.status_code == 201, result.text
    line = get(review, f"/lines/{line_id}")["line"]
    assert line["status"] == ln.BOOKED_STATUS
    assert line["explanation"]["entry_id"] == result.json()["replacement_id"]
    standing = [e for e in line["entries"] if e["status"] == "posted" and not e["reversed_by"]]
    assert [e["id"] for e in standing] == [result.json()["replacement_id"]]


def test_a_line_left_behind_by_an_older_correction_shows_the_replacement_and_can_be_repointed(review):
    """A line left on the mirrored original by an older correction shows the replacement and can be re-pointed.

    Corrections written before lines followed their entry left the line where it was.  The test builds that
    state through the repository directly."""
    from haddon.db.connection import make_engine
    from haddon.repository import corrections as corrections_repo

    engine = make_engine(review.path, busy_timeout_ms=200)
    try:
        fx = load_fixture(engine)
        with fx.tx() as ctx:
            original = en.get_entry(ctx.conn, fx.entity_id, review.booked.entry.id)
            c = corrections_repo.reverse_entry(
                ctx, original_id=original.id, dt=original.dt, reason="split out the VAT",
                replacement=corrections_repo.Replacement(
                    dt=original.dt, narration=original.narration, origin="manual",
                    legs=[en.Leg(fx.acct(CASH), "USD", "-3000000"), en.Leg(fx.acct(OFFICE), "USD", "2000000"),
                          en.Leg(fx.acct("income:sales"), "USD", "1000000")]))
    finally:
        engine.dispose()
    line_id = review.rows[0].id
    line = get(review, f"/lines/{line_id}")["line"]
    # The reader sees what stands for the line now, so it never offers to book the money a second time.
    standing = [e for e in line["entries"] if e["status"] == "posted" and not e["reversed_by"]]
    assert [e["id"] for e in standing] == [c.replacement_id]
    cash = next(p for p in standing[0]["postings"] if p["account_path"] == CASH)
    response = post(review, f"/lines/{line_id}/reexplain",
                    {"kind": "linked", "reason": "the line is the corrected entry",
                     "parts": [{"posting_id": cash["id"], "amount": "-3.00"}]}, token=True)
    assert response.status_code == 201, response.text
    assert get(review, f"/lines/{line_id}")["line"]["explanation"]["entry_id"] == c.replacement_id
