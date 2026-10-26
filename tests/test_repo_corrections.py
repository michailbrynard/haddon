"""Reversals: mirror legs are exact negatives, one mirror per original, only posted originals, a required
reason, replacement sealed together with the mirror, and a failure after the mirror sealed leaves nothing.

Nothing is consulted before a reversal runs (issue 0005); the record of authority is the audit action the
enclosing command writes.  See ``test_a_reversal_needs_no_authority_but_still_needs_a_reason``."""

from __future__ import annotations

import pytest

from haddon.domain.errors import HaddonError
from haddon.repository import corrections as co
from haddon.repository import entries as en
from haddon.repository.entries import Leg

from repo_helpers import AGENT, HUMAN, lock_books, m, pair, post

DT = "2026-03-15"
RDT = "2026-03-20"
#: `reverse_entry` requires a stated reason (chunk G2).  These tests are about the mechanics of the reversal, so
#: they all give the same one.
WHY = "the account it was booked to is wrong"


def err(ei) -> HaddonError:
    assert isinstance(ei.value, HaddonError)
    return ei.value


class InjectedFault(Exception):
    pass


def original(fx):
    with fx.tx() as ctx:
        legs = [Leg(fx.acct("expenses:office"), "USD", m("1250000"), "lunch", "VAT21"),
                Leg(fx.acct("expenses:office"), "EUR", m("990000"), "coffee", None),
                Leg(fx.acct("assets:cash"), "USD", m("-1250000"), "card", "VAT21"),
                Leg(fx.acct("assets:cash-eur"), "EUR", m("-990000"), None, None)]
        return en.post_entry(ctx, dt=DT, narration="original", origin="manual", legs=legs)


def test_reversal_produces_exact_mirror_legs(fx):
    o = original(fx)
    before = fx.counts()
    with fx.tx() as ctx:
        c = co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT)
    assert c.original_id == o.id and c.replacement_id is None
    with fx.reader() as conn:
        mirror = en.get_entry(conn, fx.entity_id, c.mirror_id)
        assert co.correction_for(conn, fx.entity_id, o.id) == c
    assert mirror.status == "posted" and mirror.origin == "reversal" and mirror.dt == RDT
    assert mirror.narration == f"Reversal of {o.id}: original"
    assert len(mirror.legs) == len(o.legs)
    for op, mp in zip(o.legs, mirror.legs):
        assert (mp.account_id, mp.commodity, mp.memo, mp.tax_code) == (op.account_id, op.commodity, op.memo, op.tax_code)
        assert mp.amount == -op.amount and mp.amount.value == -op.amount.value
    assert en.leg_multiset(mirror) == co.negated_multiset(o)
    after = fx.counts()
    assert after["entries"] == before["entries"] + 1 and after["postings"] == before["postings"] + 4 and after["corrections"] == 1
    assert fx.q("SELECT typeof(amount_micros) FROM postings WHERE entry_id=? GROUP BY 1", (c.mirror_id,)) == [("integer",)]


def test_second_reversal_of_same_original_is_refused(fx):
    o = original(fx)
    with fx.tx() as ctx:
        c = co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT)
    before = fx.counts()
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT)
    e = err(ei)
    assert (e.code, e.status) == ("immutable", 409) and e.details[0]["mirror_id"] == c.mirror_id
    assert fx.counts() == before
    # the mirror itself cannot be reversed either through the same original (spec: correct the replacement)
    with fx.tx() as ctx:
        c2 = co.reverse_entry(ctx, reason=WHY, original_id=c.mirror_id, dt=RDT)  # reversing the mirror is a new original
    assert c2.original_id == c.mirror_id


@pytest.mark.parametrize("status", ["draft", "void"])
def test_reversing_a_draft_or_void_is_refused(fx, status):
    with fx.tx() as ctx:
        e = en.create_draft(ctx, dt=DT, narration="n", origin="manual", legs=pair(fx, "10000"))
        if status == "void":
            e = en.void(ctx, e.id, expected_version=1)
    before = fx.counts()
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            co.reverse_entry(ctx, reason=WHY, original_id=e.id, dt=RDT)
    e_ = err(ei)
    assert (e_.code, e_.status) == ("validation", 422) and e_.details[0]["status"] == status
    assert fx.counts() == before
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            co.reverse_entry(ctx, reason=WHY, original_id="ghost", dt=RDT)
    assert err(ei).status == 404


def test_a_reversal_needs_no_authority_but_still_needs_a_reason(fx):
    """No decision row is needed before a reversal runs (issue 0005), but a reason still is.

    The record of authority is the audit action the enclosing command writes (``docs/architecture.md``, "Three
    kinds of fact").  The reason is the one thing that cannot be reconstructed from the numbers a year later, so
    a blank one is a 422 that writes nothing.
    """
    o = original(fx)
    before = fx.counts()
    with fx.tx() as ctx:
        for blank in ("", "   "):
            with pytest.raises(HaddonError) as ei:
                co.reverse_entry(ctx, reason=blank, original_id=o.id, dt=RDT)
            assert err(ei).status == 422
    assert fx.counts() == before
    with fx.tx() as ctx:
        c = co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT)
    assert c.original_id == o.id
    assert not hasattr(c, "decision_id")


def test_reversal_with_replacement_seals_both(fx):
    o = original(fx)
    repl = co.Replacement(dt=RDT, narration="corrected", origin="adjustment",
                          legs=[Leg(fx.acct("expenses:office"), "USD", m("1200000"), "lunch", "VAT21"),
                                Leg(fx.acct("assets:cash"), "USD", m("-1200000"), "card", "VAT21")])
    with fx.tx() as ctx:
        c = co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT, replacement=repl, mirror_id="mir", replacement_id="rep", id="cor")
    assert (c.id, c.mirror_id, c.replacement_id) == ("cor", "mir", "rep")
    assert fx.q("SELECT id, status, origin, version FROM entries WHERE id IN ('mir','rep') ORDER BY id") == [
        ("mir", "posted", "reversal", 2), ("rep", "posted", "adjustment", 2)]
    assert fx.q("SELECT original_id, mirror_id, replacement_id FROM corrections") == [(o.id, "mir", "rep")]


def test_replacement_shape_is_checked(fx):
    o = original(fx)
    before = fx.counts()
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT, replacement={"dt": RDT})
        assert err(ei).code == "validation" and err(ei).details[0]["path"] == "replacement"
        # a replacement with a malformed body (unknown account / bad quantum / reserved origin) is refused up front too
        bad_leg = co.Replacement(dt=RDT, narration="x", origin="adjustment", legs=[Leg("ghost", "USD", m("10000")), Leg(fx.acct("assets:cash"), "USD", m("-10000"))])
        with pytest.raises(HaddonError) as ei:
            co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT, replacement=bad_leg)
        assert err(ei).code == "account_unknown"
        reserved = co.Replacement(dt=RDT, narration="x", origin="reversal", legs=pair(fx, "10000"))
        with pytest.raises(HaddonError) as ei:
            co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT, replacement=reserved)
        assert err(ei).details[0]["reason"] == "reserved_origin"
        # the errors were handled inside the transaction: had any draft been written it would now be committed
    assert fx.counts() == before, "a refused replacement left a mirror draft behind"


def test_unbalanced_replacement_fails_after_mirror_sealed_and_leaves_nothing(fx):
    """The natural post-seal failure: the mirror seals, then the replacement's seal raises `unbalanced`."""
    o = original(fx)
    before = fx.counts()
    bad = co.Replacement(dt=RDT, narration="bad", origin="adjustment",
                         legs=[Leg(fx.acct("expenses:office"), "USD", m("10000")), Leg(fx.acct("assets:cash"), "USD", m("-20000"))])
    with pytest.raises(HaddonError) as ei:
        with fx.tx() as ctx:
            co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT, replacement=bad, mirror_id="mir-gone")
    assert err(ei).code == "unbalanced"
    assert fx.counts() == before
    assert fx.scalar("SELECT count(*) FROM entries WHERE id='mir-gone' OR origin='reversal'") == 0


def test_injected_fault_after_mirror_sealed_leaves_zero_rows(fx, monkeypatch):
    """Inject a failure after the mirror sealed (in the multiset gate) and prove the whole reversal rolled back."""
    o = original(fx)
    before = fx.counts()
    sealed_ids: list[str] = []
    real_seal = co.seal

    def seal_then_fail(ctx, entry_id, *, expected_version):
        e = real_seal(ctx, entry_id, expected_version=expected_version)
        sealed_ids.append(e.id)
        # the mirror really is posted at this point, inside the open transaction
        assert ctx.conn.execute(__import__("sqlalchemy").text("SELECT status FROM entries WHERE id=:i"), {"i": e.id}).scalar() == "posted"
        raise InjectedFault("after seal")

    monkeypatch.setattr(co, "seal", seal_then_fail)
    with pytest.raises(InjectedFault):
        with fx.tx() as ctx:
            co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT, mirror_id="mir-fault")
    assert sealed_ids == ["mir-fault"], "the fault must have fired after a real seal"
    assert fx.counts() == before
    assert fx.q("SELECT count(*) FROM entries WHERE id='mir-fault'") == [(0,)]
    assert fx.q("SELECT count(*) FROM postings WHERE entry_id='mir-fault'") == [(0,)]
    assert fx.q("SELECT count(*) FROM corrections") == [(0,)]
    # the engine is reusable and the same reversal now succeeds (positive control)
    monkeypatch.undo()
    with fx.tx() as ctx:
        assert co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT).original_id == o.id


def test_gate_failure_after_seal_is_internal_error_and_rolls_back(fx, monkeypatch):
    """If the mirror multiset ever disagreed with the negated original, the repository gate must refuse."""
    o = original(fx)
    before = fx.counts()
    monkeypatch.setattr(co, "negated_multiset", lambda e: {"tampered": 1})
    with pytest.raises(HaddonError) as ei:
        with fx.tx() as ctx:
            co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT)
    assert (err(ei).code, err(ei).status) == ("internal_error", 500)
    assert fx.counts() == before


def test_mirror_in_locked_period_is_refused_before_any_write(fx):
    o = original(fx)
    lock_books(fx, "2026-03-31")
    before = fx.counts()
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt=RDT)  # RDT is inside the lock
        assert err(ei).code == "period_locked"
        c = co.reverse_entry(ctx, reason=WHY, original_id=o.id, dt="2026-04-01")  # positive control: after the lock
    assert c.original_id == o.id
    assert fx.counts()["entries"] == before["entries"] + 1


def test_mirror_legs_helper_negates_every_leg(fx):
    o = original(fx)
    legs = co.mirror_legs(o)
    assert [(l.account_id, l.commodity, l.amount.value, l.memo, l.tax_code) for l in legs] == [
        (p.account_id, p.commodity, -p.amount.value, p.memo, p.tax_code) for p in o.legs]
