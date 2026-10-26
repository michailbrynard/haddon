"""Reconciliation is an extension, and "optional" has to mean operable (issue 0004's exit gate).

The journal core is the product; statements are an enrichment of it.  That claim is easy to make in a diagram
and easy to break in code, so it is tested here from three sides:

1. The core journal loop (initialize, post, correct, reverse, report, close) runs with the extension switched
   off, and nothing about it mentions a statement.
2. The surface tells the truth: with the extension off its routes are not mounted and ``/capabilities`` does
   not advertise them, so there is no published command that answers 404.
3. The close does not demand evidence nobody is expected to supply: with the extension off its checks are not
   in the close policy at all, rather than blocking a month for want of a statement nobody will load.

And the control in the other direction: with the extension on, the same close is blocked for want of that
statement.  Otherwise this file would pass against a switch that does nothing.
"""

from __future__ import annotations

import pytest

from haddon import commands, extensions
from haddon.api.config import ApiConfig
from haddon.repository import close_checks as cc
from haddon.reports.balances import trial_balance

from conftest import API_TOKEN, build_api
from repo_helpers import AGENT, HUMAN, lock_books, post
from statement_helpers import CASH, OFFICE, SALES, checks, quiet_months, statement


@pytest.fixture
def off():
    with extensions.without(extensions.RECONCILIATION):
        yield


# 1. the core loop, without it
def test_the_whole_journal_loop_runs_with_reconciliation_disabled(fx, off):
    first = post(fx, "2026-03-05", "1250000", debit=OFFICE, credit=SALES, narration="february rent")
    with fx.tx(AGENT) as ctx:
        corrected = commands.correct_entry(
            ctx, original_id=first.id, reason="it was a sale, not a cost",
            replacement=commands.Replacement(
                dt="2026-03-05", narration="february rent", origin="manual",
                legs=[commands.LegSpec(SALES, "USD", "1.25"), commands.LegSpec(OFFICE, "USD", "-1.25")]))
    assert corrected.correction.replacement_id
    with fx.reader() as conn:
        tb = trial_balance(conn, fx.entity_id, through="2026-03-31")
    assert tb.lines, "the journal reports"
    lock_books(fx, "2026-03-31")
    assert fx.scalar("SELECT through FROM period_locks") == "2026-03-31"
    # and nothing in the reconciliation extension was written along the way
    for table in ("documents", "statements", "statement_lines", "explanations", "allocations"):
        assert fx.scalar(f"SELECT count(*) FROM {table}") == 0


def test_a_month_with_no_statement_closes_when_the_extension_is_off(fx, off):
    """With the extension off its checks are not in the policy at all (issue 0005).

    They do not pass; they are not asked.  "Every statement is loaded" is not a check that succeeded when
    nobody was expected to load one, and recording it as one would make an optional extension compulsory in the
    audit trail even where it is switched off.  What the close record keeps instead is which extensions were
    running, so the shorter list of checks is readable rather than mysterious.
    """
    post(fx, "2026-03-05", "1250000", debit=OFFICE, credit=SALES)
    ck = checks(fx, "2026-03-31")
    assert ck.eligible and ck.may_close
    assert [g.key for g in ck.checks] == ["nothing_in_draft"]
    assert ck.check("nothing_in_draft").status == cc.PASS
    assert cc.checks_for_audit(ck)["extensions"] == {"reconciliation": False}


def test_with_the_extension_on_the_same_month_is_blocked_for_want_of_a_statement(fx):
    """The control.  Without this the switch could be doing nothing at all and the test above would still pass."""
    post(fx, "2026-03-05", "1250000", debit=OFFICE, credit=SALES)
    ck = checks(fx, "2026-03-31")
    assert not ck.may_close
    assert ck.check("statement_coverage").status == cc.FAIL
    assert "statement_not_loaded" in {o.kind for o in ck.check("statement_coverage").outstanding}


def test_revising_a_draft_does_not_touch_a_reconciliation_table_with_the_extension_off(fx, off):
    """The core must not hold a hard dependency on the extension's tables (exit gate 6).

    ``haddon.api.app`` imports the line routes unconditionally, so importing the API registers the allocation
    guard whatever the switch says.  The core has to check that reconciliation is running, not just that a hook
    exists.  Otherwise every draft revision in an installation that never reconciles queries ``allocations`` and
    ``explanations``, and against a database without them that is an ``OperationalError`` rather than a refusal.
    """
    import haddon.api.app  # noqa: F401  -- the import that registers the guard, as a deployment would
    from haddon.repository import entries as en

    with fx.tx(AGENT) as ctx:
        draft = en.create_draft(ctx, dt="2026-03-10", narration="unfinished", origin="manual",
                                legs=[en.Leg(fx.acct(OFFICE), "USD", "1000000"),
                                      en.Leg(fx.acct(SALES), "USD", "-1000000")])
    # the sharpest form of "it does not read them": the tables are not there to read
    con = fx.raw()
    con.execute("DROP TABLE allocations")
    con.commit()
    con.close()
    with fx.tx(AGENT) as ctx:
        revised = en.revise_draft(ctx, draft.id, expected_version=draft.version,
                                  legs=[en.Leg(fx.acct(OFFICE), "USD", "2000000"),
                                        en.Leg(fx.acct(SALES), "USD", "-2000000")])
    assert [str(p.amount) for p in revised.legs] == ["2000000", "-2000000"]


def test_the_guard_is_back_the_moment_the_extension_is_on(fx):
    """The control: this is the switch, not a guard that has simply stopped working.

    Without it the test above would pass against a broken hook, which is exactly what "the switch is real" is
    most likely to be mistaken for.
    """
    from haddon.repository import entries as en

    from statement_helpers import book, statement

    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),))
    booked = book(fx, rows[0].id)
    cash = next(p for p in booked.entry.legs if p.account_id == fx.acct(CASH))
    with fx.tx(AGENT) as ctx:
        assert en._allocated_postings(ctx, booked.entry.id) == {cash.id}, "with it on, the guard answers"
    with extensions.without(extensions.RECONCILIATION):
        with fx.tx(AGENT) as ctx:
            assert en._allocated_postings(ctx, booked.entry.id) == set(), "with it off, it answers nothing"


def test_a_draft_is_still_found_when_the_extension_is_off(fx, off):
    """The core check stays on: an unposted draft is unfinished bookkeeping whether or not anybody reconciles.

    Issue 0005 split the draft check from the unexplained-line check, so only the statement half leaves with the
    extension.
    """
    from haddon.repository import entries as en

    post(fx, "2026-03-05", "1250000", debit=OFFICE, credit=SALES)
    with fx.tx(AGENT) as ctx:
        en.create_draft(ctx, dt="2026-03-10", narration="unfinished", origin="manual",
                        legs=[en.Leg(fx.acct(OFFICE), "USD", "1000000"),
                              en.Leg(fx.acct(SALES), "USD", "-1000000")])
    gate = checks(fx, "2026-03-31").check("nothing_in_draft")
    assert gate.status == cc.FAIL
    assert {o.kind for o in gate.outstanding} == {"entry_not_posted"}


# 2. the published surface tells the truth
def test_the_reconciliation_routes_are_not_mounted_when_the_extension_is_off(fx, off):
    api = build_api({fx.entity_id: fx.path})[1]
    with api:
        served = {p for p in api.get("/openapi.json").json()["paths"]}
    # A reconciliation route is exactly one of these or under it.  A bare ``endswith`` would also catch the
    # artifact extension's ``/reports/statements.pdf`` and the core's shaped ``/reports/statements``, which are
    # rendered statements of account rather than bank statements, so the match is on the whole segment.
    reconciliation = ("/statements", "/lines", "/reconciliation", "/provenance")
    assert not [p for p in served
                if any(p == x or p.startswith(f"{x}/") for x in reconciliation)], sorted(served)
    # the core surface is untouched
    assert any(p.endswith("/entries") for p in served)
    assert any(p.endswith("/reports/trial-balance") for p in served)


def _surface(fx):
    """The reads and writes an app built under the current switch derives (issue 0006).

    An app is built once and its routers are mounted then, so the switch is read at construction rather than at
    every request.  That is why this builds an app instead of calling a function twice under two settings: the
    served surface cannot change under a running process.
    """
    from haddon.api import surface

    app = build_api({fx.entity_id: fx.path})[1].app
    reads, writes = surface.published(app)
    return [r.path for r in reads], [f"{w.method} {w.path}" for w in writes]


def test_capabilities_publishes_only_what_it_serves(fx, off):
    reads, writes = _surface(fx)
    reconciliation = ("/statements", "/lines", "/reconciliation", "/provenance")
    assert not [p for p in reads + writes if any(p == x or p.startswith(f"{x}/") for x in reconciliation)]
    assert "POST /entries" in writes and "/reports/trial-balance" in reads


def test_turning_it_back_on_restores_exactly_the_reconciliation_surface(fx):
    """The difference the switch makes, named exactly, so an extension route that leaks into the core surface
    or a core route that goes missing with the extension fails here."""
    on_reads, on_writes = _surface(fx)
    with extensions.without(extensions.RECONCILIATION):
        off_reads, off_writes = _surface(fx)
    assert sorted(set(on_reads) - set(off_reads)) == [
        "/lines", "/lines/{line_id}", "/provenance", "/reconciliation", "/statements", "/statements/{statement_id}",
        "/statements/{statement_id}/lines"]
    assert sorted(set(on_writes) - set(off_writes)) == [
        "POST /lines/{line_id}/book", "POST /lines/{line_id}/ignore", "POST /lines/{line_id}/link",
        "POST /lines/{line_id}/reexplain", "POST /statements", "POST /statements/{statement_id}/replace"]
    # nothing the core serves disappears with the extension
    assert not set(off_reads) - set(on_reads) and not set(off_writes) - set(on_writes)


def test_a_disabled_extension_publishes_none_of_its_commands(fx, off):
    """``/capabilities`` names commands, and a named command that answers 404 is worse than an unnamed one."""
    api = build_api({fx.entity_id: fx.path})[1]
    with api:
        published = api.get(f"/v1/entities/{fx.entity_id}/capabilities",
                            headers={"Authorization": f"Bearer {API_TOKEN}"}).json()
    names = {c["name"] for c in published["commands"]}
    assert names and not names & {"load_statement", "replace_statement", "book_line", "link_line",
                                  "ignore_line", "reexplain_line"}
    assert {"post_entry", "close_period", "create_account"} <= names


# 3. the switch itself
def test_the_switch_is_read_from_the_environment_and_defaults_to_on(monkeypatch):
    monkeypatch.delenv("HADDON_RECONCILIATION", raising=False)
    assert extensions.enabled(extensions.RECONCILIATION) is True
    for value in ("0", "off", "false", "no", "OFF"):
        monkeypatch.setenv("HADDON_RECONCILIATION", value)
        assert extensions.enabled(extensions.RECONCILIATION) is False, value
    monkeypatch.setenv("HADDON_RECONCILIATION", "on")
    assert extensions.enabled(extensions.RECONCILIATION) is True


def test_an_unknown_extension_is_a_mistake_and_says_so():
    with pytest.raises(KeyError):
        extensions.enabled("no-such-extension")


def test_disabling_it_deletes_nothing_that_was_already_loaded(fx):
    """Switching the extension off hides a surface; it must not make a loaded statement unreadable, or the
    switch would be a data-loss button."""
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),))
    with extensions.without(extensions.RECONCILIATION):
        assert fx.scalar("SELECT count(*) FROM statement_lines") == 1
        assert fx.scalar("SELECT id FROM statement_lines") == rows[0].id
