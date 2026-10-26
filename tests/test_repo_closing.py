"""Closing and reopening a period: the public command, and the mechanics it is built on.

Issue 0005 made this one command.  ``commands.close_period`` takes a date, a reason, the lock version it expects
and, only where a check is blocking, an override naming that check and saying why.  It evaluates the checks,
computes and stores its own verification snapshot, advances the scalar lock and writes one audit action, all in
one transaction.  The audit action is the record of authority (``docs/architecture.md``).

Tested here: that contract, plus the mechanics underneath that a caller cannot reach directly (the
snapshot/close-date match, U7 that the lock can never be cleared, and the order the refusals come in).
"""

from __future__ import annotations

import pytest

from haddon import commands
from haddon.domain.errors import HaddonError
from haddon.repository import close_checks as cc
from haddon.repository import closing as cl

from repo_helpers import AGENT, HUMAN, OVERRIDE_REASON, blocking_overrides, lock_books, pair, post


def err(ei) -> HaddonError:
    assert isinstance(ei.value, HaddonError)
    return ei.value


def waive(ctx, through):
    """One override per blocking check.  An override is a check name and a reason.

    Every reconciliation check fails closed with no statement loaded, so a fixture close names them.  It can only
    name what is actually blocking, because an override that answers nothing is itself a refusal.
    """
    return blocking_overrides(ctx.conn, ctx.entity_id, through, reason=OVERRIDE_REASON)


def seed_activity(fx):
    """One posted entry, so the journal is non-empty: a continuous journal's first close needs something to close."""
    return post(fx, "2026-01-15", "10000")


def close(ctx, through, *, reason="month close", version, overrides=None):
    return commands.close_period(ctx, through=through, reason=reason, expected_version=version,
                                 overrides=waive(ctx, through) if overrides is None else overrides)


# the close command
def test_close_advances_the_lock_and_writes_one_record_with_its_own_snapshot(fx):
    with fx.reader() as conn:
        lock0 = cl.get_lock(conn, fx.entity_id)
    assert (lock0.through, lock0.version, lock0.close_id) == (None, 1, None)
    seed_activity(fx)
    with fx.tx(HUMAN) as ctx:
        closed = close(ctx, "2026-03-31", reason="Q1 close", version=1)
    assert (closed.lock.through, closed.lock.version) == ("2026-03-31", 2) and closed.lock.close_id is not None
    # the snapshot is the command's own: nobody passed one in, and it is taken through the close date
    assert closed.snapshot.snapshot_seq == 1 and closed.snapshot.through == "2026-03-31"
    assert len(closed.snapshot.state_hash) == 64
    assert fx.q("SELECT kind, through, prior_through, snapshot_id, reason FROM close_records") == [
        ("close", "2026-03-31", None, closed.snapshot.id, "Q1 close")]
    assert fx.q("SELECT through, version, close_id FROM period_locks") == [("2026-03-31", 2, closed.lock.close_id)]
    # a second close advances further and records the prior lock
    with fx.tx(HUMAN) as ctx:
        second = close(ctx, "2026-06-30", reason="Q2", version=2)
    assert (second.lock.through, second.lock.version) == ("2026-06-30", 3)
    assert fx.q("SELECT prior_through FROM close_records WHERE id=?", (second.close_id,)) == [("2026-03-31",)]


def test_the_snapshot_is_computed_from_the_books_and_verifies_them_per_commodity(fx):
    """It is a verification snapshot, not a receipt: it reads the posted journal, checks that every commodity
    nets to zero across the chart, and hashes what it read."""
    seed_activity(fx)
    with fx.tx(HUMAN) as ctx:
        closed = close(ctx, "2026-03-31", version=1)
    manifest, report, checks = fx.q(
        "SELECT manifest, report, checks FROM snapshots WHERE id=?", (closed.snapshot.id,))[0]
    import json
    manifest, report, checks = json.loads(manifest), json.loads(report), json.loads(checks)
    assert manifest["through"] == "2026-03-31" and manifest["entity_id"] == fx.entity_id
    assert manifest["balances"], "the snapshot must carry the balances it hashed, not just their digest"
    assert set(report["per_commodity_totals"].values()) == {"0"}
    assert checks == {"kind": "per_commodity_equilibrium", "result": "pass",
                      "commodities": sorted(report["per_commodity_totals"]), "through": "2026-03-31"}
    assert report["counts"]["entries"] >= 1 and report["counts"]["postings"] >= 2


def test_the_same_books_through_the_same_date_reuse_one_snapshot(fx):
    """``snapshots.state_hash`` is UNIQUE and one state is one snapshot.  Closing, reopening and closing again
    over an unchanged book re-derives the same hash, so the row is reused rather than refused."""
    seed_activity(fx)
    with fx.tx(HUMAN) as ctx:
        first = close(ctx, "2026-03-31", version=1)
    with fx.tx(HUMAN) as ctx:
        commands.reopen_period(ctx, through="2026-02-28", reason="a late invoice is coming", expected_version=2)
    with fx.tx(HUMAN) as ctx:
        again = close(ctx, "2026-03-31", reason="closed again", version=3)
    assert again.snapshot.id == first.snapshot.id and again.snapshot.state_hash == first.snapshot.state_hash
    assert fx.scalar("SELECT count(*) FROM snapshots WHERE through='2026-03-31'") == 1
    # and two close records point at the one snapshot, which is exactly what "one state is one snapshot" means
    assert fx.scalar("SELECT count(*) FROM close_records WHERE snapshot_id=?", (first.snapshot.id,)) == 2


def test_a_changed_book_produces_a_different_state_hash(fx):
    """The hash is only worth having if a changed book cannot reproduce it."""
    seed_activity(fx)
    with fx.tx(HUMAN) as ctx:
        first = close(ctx, "2026-03-31", version=1)
    with fx.tx(HUMAN) as ctx:
        commands.reopen_period(ctx, through="2026-02-28", reason="a late invoice arrived", expected_version=2)
    post(fx, "2026-03-20", "50000")
    with fx.tx(HUMAN) as ctx:
        again = close(ctx, "2026-03-31", reason="closed again", version=3)
    assert again.snapshot.state_hash != first.snapshot.state_hash


def test_a_stale_lock_version_is_412_and_writes_nothing(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            close(ctx, "2026-03-31", version=2, overrides=())
        e = err(ei)
        assert (e.code, e.status) == ("stale_version", 412) and e.details[0]["current_version"] == 1
    assert fx.q("SELECT through FROM period_locks") == [(None,)]
    assert fx.scalar("SELECT count(*) FROM close_records") == 0
    assert fx.scalar("SELECT count(*) FROM snapshots") == 0, "a refused close leaves no snapshot behind"


def test_a_close_that_does_not_advance_is_refused(fx):
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    with fx.tx(HUMAN) as ctx:
        for through in ("2026-03-31", "2026-02-28"):
            with pytest.raises(HaddonError) as ei:
                close(ctx, through, version=2, overrides=())
            assert err(ei).code == "validation" and err(ei).details[0]["reason"] == "not_advancing"
        assert close(ctx, "2026-04-30", version=2).lock.version == 3


def test_a_close_validates_its_date_and_insists_on_a_reason(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            commands.close_period(ctx, through="2026-3-31", reason="r", expected_version=1)
        assert err(ei).code == "validation" and err(ei).details[0]["path"] == "through"
        for blank in (None, "", "   "):
            with pytest.raises(HaddonError) as ei:
                commands.close_period(ctx, through="2026-03-31", reason=blank, expected_version=1)
            assert err(ei).details[0]["path"] == "reason"
    assert fx.scalar("SELECT count(*) FROM snapshots") == 0


# overrides on the record
def test_an_override_is_stored_on_the_close_it_let_through(fx):
    """There is no override table: the waiver lives on the close, with the check's status and outstanding count as
    they stood, the actor and the reason.  A record that outlived its close would be a standing permission."""
    import json

    seed_activity(fx)
    with fx.tx(AGENT) as ctx:
        closed = close(ctx, "2026-03-31", version=1)
    stored = json.loads(fx.scalar("SELECT overrides FROM close_records WHERE id=?", (closed.close_id,)))
    assert stored, "the fixture close is only possible through overrides, so they must be on the record"
    for waiver in stored:
        assert waiver["check"] in {c.key for c in cc.policy()}
        assert waiver["through"] == "2026-03-31"
        assert waiver["reason"] == OVERRIDE_REASON
        assert waiver["actor"] == AGENT
        assert waiver["status_at_close"] in (cc.FAIL, cc.UNEVALUABLE)
    assert {w["check"] for w in stored} == {g.key for g in closed.checks.checks if g.waiver is not None}


def test_an_override_never_makes_the_month_eligible(fx):
    """``eligible`` is whether the books are complete; ``may_close`` is whether a close is permitted right now.
    An override moves the second and never the first, and the real finding stays visible beside the waiver."""
    seed_activity(fx)
    with fx.tx(HUMAN) as ctx:
        closed = close(ctx, "2026-03-31", version=1)
    assert closed.checks.eligible is False and closed.checks.may_close is True
    waived = [g for g in closed.checks.checks if g.waiver is not None]
    assert waived and all(g.status != cc.PASS and g.blocking is False for g in waived)


def test_an_override_that_names_no_current_failure_is_refused(fx):
    """The acceptance criterion in one test: an override answers a problem that exists.  A list assembled once
    and replayed at every close stops working the moment its check goes green.

    Reconciliation is switched off here so that every check genuinely passes, which is the only state in which a
    close needs no override at all, and so the only state in which offering one is unambiguously wrong.
    """
    from haddon import extensions

    seed_activity(fx)
    with extensions.without(extensions.RECONCILIATION):
        with fx.tx(HUMAN) as ctx:
            assert cc.close_checks(ctx.conn, ctx.entity_id, through="2026-03-31").blocking == ()
            with pytest.raises(HaddonError) as ei:
                close(ctx, "2026-03-31", version=1,
                      overrides=[cc.Override(check="nothing_in_draft", reason="pre-emptive")])
            e = err(ei)
            assert (e.code, e.status) == ("validation", 422)
            assert e.details[0]["reason"] == "override_not_blocking"
            assert e.details[0]["checks"] == ["nothing_in_draft"]
            # and a check that is not in this installation's policy at all is a different refusal again:
            # with the extension off, "statement_coverage" is not a thing that can be overridden
            with pytest.raises(HaddonError) as ei:
                close(ctx, "2026-03-31", version=1,
                      overrides=[cc.Override(check="statement_coverage", reason="pre-emptive")])
            assert err(ei).details[0]["reason"] == "check_unknown"
        assert fx.scalar("SELECT count(*) FROM close_records") == 0
        # positive control: with nothing blocking, the same close with no override at all goes through
        with fx.tx(HUMAN) as ctx:
            closed = close(ctx, "2026-03-31", version=1, overrides=())
        assert closed.checks.eligible is True and closed.lock.through == "2026-03-31"


def test_an_override_needs_a_gate_that_exists_and_a_reason(fx):
    seed_activity(fx)
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            close(ctx, "2026-03-31", version=1, overrides=[cc.Override(check="make_it_go", reason="please")])
        assert err(ei).details[0]["reason"] == "check_unknown"
        with pytest.raises(HaddonError) as ei:
            close(ctx, "2026-03-31", version=1, overrides=[cc.Override(check="statement_coverage", reason="  ")])
        assert err(ei).details[0]["reason"] == "required"
        # the same check twice is a refusal, not a silent last-one-wins
        with pytest.raises(HaddonError) as ei:
            close(ctx, "2026-03-31", version=1,
                  overrides=[cc.Override(check="statement_coverage", reason="a"),
                             cc.Override(check="statement_coverage", reason="b")])
        assert err(ei).details[0]["reason"] == "override_duplicate"
        # a bare string would otherwise iterate into characters and waive nothing at all
        with pytest.raises(HaddonError) as ei:
            close(ctx, "2026-03-31", version=1, overrides="statement_coverage")
        assert err(ei).details[0]["reason"] == "list_required"
    assert fx.scalar("SELECT count(*) FROM close_records") == 0


def test_the_close_audit_action_carries_the_checks_the_overrides_and_the_hash(fx):
    import json

    seed_activity(fx)
    with fx.tx(HUMAN) as ctx:
        closed = close(ctx, "2026-03-31", reason="Q1 is done", version=1)
    assert closed.audit.kind == "period.closed" and closed.audit.subject_id == closed.close_id
    assert closed.audit.reason == "Q1 is done" and closed.audit.actor == HUMAN
    result = closed.audit.result
    assert result["state_hash"] == closed.snapshot.state_hash and result["snapshot_id"] == closed.snapshot.id
    assert result["eligible"] is False and result["lock_version"] == 2
    assert set(result["checks"]) == {c.key for c in cc.policy()}
    assert [o["check"] for o in result["overrides"]] == sorted(o["check"] for o in result["overrides"])
    # and the full evaluation is on its own event beside it, which is where the outstanding items live
    after = json.loads(fx.scalar("SELECT after FROM events WHERE kind='close.eligibility' AND subject_id=?",
                                 (closed.close_id,)))
    assert after["through"] == "2026-03-31" and "outstanding_counts" in after and "waived_checks" in after


# reopen
def test_reopen_moves_the_lock_back_and_keeps_the_close_it_reopened(fx):
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    closes_before = fx.scalar("SELECT count(*) FROM close_records WHERE kind='close'")
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            commands.reopen_period(ctx, through="2026-02-28", reason="  ", expected_version=2)
        assert err(ei).code == "validation" and err(ei).details[0]["path"] == "reason"
        with pytest.raises(HaddonError) as ei:
            commands.reopen_period(ctx, through="2026-02-28", reason="late invoice", expected_version=1)
        assert err(ei).code == "stale_version"
        reopened = commands.reopen_period(ctx, through="2026-02-28", reason="late invoice", expected_version=2)
    assert (reopened.lock.through, reopened.lock.version) == ("2026-02-28", 3)
    assert reopened.audit.kind == "period.reopened" and reopened.audit.reason == "late invoice"
    # the close it reopened is untouched: append-only means the history gains a row, it does not lose one
    assert fx.scalar("SELECT count(*) FROM close_records WHERE kind='close'") == closes_before
    assert fx.q("SELECT kind, through, prior_through, reason FROM close_records ORDER BY rowid") == [
        ("close", "2026-03-31", None, "month close"), ("reopen", "2026-02-28", "2026-03-31", "late invoice")]
    assert fx.q("SELECT overrides FROM close_records WHERE kind='reopen'") == [("[]",)]
    # March is open again: an entry dated in March posts
    assert post(fx, "2026-03-15", "10000").status == "posted"


def test_reopen_takes_a_close_to_undo_as_well_as_a_boundary(fx):
    """"Open March again" is a boundary and "undo that close" is an action; they are the same reopen, and the
    close resolves to the boundary it advanced from, which is the only reading that puts the lock back."""
    seed_activity(fx)
    lock_books(fx, "2026-02-28")
    lock_books(fx, "2026-03-31")
    march = fx.scalar("SELECT id FROM close_records WHERE through='2026-03-31' AND kind='close'")
    with fx.tx(HUMAN) as ctx:
        reopened = commands.reopen_period(ctx, close_id=march, reason="March was closed early",
                                          expected_version=3)
    assert reopened.lock.through == "2026-02-28" and reopened.reopened_close_id == march
    assert reopened.audit.result["reopened_close_id"] == march


def test_reopen_refuses_to_name_neither_a_boundary_nor_a_close_or_both(fx):
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    with fx.tx(HUMAN) as ctx:
        for kwargs in ({}, {"through": "2026-02-28", "close_id": "cr-anything"}):
            with pytest.raises(HaddonError) as ei:
                commands.reopen_period(ctx, reason="x", expected_version=2, **kwargs)
            assert err(ei).details[0]["reason"] == "target_required"


def test_undoing_the_first_close_of_a_journal_is_refused_rather_than_clearing_the_lock(fx):
    """U7 again, reached the other way: the first close has no prior boundary, so "undo it" would mean "no lock",
    which the lock may never return to.  It says so instead of quietly reopening to some other date."""
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    first = fx.scalar("SELECT id FROM close_records WHERE prior_through IS NULL")
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            commands.reopen_period(ctx, close_id=first, reason="undo it", expected_version=2)
        assert err(ei).details[0]["reason"] == "would_clear_the_lock"
    assert fx.q("SELECT through FROM period_locks") == [("2026-03-31",)]


def test_reopen_refuses_an_unknown_close_and_a_reopen_record(fx):
    seed_activity(fx)
    lock_books(fx, "2026-02-28")
    lock_books(fx, "2026-03-31")
    with fx.tx(HUMAN) as ctx:
        commands.reopen_period(ctx, through="2026-02-28", reason="fix March", expected_version=3)
    reopen_id = fx.scalar("SELECT id FROM close_records WHERE kind='reopen'")
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            commands.reopen_period(ctx, close_id="ghost", reason="x", expected_version=4)
        assert err(ei).status == 404
        with pytest.raises(HaddonError) as ei:
            commands.reopen_period(ctx, close_id=reopen_id, reason="x", expected_version=4)
        assert err(ei).details[0]["reason"] == "not_a_close"


def test_reopen_can_never_clear_the_lock(fx):
    """U7: the first close is permanent history; reopen can only move the lock earlier, never to NULL."""
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            commands.reopen_period(ctx, through=None, close_id=None, reason="clear", expected_version=2)
        assert err(ei).code == "validation"
        for through in ("2026-03-31", "2026-04-30"):  # not moving back
            with pytest.raises(HaddonError) as ei:
                commands.reopen_period(ctx, through=through, reason="x", expected_version=2)
            assert err(ei).details[0]["reason"] == "not_reopening"
    assert fx.q("SELECT through FROM period_locks") == [("2026-03-31",)]
    # and the database itself refuses a NULL: the locks_update trigger requires a matching close record
    con = fx.raw()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE period_locks SET through=NULL, version=3 WHERE id='lock'")
    con.close()


def test_reopen_when_nothing_is_closed_is_refused(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            commands.reopen_period(ctx, through="2026-02-28", reason="x", expected_version=1)
    assert err(ei).code == "immutable"


# the mechanics underneath
def test_a_snapshot_that_is_not_through_the_close_date_is_now_unrepresentable(fx):
    """There is no "snapshot must be taken through the close date" guard, because nothing can violate it.

    ``close`` computes the snapshot from the very date it is closing through, so no caller, public or internal,
    can hand in one taken through another date.  This test pins that instead of a guard against a state that
    cannot be constructed.
    """
    import inspect

    assert "snapshot_id" not in inspect.signature(cl.close).parameters
    assert "snapshot_id" not in inspect.signature(cl.reopen).parameters
    seed_activity(fx)
    with fx.tx(HUMAN) as ctx:
        closed = close(ctx, "2026-03-31", version=1)
    assert fx.scalar("SELECT through FROM snapshots WHERE id=?", (closed.snapshot.id,)) == "2026-03-31"


def test_snapshot_validation(fx):
    with fx.tx(HUMAN) as ctx:
        with pytest.raises(HaddonError) as ei:
            cl.create_snapshot(ctx, through="2026-03-31", state_hash="", manifest={}, report={}, checks={})
        assert err(ei).details[0]["path"] == "state_hash"
        s = cl.create_snapshot(ctx, through="2026-03-31", state_hash="sh-1", manifest={"m": 1}, report={"r": []},
                               checks={"ok": True})
    with fx.reader() as conn:
        assert cl.get_snapshot(conn, fx.entity_id, s.id) == s and cl.get_snapshot(conn, fx.entity_id, "ghost") is None
    # state_hash is UNIQUE and not pre-validated: the DB refuses the duplicate (422 via the fallback map), the
    # transaction is tainted and rolls back as a whole (a caller cannot swallow this one and go on)
    with pytest.raises(HaddonError) as ei:
        with fx.tx(HUMAN) as ctx:
            cl.create_snapshot(ctx, through="2026-04-30", state_hash=s.state_hash, manifest={}, report={}, checks={})
    assert (err(ei).code, err(ei).status) == ("validation", 422)
    assert fx.scalar("SELECT count(*) FROM snapshots") == 1


def test_locked_period_refuses_posting_after_close(fx):
    """End-to-end control that a close actually locks: a posting dated inside the closed period is refused."""
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    with fx.tx() as ctx:
        from haddon.repository import entries as en
        with pytest.raises(HaddonError) as ei:
            en.post_entry(ctx, dt="2026-03-31", narration="n", origin="manual", legs=pair(fx, "10000"))
        assert err(ei).code == "period_locked"
        assert en.post_entry(ctx, dt="2026-04-01", narration="n", origin="manual", legs=pair(fx, "10000")).status == "posted"
