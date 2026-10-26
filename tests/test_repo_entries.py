"""Entries: the fixed-order validation in ``entries.py``'s docstring is the spec.  One test per ordering claim, plus
the seal/void contract.  Every refusal test has a positive control: the same shape succeeds once the defect is
removed, so a test cannot pass because the setup was already broken.

draft creation   1 shape (422 validation)  2 overflow (422)  3 commodity known (422 validation)
                 4 account known (422 account_unknown)  5 commodity allowed on account (422 validation)
                 6 quantum (422 precision)  7 period lock (409 period_locked)
seal             1 exists (404)  2 still draft (409 immutable)  3 version (412 stale_version)
                 4 period lock (409 period_locked)  5 two legs + balanced per commodity (422 unbalanced)
                 6 accounts known / not archived (422 account_unknown / account_archived, U8)
void             1 exists  2 still draft  3 version  (a locked draft may be voided: D4)
"""

from __future__ import annotations

import pytest

from haddon.domain.errors import HaddonError
from haddon.domain.micros import MAX_MICROS, Micros
from haddon.repository import entries as en
from haddon.repository import provisioning as pv
from haddon.repository.entries import Leg

from repo_helpers import AGENT, HUMAN, lock_books, m, pair, post

DT = "2026-03-15"
BAD_Q = "12345"  # not a multiple of the USD quantum 10000


def err(ei) -> HaddonError:
    assert isinstance(ei.value, HaddonError)
    return ei.value


def draft(fx, legs, dt=DT, origin="manual", **kw):
    with fx.tx() as ctx:
        return en.create_draft(ctx, dt=dt, narration="n", origin=origin, legs=legs, **kw)


def refused(fx, legs, dt=DT, origin="manual", **kw) -> HaddonError:
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.create_draft(ctx, dt=dt, narration="n", origin=origin, legs=legs, **kw)
    return err(ei)


def seed_activity(fx):
    """One posted entry, so the journal is non-empty: a continuous journal's first close needs something to close."""
    return post(fx, "2026-01-15", "10000")


# happy paths
def test_post_entry_happy_path(fx):
    e = post(fx, DT, "1250000", memo="lunch", tax_code="VAT0")
    assert e.status == "posted" and e.version == 2 and e.origin == "manual"
    assert [(p.seq, p.account_id, p.commodity, p.amount.render(), p.memo, p.tax_code) for p in e.legs] == [
        (1, fx.acct("expenses:office"), "USD", "1250000", "lunch", "VAT0"),
        (2, fx.acct("assets:cash"), "USD", "-1250000", "lunch", "VAT0")]
    assert fx.q("SELECT status, version FROM entries WHERE id=?", (e.id,)) == [("posted", 2)]
    assert fx.q("SELECT seq, amount_micros, typeof(amount_micros) FROM postings WHERE entry_id=? ORDER BY seq", (e.id,)) == [
        (1, 1250000, "integer"), (2, -1250000, "integer")]
    with fx.reader() as conn:
        assert en.get_entry(conn, fx.entity_id, e.id) == e
        assert en.get_entry(conn, fx.entity_id, "nope") is None


def test_multi_commodity_entry_balanced_per_commodity_seals(fx):
    """U5: legs net to zero per commodity."""
    legs = pair(fx, "1000000") + pair(fx, "2000000", commodity="EUR", credit="assets:cash-eur")
    with fx.tx() as ctx:
        e = en.post_entry(ctx, dt=DT, narration="n", origin="manual", legs=legs)
    assert e.status == "posted" and len(e.legs) == 4


def test_draft_then_seal_with_explicit_ids(fx):
    with fx.tx() as ctx:
        d = en.create_draft(ctx, dt=DT, narration="n", origin="agent", legs=pair(fx, "10000"), id="e-custom")
        assert d.id == "e-custom" and d.status == "draft" and d.version == 1
        assert [p.id for p in d.legs] == ["e-custom.p1", "e-custom.p2"]
        s = en.seal(ctx, "e-custom", expected_version=1)
    assert s.status == "posted" and s.version == 2


def test_drafts_are_real_rows_and_can_be_voided_later(fx):
    d = draft(fx, pair(fx, "10000"))
    assert fx.q("SELECT status FROM entries WHERE id=?", (d.id,)) == [("draft",)]
    with fx.tx() as ctx:
        v = en.void(ctx, d.id, expected_version=1)
    assert v.status == "void" and v.version == 2


# header
def test_header_validation(fx):
    for dt in ("2026-3-15", "2026-02-30", "20260315", None, "2026-03-15T00:00"):
        e = refused(fx, pair(fx, "10000"), dt=dt)
        assert e.code == "validation" and e.details[0]["path"] == "dt", dt
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.create_draft(ctx, dt=DT, narration=" ", origin="manual", legs=pair(fx, "10000"))
        assert err(ei).details[0]["path"] == "narration"
    e = refused(fx, pair(fx, "10000"), origin="bogus")
    assert e.code == "validation" and e.details[0]["reason"] == "enum"


@pytest.mark.parametrize("origin", ["reversal", "opening"])
def test_caller_supplied_reserved_origin_is_refused(fx, origin):
    e = refused(fx, pair(fx, "10000"), origin=origin)
    assert e.code == "validation" and e.details[0]["reason"] == "reserved_origin"
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.post_entry(ctx, dt=DT, narration="n", origin=origin, legs=pair(fx, "10000"))
        assert err(ei).details[0]["reason"] == "reserved_origin"
        # the service itself may use it
        e2 = en.create_draft(ctx, dt=DT, narration="n", origin=origin, legs=pair(fx, "10000"), _allow_reserved_origin=True)
        assert e2.origin == origin


# draft phases 1..7
def test_phase1_shape_before_everything(fx):
    """Shape (422 validation) beats overflow, unknown commodity, unknown account, quantum and the lock."""
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    legs = [Leg("ghost", "GBP", m(str(MAX_MICROS))), Leg(fx.acct("assets:cash"), "USD", 12)]  # int amount is a shape error
    e = refused(fx, legs)
    assert e.code == "validation" and e.details == [{"path": "legs[1].amount", "reason": "string_required", "type": "int"}]


def test_phase1_amount_string_shape_before_overflow(fx):
    legs = [Leg(fx.acct("assets:cash"), "USD", "12.5"), Leg(fx.acct("expenses:office"), "USD", str(MAX_MICROS + 1))]
    e = refused(fx, legs)
    assert e.code == "validation" and e.details[0]["reason"] == "not_integer_string" and e.details[0]["path"] == "legs[0].amount"


def test_phase2_overflow_before_unknown_commodity(fx):
    legs = [Leg(fx.acct("assets:cash"), "GBP", str(MAX_MICROS + 1)), Leg(fx.acct("expenses:office"), "USD", "10000")]
    e = refused(fx, legs)
    assert (e.code, e.status) == ("overflow", 422) and e.details[0]["path"] == "legs[0].amount"
    e = refused(fx, [Leg(fx.acct("assets:cash"), "USD", str(-2**63)), Leg(fx.acct("expenses:office"), "USD", "10000")])
    assert e.code == "overflow"  # U6: -2^63 is excluded


def test_phase3_unknown_commodity_before_unknown_account(fx):
    e = refused(fx, [Leg("ghost", "GBP", "10000"), Leg(fx.acct("expenses:office"), "USD", "-10000")])
    assert e.code == "validation" and e.details == [{"path": "legs[0].commodity", "reason": "commodity_unknown", "value": "GBP"}]
    # positive control: fix the commodity and the account error appears instead
    e = refused(fx, [Leg("ghost", "USD", "10000"), Leg(fx.acct("expenses:office"), "USD", "-10000")])
    assert (e.code, e.status) == ("account_unknown", 422) and e.details == [{"path": "legs[0].account_id", "id": "ghost"}]


def test_phase4_unknown_account_before_not_allowed_and_quantum(fx):
    legs = [Leg("ghost", "USD", BAD_Q), Leg(fx.acct("assets:cash"), "EUR", "-10000")]  # EUR not allowed on cash
    e = refused(fx, legs)
    assert e.code == "account_unknown" and e.details[0]["id"] == "ghost"


def test_phase5_commodity_not_allowed_before_quantum(fx):
    legs = [Leg(fx.acct("assets:cash"), "EUR", BAD_Q), Leg(fx.acct("expenses:office"), "EUR", "-10000")]
    e = refused(fx, legs)
    assert e.code == "validation" and e.details[0]["reason"] == "commodity_not_allowed"
    assert e.details[0]["account_id"] == fx.acct("assets:cash") and e.details[0]["commodity"] == "EUR"
    # positive control: same amounts on an account that allows EUR -> the quantum error appears
    e = refused(fx, [Leg(fx.acct("assets:cash-eur"), "EUR", BAD_Q), Leg(fx.acct("expenses:office"), "EUR", "-10000")])
    assert e.code == "precision"


def test_phase6_quantum_is_422_precision_with_details(fx):
    e = refused(fx, [Leg(fx.acct("assets:cash"), "USD", BAD_Q), Leg(fx.acct("expenses:office"), "USD", "-" + BAD_Q)])
    assert (e.code, e.status) == ("precision", 422)
    assert [d["path"] for d in e.details] == ["legs[0].amount", "legs[1].amount"]
    assert e.details[0]["quantum"] == 10000 and e.details[0]["value"] == BAD_Q
    # JPY has quantum 1_000_000: 1 yen is fine, 0.5 yen is not
    yen = [Leg(fx.acct("assets:cash-jpy"), "JPY", "1000000"), Leg(fx.acct("assets:cash-jpy"), "JPY", "-1000000")]
    assert draft(fx, yen).status == "draft"
    e = refused(fx, [Leg(fx.acct("assets:cash-jpy"), "JPY", "500000"), Leg(fx.acct("assets:cash-jpy"), "JPY", "-500000")])
    assert e.code == "precision"


def test_phase6_quantum_before_phase7_lock(fx):
    """A draft that is both quantum-bad and in a locked period returns `precision` (content before state, 422 before 409)."""
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    e = refused(fx, [Leg(fx.acct("assets:cash"), "USD", BAD_Q), Leg(fx.acct("expenses:office"), "USD", "-" + BAD_Q)])
    assert (e.code, e.status) == ("precision", 422)
    # positive control for the lock: a well-formed draft in the same period is period_locked ...
    e = refused(fx, pair(fx, "10000"))
    assert (e.code, e.status) == ("period_locked", 409) and e.details[0]["locked_through"] == "2026-03-31"
    # ... and the same draft one day after the lock is accepted
    assert draft(fx, pair(fx, "10000"), dt="2026-04-01").status == "draft"


def test_phase7_lock_boundary_is_inclusive(fx):
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    assert refused(fx, pair(fx, "10000"), dt="2026-03-31").code == "period_locked"
    assert draft(fx, pair(fx, "10000"), dt="2026-04-01").status == "draft"


def test_leg_count_bounds(fx):
    e = refused(fx, [])
    assert e.code == "validation" and e.details[0]["reason"] == "leg_count"
    one = draft(fx, [Leg(fx.acct("assets:cash"), "USD", "10000")])  # one leg is a valid DRAFT
    assert len(one.legs) == 1
    legs = [Leg(fx.acct("assets:cash"), "USD", "10000")] * (en.MAX_LEGS + 1)
    assert refused(fx, legs).details[0]["count"] == en.MAX_LEGS + 1


def test_malformed_leg_fields_are_shape_errors(fx):
    e = refused(fx, [Leg("", "USD", "10000"), Leg(fx.acct("assets:cash"), "", "-10000", memo=3, tax_code=4)])
    assert e.code == "validation"
    assert {d["path"] for d in e.details} == {"legs[0].account_id", "legs[1].commodity", "legs[1].memo", "legs[1].tax_code"}
    e = refused(fx, [("not", "a", "leg")])
    assert e.details[0]["reason"] == "leg_shape"


# seal ordering
def test_seal_unknown_entry_is_404(fx):
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, "ghost", expected_version=1)
    assert (err(ei).code, err(ei).status) == ("not_found", 404)


def test_seal_posted_entry_is_409_immutable_before_version(fx):
    e = post(fx, DT, "10000")
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, e.id, expected_version=99)  # wrong version too: still `immutable` first
        assert (err(ei).code, err(ei).status) == ("immutable", 409)
        with pytest.raises(HaddonError) as ei:
            en.void(ctx, e.id, expected_version=2)
        assert err(ei).code == "immutable"
    v = draft(fx, pair(fx, "10000"))
    with fx.tx() as ctx:
        en.void(ctx, v.id, expected_version=1)
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, v.id, expected_version=2)
        assert err(ei).code == "immutable" and err(ei).details[0]["status"] == "void"


def test_seal_wrong_expected_version_is_412_before_lock_and_balance(fx):
    d = draft(fx, [Leg(fx.acct("assets:cash"), "USD", "10000")])  # unbalanced, one leg
    seed_activity(fx)
    lock_books(fx, "2026-03-31")  # and now locked
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, d.id, expected_version=7)
        e = err(ei)
        assert (e.code, e.status) == ("stale_version", 412)
        assert e.details[0] == {"id": d.id, "current_version": 1, "expected_version": 7}
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, d.id, expected_version="1")
        assert err(ei).code == "validation"


def test_seal_locked_period_is_409_before_unbalanced(fx):
    d = draft(fx, [Leg(fx.acct("assets:cash"), "USD", "10000")])  # would be `unbalanced` if sealed
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, d.id, expected_version=1)
    e = err(ei)
    assert (e.code, e.status) == ("period_locked", 409) and e.details[0]["locked_through"] == "2026-03-31"
    assert fx.q("SELECT status FROM entries WHERE id=?", (d.id,)) == [("draft",)]


def test_seal_one_leg_is_unbalanced(fx):
    d = draft(fx, [Leg(fx.acct("assets:cash"), "USD", "10000")])
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, d.id, expected_version=1)
    e = err(ei)
    assert (e.code, e.status) == ("unbalanced", 422) and e.details[0]["reason"] == "two_legs_required"


def test_seal_unbalanced_reports_offending_sums_per_commodity(fx):
    legs = [Leg(fx.acct("assets:cash"), "USD", "10000"), Leg(fx.acct("expenses:office"), "USD", "-30000"),
            Leg(fx.acct("assets:cash-eur"), "EUR", "50000"), Leg(fx.acct("expenses:office"), "EUR", "-50000")]
    d = draft(fx, legs)
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, d.id, expected_version=1)
    e = err(ei)
    assert (e.code, e.status) == ("unbalanced", 422)
    assert e.details == [{"commodity": "USD", "sum": "-20000"}]  # EUR balances and is not reported


def test_seal_balanced_usd_unbalanced_eur_is_unbalanced(fx):
    legs = pair(fx, "10000") + [Leg(fx.acct("assets:cash-eur"), "EUR", "70000"), Leg(fx.acct("expenses:office"), "EUR", "-60000")]
    d = draft(fx, legs)
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, d.id, expected_version=1)
    assert err(ei).code == "unbalanced" and err(ei).details == [{"commodity": "EUR", "sum": "10000"}]
    assert fx.q("SELECT status FROM entries WHERE id=?", (d.id,)) == [("draft",)]


def test_seal_archived_account_is_422_account_archived_after_balance(fx):
    """U8: an archived account is accepted on a draft leg and refused at seal (balance is checked first)."""
    sales = fx.acct("income:sales")
    d = draft(fx, pair(fx, "10000", debit="income:sales"))
    with fx.tx(HUMAN) as ctx:
        pv.archive_account(ctx, account_id=sales, expected_version=1)
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, d.id, expected_version=1)
    e = err(ei)
    assert (e.code, e.status) == ("account_archived", 422) and e.details == [{"path": "legs[0].account_id", "id": sales}]
    # new drafts on the archived account are still accepted (caught at seal), and an unbalanced draft on it says unbalanced first
    d2 = draft(fx, [Leg(sales, "USD", "10000"), Leg(fx.acct("assets:cash"), "USD", "-20000")])
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.seal(ctx, d2.id, expected_version=1)
    assert err(ei).code == "unbalanced"


# void
def test_void_draft_inside_locked_period_succeeds(fx):
    """Decision D4: a draft is not accounting; voiding it under a lock is allowed."""
    d = draft(fx, pair(fx, "10000"))
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:  # control: the same draft cannot be sealed under the lock
            en.seal(ctx, d.id, expected_version=1)
        assert err(ei).code == "period_locked"
        v = en.void(ctx, d.id, expected_version=1)
    assert v.status == "void" and v.version == 2
    assert fx.q("SELECT status, version FROM entries WHERE id=?", (d.id,)) == [("void", 2)]


def test_void_ordering_exists_draft_version(fx):
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.void(ctx, "ghost", expected_version=1)
        assert err(ei).status == 404
    d = draft(fx, pair(fx, "10000"))
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.void(ctx, d.id, expected_version=2)
        assert err(ei).code == "stale_version"
        en.void(ctx, d.id, expected_version=1)
        with pytest.raises(HaddonError) as ei:
            en.void(ctx, d.id, expected_version=2)
        assert err(ei).code == "immutable"


def test_lock_helpers(fx):
    with fx.reader() as conn:
        assert en.lock_through(conn) is None and not en.is_locked(conn, "2000-01-01")
    seed_activity(fx)
    lock_books(fx, "2026-03-31")
    with fx.reader() as conn:
        assert en.lock_through(conn) == "2026-03-31"
        assert en.is_locked(conn, "2026-03-31") and not en.is_locked(conn, "2026-04-01")


def test_leg_multiset_is_canonical(fx):
    e = post(fx, DT, "10000", memo="a")
    ms = en.leg_multiset(e)
    assert ms[(fx.acct("expenses:office"), "USD", 10000, "a", None)] == 1
    assert ms[(fx.acct("assets:cash"), "USD", -10000, "a", None)] == 1
    assert sum(ms.values()) == 2


def test_explicit_duplicate_entry_id_is_a_clean_409_not_a_tainted_transaction(fx):
    draft(fx, pair(fx, "10000"), id="e-dup")
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            en.create_draft(ctx, dt=DT, narration="n", origin="manual", legs=pair(fx, "10000"), id="e-dup")
        assert (err(ei).code, err(ei).status) == ("immutable", 409) and err(ei).details[0]["table"] == "entries"
        ok = en.create_draft(ctx, dt=DT, narration="n", origin="manual", legs=pair(fx, "10000"), id="e-new")  # tx still usable
    assert ok.id == "e-new"
    assert fx.q("SELECT id, count(*) FROM entries GROUP BY id ORDER BY id") == [("e-dup", 1), ("e-new", 1)]
    assert fx.scalar("SELECT count(*) FROM postings WHERE entry_id='e-dup'") == 2
