"""A month closes only when it is complete.

An installation running reconciliation has four checks: ``nothing_in_draft`` from the journal core, and
``statement_coverage`` / ``lines_explained`` / ``nothing_partial`` from the extension.  A refusal names which one
failed and what is outstanding.  The rule everything rests on: a check that cannot answer blocks instead of
passing.

The policy is a literal tuple of ``Check`` records (issue 0005), so "a required key with nothing registered"
cannot happen.  The reconciliation checks live with the extension, so a switched-off extension contributes no
checks at all rather than three passes.

The check tests run against chunk A's seeded template, the only fixture that holds real statements, lines,
explanations and allocations written through the real triggers.  The ``fx`` fixture (a provisioned chart and
nothing else) is the empty case: no statement has been loaded, so the statement-side checks cannot evaluate,
which is the fail-closed path this has to get right.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from haddon import commands
from haddon.domain.errors import HaddonError
from haddon.repository import close_checks as cc
from haddon.repository import closing as cl
from haddon.repository import entries as en
from haddon.repository.transaction import write_transaction

from repo_helpers import AGENT, HUMAN, OVERRIDE_REASON, blocking_overrides, lock_books, m, pair, post

SEED_ENTITY = "E"
THROUGH = "2026-03-31"

#: Every check this installation runs.  Read from the policy rather than restated, so a check added or removed
#: cannot leave a test asserting about a policy that no longer exists.
POLICY_KEYS = {c.key for c in cc.policy()}


def err(ei) -> HaddonError:
    assert isinstance(ei.value, HaddonError)
    return ei.value


def seed_window(fx) -> None:
    """One posted entry so a first close has a window at all.

    With no annual book, the window starts at the earliest entry the books hold.  Non-cash, so it moves nothing
    on the cash accounts check 1 asks about."""
    post(fx, "2025-01-01", "1000000", debit="expenses:office", credit="income:sales")


def stage(engine, *statements: str) -> None:
    """Run raw staging SQL through a guarded write transaction, so every trigger fires exactly as in production.

    A test that needs a particular staged row writes it itself, past the same trigger suite a real writer meets.
    """
    with write_transaction(engine) as conn:
        for s in statements:
            conn.execute(sa.text(s))


def checks(engine, through: str = THROUGH, **kw) -> cc.CloseChecks:
    with engine.connect() as conn:
        return cc.close_checks(conn, SEED_ENTITY, through=through, **kw)


def kinds(result: cc.CheckResult) -> list[str]:
    return sorted({o.kind for o in result.outstanding})


def counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        names = [r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))]
        return {n: conn.execute(sa.text(f'SELECT count(*) FROM "{n}"')).scalar_one() for n in names}


# Close refuses an incomplete month
def test_close_refuses_an_incomplete_month_and_names_every_blocking_check(fx):
    """A close with no overrides is refused, with 409 ``close_not_eligible`` and one detail per blocking check:
    its key, status, human-readable reason and the outstanding items."""
    seed_window(fx)
    before = fx.counts()
    with pytest.raises(HaddonError) as ei:
        with fx.tx(AGENT) as ctx:
            commands.close_period(ctx, through=THROUGH, reason="r", expected_version=1)
    e = err(ei)
    assert (e.code, e.status) == ("close_not_eligible", 409)
    assert {d["check"] for d in e.details} == POLICY_KEYS - {"nothing_in_draft"}
    for detail in e.details:
        assert detail["blocking"] is True and detail["status"] != "pass"
        assert detail["reason"] and detail["requirement"] and detail["through"] == THROUGH
    # The whole transaction rolled back: no close record, no lock movement, no snapshot left behind.
    assert fx.counts() == before
    assert fx.q("SELECT through, version FROM period_locks") == [(None, 1)]


def test_eligibility_is_checked_after_every_mechanic_that_was_already_there(fx):
    """Eligibility is a precondition applied last, not a replacement.

    The lock version, "close must advance" and the missing reason all refuse first, with their own codes.
    """
    seed_window(fx)
    lock_books(fx, "2026-02-28")
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:                                    # stale version wins
            commands.close_period(ctx, through=THROUGH, reason="r", expected_version=99)
        assert err(ei).code == "stale_version"
        with pytest.raises(HaddonError) as ei:                                    # not advancing wins
            commands.close_period(ctx, through="2026-01-31", reason="r", expected_version=2)
        assert err(ei).details[0]["reason"] == "not_advancing"
        with pytest.raises(HaddonError) as ei:                                    # a missing reason wins
            commands.close_period(ctx, through=THROUGH, reason="  ", expected_version=2)
        assert err(ei).details[0]["path"] == "reason"
        with pytest.raises(HaddonError) as ei:                                    # only then: eligibility
            commands.close_period(ctx, through=THROUGH, reason="r", expected_version=2)
        assert err(ei).code == "close_not_eligible"


# The fail-closed rule, from four sides
def _raises(ctx):
    raise RuntimeError("the statement store is unreachable")


def _malformed(ctx):
    return cc.CheckOutcome("ok", "everything is fine, honest")


def _passes(ctx):
    return cc.CheckOutcome(cc.PASS, "nothing partial")


RAISES = cc.Check("statement_coverage", "raises", "r", _raises)
MALFORMED = cc.Check("lines_explained", "malformed", "r", _malformed)
PASSES = cc.Check("nothing_partial", "passes", "r", _passes)


def test_an_unevaluable_check_blocks_the_close_and_never_silently_passes(fx):
    """The most important test here.  Every way a check can fail to say "pass" must block, and must report
    itself as not evaluated rather than as a specific finding it did not make."""
    seed_window(fx)
    with fx.reader() as conn:
        ck = cc.close_checks(conn, fx.entity_id, through=THROUGH, checks=[RAISES, MALFORMED, PASSES])

    raised = ck.check("statement_coverage")                                   # 1. the check raised
    assert (raised.status, raised.blocking) == ("unevaluable", True)
    assert raised.reason.startswith("could not be evaluated")
    assert kinds(raised) == ["check_raised"]
    assert "the statement store is unreachable" in raised.outstanding[0].detail["error"]

    malformed = ck.check("lines_explained")                                   # 2. the check returned a non-status
    assert (malformed.status, malformed.blocking) == ("unevaluable", True)
    assert malformed.reason.startswith("could not be evaluated") and kinds(malformed) == ["check_malformed"]

    assert ck.check("nothing_partial").status == "pass"                       # 3. control: a pass is honoured
    assert not ck.eligible and not ck.may_close
    assert {g.key for g in ck.blocking} == {"statement_coverage", "lines_explained"}

    # A close that ran only the one passing check is still refused, because a pass is not a majority.
    with pytest.raises(HaddonError) as ei:
        with fx.reader() as conn:
            cc.require_eligible(conn, fx.entity_id, through=THROUGH, checks_override=[RAISES, PASSES])
    assert err(ei).code == "close_not_eligible"


def test_an_extension_that_is_on_but_not_installed_blocks_instead_of_passing_vacuously(fx):
    """An installation running reconciliation whose close checks were never imported blocks.

    A literal policy cannot lose an implementation, but the extension's checks can still fail to be imported.
    Then the core check alone would go green and the close would succeed on a question nobody asked.  It blocks
    instead, and says which extension is missing.
    """
    seed_window(fx)
    installed = cc._EXTENSION_CHECKS.pop(cc_extension(), None)
    try:
        keys = [c.key for c in cc.policy()]
        assert keys == ["nothing_in_draft", "reconciliation_checks"]
        with fx.reader() as conn:
            ck = cc.close_checks(conn, fx.entity_id, through=THROUGH)
        stand_in = ck.check("reconciliation_checks")
        assert (stand_in.status, stand_in.blocking) == ("unevaluable", True)
        assert kinds(stand_in) == ["extension_checks_not_installed"]
        assert stand_in.outstanding[0].detail["extension"] == "reconciliation"
        assert ck.check("nothing_in_draft").status == "pass"          # the core check alone would have passed
        assert not ck.may_close
    finally:
        if installed is not None:
            cc.register_checks(cc_extension(), installed)
    assert {c.key for c in cc.policy()} == POLICY_KEYS


def cc_extension() -> str:
    from haddon import extensions
    return extensions.RECONCILIATION


def test_an_unevaluable_check_says_so_and_does_not_invent_a_specific_finding(fx):
    """``fx`` has no statement at all, so the statement-side checks cannot answer.  They must say so in those
    words: never a bare "not eligible", and never an empty query dressed up as a pass."""
    seed_window(fx)
    with fx.reader() as conn:
        ck = cc.close_checks(conn, fx.entity_id, through=THROUGH)
    assert ck.check("nothing_in_draft").status == "pass"   # the core check is evaluable with nothing loaded
    for key in ("lines_explained", "nothing_partial"):
        g = ck.check(key)
        assert (g.status, g.blocking) == ("unevaluable", True), key
        assert "could not be evaluated" in g.reason, (key, g.reason)
        assert "check_unevaluable" in kinds(g), (key, kinds(g))
        # It did not claim to have found unexplained or partly allocated lines: there are none loaded to find.
        assert "statement_line_unexplained" not in kinds(g) and "statement_line_partially_allocated" not in kinds(g)
        assert all("no statement is loaded" in o.message for o in g.outstanding if o.kind == "check_unevaluable")
    # Check 1, by contrast, is wired against what exists: it reports a real, specific finding.
    one = ck.check("statement_coverage")
    assert one.status == "fail" and "statement_not_loaded" in kinds(one)
    assert {o.detail["account_path"] for o in one.outstanding} == {"assets:cash", "assets:cash-eur", "assets:cash-jpy"}


def test_an_entity_with_no_cash_account_cannot_establish_coverage_either(repo_engine):
    """The degenerate case that would otherwise pass vacuously: nothing to check is not the same as all checked.

    A chart with no cash account gives every statement-side check an empty question.  An empty question has no
    answer, so all three report ``unevaluable`` and the close stays shut.
    """
    from haddon.repository import provisioning as pv

    with write_transaction(repo_engine) as conn:
        ctx = pv.provision_entity(conn, actor=HUMAN, id="E-nocash", name="No Cash Ltd", legal_form="Inc",
                                  country="US", base_currency="USD", tax_regime={})
        pv.add_commodity(ctx, code="USD", precision=2)
        office = pv.add_account(ctx, path="expenses:office", type="expense", is_cash=False, commodities=["USD"])
        retained = pv.add_account(ctx, path="equity:retained-earnings", type="equity", is_cash=False,
                                  commodities=["USD"])
        # A first posted entry, so the continuous journal has a close window at all; no cash account moves.
        en.post_entry(ctx, dt="2026-03-05", narration="opening accrual", origin="manual",
                      legs=[en.Leg(office.id, "USD", m("1000000")), en.Leg(retained.id, "USD", m("-1000000"))])
    with repo_engine.connect() as conn:
        ck = cc.close_checks(conn, "E-nocash", through=THROUGH)
    for g in ck.checks:
        if g.key == "nothing_in_draft":
            continue  # the core check asks about drafts, not cash: it is evaluable and it passes
        assert (g.status, g.blocking) == ("unevaluable", True), g.key
        assert "no_cash_accounts_configured" in kinds(g), (g.key, kinds(g))
        assert "could not be evaluated" in g.reason
    assert not ck.may_close


# The checks, evaluated against real staged workflow data
def test_each_check_names_the_right_outstanding_items(locked_seeded_engine):
    """Against chunk A's seeded workflow rows each check finds its own problems, and describes them precisely
    enough to act on: ids, account, commodity, month and amounts as integer micro strings."""
    ck = checks(locked_seeded_engine)
    assert [m.as_json() for m in ck.months] == [{"month": "2026-03", "start": "2026-03-01", "end": "2026-03-31"}]

    one = ck.check("statement_coverage")
    assert one.status == "fail"
    # A statement is its own closing-balance assertion (issue 0004), so there is no separate "no balance was
    # asserted" finding: a loaded statement always carries one.
    assert kinds(one) == ["closing_balance_disagrees"]
    disagree = one.outstanding[0]
    assert disagree.detail["account_id"] == "cash" and disagree.detail["statement_id"] == "s1"
    # The seeded March statement says 1.00; the seeded March ledger says 62.00.
    assert (disagree.detail["asserted_micros"], disagree.detail["ledger_micros"],
            disagree.detail["difference_micros"]) == ("1000000", "62000000", "61000000")

    # A draft entry is the core's business and an unexplained statement line is the extension's (issue 0005).
    # They are evaluable under different conditions.
    drafts = ck.check("nothing_in_draft")
    assert drafts.status == "fail" and kinds(drafts) == ["entry_not_posted"]
    assert {o.detail["entry_id"] for o in drafts.outstanding} == {"e_draft", "e_draft2"}

    two = ck.check("lines_explained")
    assert two.status == "fail" and kinds(two) == ["statement_line_unexplained"]
    # r1 is linked in full, r2 partly (``nothing_partial``'s finding, not this one), r3 is ignored and r_eur's
    # live explanation supersedes an earlier one: none of those is unexplained.  r_jan is before the window.
    assert {o.detail["line_id"] for o in two.outstanding} == {"r4"}

    three = ck.check("nothing_partial")
    assert three.status == "fail"
    assert kinds(three) == ["posting_partially_allocated", "statement_line_partially_allocated"]
    held = next(o for o in three.outstanding if o.kind == "statement_line_partially_allocated").detail
    assert (held["line_id"], held["amount_micros"], held["allocated_micros"]) == ("r2", "50000000", "20000000")


def test_statement_coverage_passes_only_when_loaded_and_agreeing(locked_seeded_engine):
    """Positive control that check 1 is wired and not a permanent refusal.

    Replace the statement that disagrees with one that agrees and watch it go green, then break the agreement
    again and watch it go red.  A statement loaded with the wrong closing balance is corrected by a successor
    that points at it, never by editing the row (issue 0004).
    """
    eng = locked_seeded_engine
    assert checks(eng).check("statement_coverage").status == "fail"

    # Replace the March USD statement with one that agrees with the ledger (67.00).
    stage(eng, "INSERT INTO statements(id,entity_id,created_at,created_by,document_id,account_id,commodity,"
               "period_start,period_end,opening_micros,closing_micros,locator,reason,replaces_id) "
               "VALUES('s1b','E','2026-09-11T00:00:00Z','t','f4','cash','USD','2026-03-01','2026-03-31',"
               "-7000000,62000000,'p1','the download was truncated','s1')")
    assert checks(eng).check("statement_coverage").status == "pass"

    # A posting that moves the ledger without a new statement breaks the agreement again, by exactly its amount.
    stage(eng, "INSERT INTO entries(id,entity_id,created_at,created_by,dt,narration,origin) "
               "VALUES('e_late','E','2026-09-11T00:00:00Z','t','2026-03-20','late','manual')",
          "INSERT INTO postings(id,entity_id,created_at,created_by,entry_id,seq,account_id,commodity,amount_micros) "
          "VALUES('e_late.p1','E','2026-09-11T00:00:00Z','t','e_late',1,'cash','USD',3000000)",
          "INSERT INTO postings(id,entity_id,created_at,created_by,entry_id,seq,account_id,commodity,amount_micros) "
          "VALUES('e_late.p2','E','2026-09-11T00:00:00Z','t','e_late',2,'exp','USD',-3000000)",
          "UPDATE entries SET status='posted', version=version+1 WHERE id='e_late'")
    broken = checks(eng).check("statement_coverage")
    assert broken.status == "fail" and kinds(broken) == ["closing_balance_disagrees"]
    assert broken.outstanding[0].detail["difference_micros"] == "3000000"


def test_the_line_side_checks_pass_once_every_line_is_explained_in_full(locked_seeded_engine):
    """Positive control for checks 2 and 3: post the drafts, ignore the loose line, and supersede the partial
    explanation with one that ignores its line outright, so nothing is left half-allocated."""
    eng = locked_seeded_engine
    stage(eng,
          "UPDATE entries SET status='posted', version=version+1 WHERE id IN ('e_draft','e_draft2')",
          "INSERT INTO explanations(id,entity_id,created_at,created_by,line_id,kind,reason) "
          "VALUES('x_r4','E','2026-09-11T00:00:00Z','t','r4','ignored','not a transaction')",
          "INSERT INTO explanations(id,entity_id,created_at,created_by,line_id,kind,reason,supersedes_id) "
          "VALUES('x_r2b','E','2026-09-11T00:00:00Z','t','r2','ignored','not a transaction','x_partial')")
    ck = checks(eng)
    for key in ("nothing_in_draft", "lines_explained", "nothing_partial"):
        assert ck.check(key).status == "pass", [o.as_json() for o in ck.check(key).outstanding]
    # Statement coverage still fails, and that alone still blocks: the checks are a conjunction.
    assert ck.check("statement_coverage").status == "fail" and not ck.may_close


def test_a_partly_allocated_line_and_a_partly_allocated_posting_are_both_named(locked_seeded_engine):
    """Check 3's two halves, on the seed's own partial explanation.  ``x_partial`` allocates 20.00 of r2's 50.00,
    and the same allocation leaves the posting it touched partly unallocated.  That is visible from the posting
    side even when the line is whole, which is why the check asks both questions."""
    three = checks(locked_seeded_engine).check("nothing_partial")
    assert three.status == "fail"
    assert {"statement_line_partially_allocated", "posting_partially_allocated"} <= set(kinds(three))
    line = next(o for o in three.outstanding if o.kind == "statement_line_partially_allocated").detail
    assert (line["line_id"], line["amount_micros"], line["allocated_micros"], line["unallocated_micros"]) == \
        ("r2", "50000000", "20000000", "30000000")
    posting = next(o for o in three.outstanding if o.kind == "posting_partially_allocated").detail
    assert (posting["posting_id"], posting["allocated_micros"], posting["unallocated_micros"]) == \
        ("e_posted2.p1", "20000000", "30000000")


def test_the_checks_do_not_count_an_allocation_a_replacement_already_retired(locked_seeded_engine):
    """One definition of "live", and the Python readers must use the SQL one (issue 0004).

    ``live_explanations`` requires a live statement, so replacing a statement retires its lines' explanations
    and their allocations stop consuming their postings.  A reader that checked only ``supersedes_id`` would
    count the same posting re-linked from the replacement's line twice: the seed's fully allocated
    r1/``e_posted.p1`` pair would show -24.00 against a -12.00 posting and fail ``nothing_partial`` over an
    allocation the database had already let go.
    """
    eng = locked_seeded_engine
    stage(eng,
          # Replace the March USD statement, restating r1 as r1b: same line, same posting.
          "INSERT INTO statements(id,entity_id,created_at,created_by,document_id,account_id,commodity,"
          "period_start,period_end,opening_micros,closing_micros,locator,reason,replaces_id) "
          "VALUES('s1b','E','2026-09-11T00:00:00Z','t','f4','cash','USD','2026-03-01','2026-03-31',"
          "-7000000,62000000,'p1','the download was truncated','s1')",
          "INSERT INTO statement_lines(id,entity_id,created_at,created_by,statement_id,line_no,source_key,"
          "content_hash,dt,amount_micros,description,fields,raw) "
          "VALUES('r1b','E','2026-09-11T00:00:00Z','t','s1b',1,'k1','rh1','2026-03-10',-12000000,'d','{}','raw')",
          "INSERT INTO explanations(id,entity_id,created_at,created_by,line_id,kind,reason) "
          "VALUES('x_r1b','E','2026-09-11T00:00:00Z','t','r1b','linked','the same money, restated')",
          "INSERT INTO allocations(id,entity_id,created_at,created_by,explanation_id,line_id,posting_id,"
          "amount_micros) VALUES('al1b','E','2026-09-11T00:00:00Z','t','x_r1b','r1b','e_posted.p1',-12000000)")
    with write_transaction(eng) as conn:
        live = conn.exec_driver_sql(
            "SELECT count(*) FROM live_allocations WHERE posting_id='e_posted.p1'").scalar()
    assert live == 1, "the database holds one live allocation against that posting"
    partial = checks(eng).check("nothing_partial")
    named = [o.detail for o in partial.outstanding if o.detail.get("posting_id") == "e_posted.p1"]
    assert named == [], f"the check counted a retired allocation: {named}"


def test_a_balance_that_cannot_be_computed_is_outstanding_and_never_agreement(locked_seeded_engine, monkeypatch):
    """An assertion we cannot check against is not an assertion that agrees.  Whatever goes wrong computing the
    ledger side (an unknown account, an overflow, a bad book) becomes an outstanding item, not a pass."""
    import haddon.reports.balances as balances

    def boom(*a, **kw):
        raise RuntimeError("the balance could not be computed")

    monkeypatch.setattr(balances, "account_balance", boom)
    one = checks(locked_seeded_engine).check("statement_coverage")
    assert one.status == "fail" and "ledger_balance_unavailable" in kinds(one)
    unavailable = next(o for o in one.outstanding if o.kind == "ledger_balance_unavailable")
    assert (unavailable.detail["statement_id"], unavailable.detail["as_of"]) == ("s1", "2026-03-31")
    assert "closing_balance_disagrees" not in kinds(one)   # it did not invent an agreement OR a disagreement


def _floods(ctx):
    return cc.CheckOutcome(cc.FAIL, "a great many things",
                          tuple(cc.Outstanding("k", f"item {i}", {"i": str(i)}) for i in range(150)))


FLOODS = cc.Check("nothing_partial", "floods", "r", _floods)


def test_a_flood_of_outstanding_items_is_counted_not_silently_dropped(fx):
    """A refusal has to stay readable, but "readable" must never shade into "shorter than the truth"."""
    seed_window(fx)
    with fx.reader() as conn:
        ck = cc.close_checks(conn, fx.entity_id, through=THROUGH, checks=[FLOODS])
    g = ck.check("nothing_partial")
    assert len(g.outstanding) == cc.MAX_OUTSTANDING and g.truncated == 150 - cc.MAX_OUTSTANDING
    assert g.as_json()["outstanding_truncated"] == 50 and g.blocking


def test_the_window_is_every_month_the_close_newly_closes_including_empty_ones(fx):
    """Coverage is demanded for inactive-looking months too, and a part-month close asks for a part-month."""
    seed_window(fx)
    assert [m.month for m in cc.months_between("2026-01-15", "2026-03-10")] == ["2026-01", "2026-02", "2026-03"]
    assert cc.months_between("2026-01-15", "2026-03-10")[0].start == "2026-01-15"
    assert cc.uncovered("2026-03-01", "2026-03-31", [("2026-01-01", "2026-12-31")]) == []
    assert cc.uncovered("2026-03-01", "2026-03-31", [("2026-03-01", "2026-03-10"), ("2026-03-20", "2026-03-25")]) == [
        ("2026-03-11", "2026-03-19"), ("2026-03-26", "2026-03-31")]
    # A first close (no lock at all) reaches back to the earliest posted entry.
    with fx.reader() as conn:
        first = cc.close_checks(conn, fx.entity_id, through="2025-01-31")
    assert first.window_start == "2025-01-01" and first.prior_through is None
    # After a close, the window is only what the next close newly closes.
    lock_books(fx, "2026-02-28")
    with fx.reader() as conn:
        ck = cc.close_checks(conn, fx.entity_id, through="2026-05-15")
    assert (ck.window_start, ck.prior_through) == ("2026-03-01", "2026-02-28")
    assert [m.month for m in ck.months] == ["2026-03", "2026-04", "2026-05"] and ck.months[-1].end == "2026-05-15"


def test_a_draft_dated_before_the_earliest_posted_entry_is_in_the_first_window(fx):
    """The regression this window rule exists for (issue 0005 review, finding 1).

    A first close locks every date on or before ``through``, so a draft dated before the earliest posted entry
    is locked by it.  A window starting at the earliest posted entry would never look at that draft:
    ``nothing_in_draft`` would pass and the close record would assert "no entry in the period is still a draft"
    about a period that held one, while ``seal`` on that draft answered 409 ``period_locked``.
    """
    post(fx, "2026-03-10", "1000000", debit="expenses:office", credit="income:sales")
    with fx.tx(AGENT) as ctx:
        draft = en.create_draft(ctx, dt="2026-01-15", narration="an early draft", origin="manual",
                                legs=pair(fx, "500000", debit="expenses:office", credit="income:sales"))

    with fx.reader() as conn:
        ck = cc.close_checks(conn, fx.entity_id, through=THROUGH)
    assert ck.window_start == "2026-01-15"
    g = ck.check("nothing_in_draft")
    assert g.status == cc.FAIL and g.blocking
    assert [o.detail["entry_id"] for o in g.outstanding] == [draft.id]

    # The close is refused, naming that draft rather than the lock it would have set.
    with pytest.raises(HaddonError) as ei:
        with fx.tx(HUMAN) as ctx:
            commands.close_period(ctx, through=THROUGH, reason="r", expected_version=1)
    assert "nothing_in_draft" in {d.get("check") for d in err(ei).details}


def test_widening_the_first_window_never_narrows_what_the_other_checks_report(fx):
    """The window is the one lower bound every check shares, so moving it is only safe in one direction.

    Reaching further back can only give a check more to find, since the months it iterates and the dates it
    filters on both grow.  With the early draft present, the statement-side checks report over January too: the
    month the close would otherwise lock without asking about it.
    """
    post(fx, "2026-03-10", "1000000", debit="expenses:office", credit="income:sales")
    with fx.reader() as conn:
        narrow = cc.close_checks(conn, fx.entity_id, through=THROUGH)
    with fx.tx(AGENT) as ctx:
        en.create_draft(ctx, dt="2026-01-15", narration="an early draft", origin="manual",
                        legs=pair(fx, "500000", debit="expenses:office", credit="income:sales"))
    with fx.reader() as conn:
        wide = cc.close_checks(conn, fx.entity_id, through=THROUGH)

    assert [m.month for m in narrow.months] == ["2026-03"]
    assert [m.month for m in wide.months] == ["2026-01", "2026-02", "2026-03"]
    for g in narrow.checks:
        assert wide.check(g.key).blocking >= g.blocking, f"{g.key} stopped blocking once the window grew"
    covered = {(o.detail.get("month"), o.detail.get("account_path")) for o in wide.check("statement_coverage").outstanding}
    assert {"2026-01", "2026-02", "2026-03"} <= {month for month, _ in covered}


def test_a_void_entry_does_not_drag_the_first_window_back(fx):
    """Void is the one status left out, and on purpose.

    No close check has a finding to make about a voided entry, since it is not bookkeeping the books hold.
    Counting it would mean that voiding an entry misdated by a decade made every later first close owe statement
    coverage for the empty decade in between.
    """
    post(fx, "2026-03-10", "1000000", debit="expenses:office", credit="income:sales")
    with fx.tx(AGENT) as ctx:
        stray = en.create_draft(ctx, dt="2016-02-02", narration="misdated by a decade", origin="manual",
                                legs=pair(fx, "500000", debit="expenses:office", credit="income:sales"))
    with fx.tx(AGENT) as ctx:
        en.void(ctx, stray.id, expected_version=stray.version)
    with fx.reader() as conn:
        ck = cc.close_checks(conn, fx.entity_id, through=THROUGH)
    assert ck.window_start == "2026-03-10"
    assert ck.check("nothing_in_draft").status == cc.PASS


# Reading eligibility never writes
def test_reading_eligibility_never_mutates(fx, locked_seeded_engine):
    """Eligibility is a read.  Checked two ways: no row count moves, and the whole evaluation runs to completion
    on a connection SQLite itself refuses writes on."""
    seed_window(fx)
    before = fx.counts()
    with fx.reader() as conn:
        assert not cc.close_checks(conn, fx.entity_id, through=THROUGH).may_close
    assert fx.counts() == before

    seeded_before = counts(locked_seeded_engine)
    from haddon.db.connection import make_engine
    read_only = make_engine(locked_seeded_engine.url.database, immediate=False)
    try:
        with read_only.connect() as conn:
            conn.execute(sa.text("PRAGMA query_only=ON"))
            assert conn.execute(sa.text("PRAGMA query_only")).scalar_one() == 1
            with pytest.raises(Exception):  # the control: this connection really does refuse writes
                conn.execute(sa.text("UPDATE period_locks SET version=version+1"))
        with read_only.connect() as conn:
            conn.execute(sa.text("PRAGMA query_only=ON"))
            ck = cc.close_checks(conn, SEED_ENTITY, through=THROUGH)   # the whole evaluation, writes forbidden
    finally:
        read_only.dispose()
    assert len(ck.checks) == len(POLICY_KEYS) and not ck.may_close and ck.check("statement_coverage").outstanding
    assert counts(locked_seeded_engine) == seeded_before


# Reopen is not gated on eligibility
def test_reopen_stays_unconditional_on_eligibility(fx):
    """Reopening is how an incomplete month gets fixed.  Gating it on completeness would trap the books: the
    month that most needs reopening is the one that cannot pass the checks."""
    seed_window(fx)
    lock_books(fx, THROUGH)
    with fx.reader() as conn:
        assert not cc.close_checks(conn, fx.entity_id, through="2026-04-30").may_close
    with fx.tx(HUMAN) as ctx:                       # no overrides, no waiver, nothing: reopen just works
        reopened = commands.reopen_period(ctx, through="2026-02-28", reason="late invoice", expected_version=2)
    assert (reopened.lock.through, reopened.lock.version) == ("2026-02-28", 3)
    assert post(fx, "2026-03-15", "10000").status == "posted"        # March really is open again


# The override
def test_an_override_is_scoped_to_one_check_and_one_close(fx):
    """A waiver is not a switch.  It names one check, answers that check's current finding, and lives only as
    long as the close it let through.  The next close re-evaluates from scratch and needs its own."""
    seed_window(fx)
    with fx.reader() as conn:
        blocking = blocking_overrides(conn, fx.entity_id, THROUGH)
        one = [o for o in blocking if o.check == "statement_coverage"]
        ck = cc.close_checks(conn, fx.entity_id, through=THROUGH, overrides=one)
    assert ck.check("statement_coverage").waiver is not None
    assert {g.key for g in ck.blocking} == {"lines_explained", "nothing_partial"}   # the others still block
    assert not ck.may_close

    # Close on the full set, then look at what the next close sees: nothing carries over.
    with fx.tx(AGENT) as ctx:
        commands.close_period(ctx, through=THROUGH, reason="close on exception", expected_version=1,
                              overrides=blocking)
    post(fx, "2026-04-10", "10000")
    with fx.reader() as conn:
        after = cc.close_checks(conn, fx.entity_id, through="2026-04-30")
    assert all(g.waiver is None for g in after.checks) and not after.may_close


def test_an_override_lets_the_close_through_without_calling_the_month_complete(fx):
    """``eligible`` is the truth about the books; ``may_close`` is whether a close is permitted right now.  An
    override moves the second and never the first, and the real finding stays visible beside the waiver."""
    seed_window(fx)
    with fx.reader() as conn:
        overrides = blocking_overrides(conn, fx.entity_id, THROUGH)
        ck = cc.close_checks(conn, fx.entity_id, through=THROUGH, overrides=overrides, actor=AGENT)
    assert ck.may_close and not ck.eligible
    assert {o.check for o in overrides} == POLICY_KEYS - {"nothing_in_draft"}
    for g in ck.checks:
        if g.key == "nothing_in_draft":
            assert g.status == "pass" and g.waiver is None      # nothing to waive: it was never blocking
            continue
        assert g.status != "pass" and g.blocking is False and g.waiver is not None
        assert g.outstanding, g.key                       # the finding was not erased, only waived
        assert g.waiver.actor == AGENT and g.waiver.through == THROUGH
        assert g.waiver.outstanding_at_close == len(g.outstanding) + g.truncated

    with fx.tx(AGENT) as ctx:
        closed = commands.close_period(ctx, through=THROUGH, reason="close on exception", expected_version=1,
                                       overrides=overrides)
    assert closed.lock.through == THROUGH


def test_the_override_and_the_evaluated_checks_reach_the_audit_trail(fx):
    """A close on exception is loud and permanent, and recorded in two places.

    The waivers are on the close record itself, and one ``close.eligibility`` event carries every check's real
    status beside them.  There is no quiet way past a check, and no override that outlives its close."""
    seed_window(fx)
    lock_books(fx, THROUGH, override_reason="the bank has not sent March yet; closing on a recorded exception")

    close_id = fx.scalar("SELECT id FROM close_records")
    stored = {w["check"]: w for w in json.loads(fx.scalar("SELECT overrides FROM close_records WHERE id=?",
                                                         (close_id,)))}
    assert set(stored) == POLICY_KEYS - {"nothing_in_draft"}
    for check, waiver in stored.items():
        assert waiver["through"] == THROUGH and waiver["check"] == check
        assert waiver["status_at_close"] in ("fail", "unevaluable")
        assert waiver["outstanding_at_close"] > 0                  # how much was wrong, recorded at the time
        assert waiver["reason"] == "the bank has not sent March yet; closing on a recorded exception"
        assert waiver["actor"] == HUMAN and waiver["reason_at_close"]

    trail = fx.q("SELECT subject_kind, subject_id, after FROM events WHERE kind='close.eligibility'")
    assert len(trail) == 1
    subject_kind, subject_id, after = trail[0]
    assert (subject_kind, subject_id) == ("close_record", close_id)
    body = json.loads(after)
    assert body["through"] == THROUGH and body["may_close"] is True and body["eligible"] is False
    assert set(body["waived_checks"]) == POLICY_KEYS - {"nothing_in_draft"}
    assert {g["status"] for g in body["checks"]} <= {"pass", "fail", "unevaluable"}
    assert body["extensions"] == {"reconciliation": True}
    assert all(body["outstanding_counts"][k] > 0 for k in stored)

    # The audit action names the same waivers, so a reader who follows it never has to join a table.
    action = json.loads(fx.scalar("SELECT after FROM events WHERE kind='period.closed'"))
    assert {o["check"] for o in action["result"]["overrides"]} == set(stored)
    assert action["result"]["eligible"] is False


def test_an_override_validates_its_own_inputs(fx):
    """Everything an override can be wrong about is refused before anything is written."""
    seed_window(fx)
    with fx.tx(AGENT) as ctx:
        for override, reason in ((cc.Override(check="no-such-check", reason="r"), "check_unknown"),
                                 (cc.Override(check="nothing_partial", reason="   "), "required")):
            with pytest.raises(HaddonError) as ei:
                commands.close_period(ctx, through=THROUGH, reason="r", expected_version=1, overrides=[override])
            assert err(ei).details[0]["reason"] == reason
    assert fx.scalar("SELECT count(*) FROM close_records") == 0


# The read endpoint
@pytest.mark.slow
def test_close_checks_endpoint_reports_eligibility_without_changing_anything(api_inc):
    before = api_inc.scalar("SELECT max(seq) FROM events")
    body = api_inc.json("/close-checks?through=2025-12-31")
    assert body["entity_id"] == api_inc.entity_id and body["through"] == "2025-12-31"
    assert body["may_close"] is False and body["eligible"] is False
    assert {g["check"] for g in body["checks"]} == POLICY_KEYS
    assert set(body["blocking_checks"]) == POLICY_KEYS - {"nothing_in_draft"}
    for check in body["checks"]:
        assert check["status"] in ("pass", "fail", "unevaluable") and check["reason"] and check["requirement"]
        assert check["waiver"] is None
        for item in check["outstanding"]:
            assert item["kind"] and item["message"]
    assert body["months"][0]["month"] == "2025-01" and body["months"][-1]["month"] == "2025-12"
    assert api_inc.scalar("SELECT max(seq) FROM events") == before
    assert api_inc.scalar("SELECT count(*) FROM close_records") == 0


@pytest.mark.slow
def test_close_checks_endpoint_refuses_a_malformed_date_and_an_unknown_override(api_inc):
    r = api_inc.get("/close-checks?through=2025-13-01")
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation"
    assert api_inc.get("/close-checks").status_code == 422
    r = api_inc.get("/close-checks?through=2025-12-31&override=no-such-check")
    assert r.status_code == 422 and r.json()["error"]["details"][0]["reason"] == "check_unknown"


@pytest.mark.slow
def test_close_checks_is_listed_in_capabilities(api_inc):
    reads = api_inc.json("/capabilities")["reads"]
    assert f"/v1/entities/{api_inc.entity_id}/close-checks" in reads


# The policy is a literal, not a registry
def test_the_policy_is_a_literal_and_nothing_else_names_a_check():
    """The policy is a literal, and ``closing.py`` names no check.

    ``closing.py`` asks ``require_eligible``, which asks the policy.  The policy is a tuple of ``Check`` records,
    so there is no "declared required but not registered" state: the declaration is the implementation.  The old
    registry names (``Gate``, ``register_gate``, ``REQUIRED_GATE_KEYS``) must not return.
    """
    import pathlib

    source = pathlib.Path(cl.__file__).read_text()
    for key in POLICY_KEYS:
        assert key not in source, f"closing.py names the check {key!r}"
    for dead in ("Gate", "register_gate", "registered_gates", "REQUIRED_GATE_KEYS", "_REGISTRY"):
        assert not hasattr(cc, dead), f"{dead} came back"
    assert all(isinstance(c, cc.Check) and c.requirement and c.title for c in cc.policy())
    assert [c.key for c in cc.CORE_CHECKS] == ["nothing_in_draft"], "the core's own policy is one check"


def test_a_switched_off_extension_contributes_no_checks_rather_than_passes(fx):
    """"Every statement is loaded" is not a check that passed when nobody was expected to load one.

    With the extension off its checks are not in the policy, and the close record says which extensions were
    running.  That stops a close read later from confusing "all green" with "fewer questions".
    """
    from haddon import extensions

    seed_window(fx)
    with extensions.without(extensions.RECONCILIATION):
        assert [c.key for c in cc.policy()] == ["nothing_in_draft"]
        with fx.reader() as conn:
            ck = cc.close_checks(conn, fx.entity_id, through=THROUGH)
        assert [g.key for g in ck.checks] == ["nothing_in_draft"] and ck.eligible and ck.may_close
        assert cc.checks_for_audit(ck)["extensions"] == {"reconciliation": False}
