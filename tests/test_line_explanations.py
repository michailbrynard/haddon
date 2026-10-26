"""Explaining a statement line: book it, link it, split it, ignore it, and change your mind (issue 0004).

What each block defends:

* One line, one current status, and the whole history.  A line's status is derived from the explanation
  nothing supersedes, so there is exactly one answer and the chain that produced it is readable.
* An allocation moves no money.  Linking and re-linking must leave every balance in the books identical; the
  trial balance is compared before and after.
* Bounds.  Neither a line nor a posting can be explained for more than it is, across one explanation or
  several, and the refusal names the arithmetic.
* A locked period fails closed, on the line's date and on the date of the entry a part points at.
* Correcting is reversing.  Re-booking a line corrects the entry it booked; re-linking or ignoring it reverses
  that entry.  Nothing is silently rewritten.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from haddon import commands
from haddon.domain.errors import HaddonError
from haddon.repository import corrections as corrections_repo
from haddon.repository import entries as en
from haddon.repository import lines as ln
from haddon.repository import tables as t

from repo_helpers import AGENT, HUMAN, post
from statement_helpers import (
    CASH,
    EUR,
    OFFICE,
    SALES,
    balances,
    book,
    lines_of,
    load,
    lock_books,
    refused,
    row,
    state,
    statement,
)


def only_line(fx, **kw) -> str:
    return statement(fx, **kw)[0].id


def cash_posting(fx, entry) -> en.Posting:
    return next(p for p in entry.legs if p.account_id == fx.acct(CASH))


def posted_against(fx, dt: str = "2026-03-05", amount: str = "3000000"):
    """One ordinary entry that moves the cash account, written without any statement in sight."""
    return post(fx, dt, amount, debit=OFFICE, credit=CASH)


# booking a line
def test_booking_a_line_posts_the_entry_it_is_and_holds_the_line_against_it(fx):
    line_id = only_line(fx)
    before = balances(fx)
    result = book(fx, line_id, narration="coffee", reason="the card purchase is office supplies")
    assert result.entry.status == "posted"
    assert {p.account_id for p in result.entry.legs} == {fx.acct(CASH), fx.acct(OFFICE)}
    st = state(fx, line_id)
    assert st.status == ln.BOOKED_STATUS
    assert st.unexplained_micros == 0 and st.allocated_micros == -3000000
    assert st.live.entry_id == result.entry.id
    assert balances(fx) != before, "booking a line DOES move the books; it is the one classification that does"


def test_the_cash_leg_is_the_line_and_is_not_the_callers_to_supply(fx):
    """A booking cannot introduce money the statement did not have: the contra legs must sum to minus the line."""
    line_id = only_line(fx)
    e = refused(fx, lambda ctx: ln.book_line(ctx, line_id=line_id,
                                             legs=[en.Leg(fx.acct(OFFICE), "USD", "2000000")],
                                             narration="n", reason="r"))
    assert e.code == "unbalanced"
    d = e.details[0]
    assert (d["legs_micros"], d["expected_micros"]) == ("2000000", "3000000")
    assert d["reason"] == "booking_does_not_match_the_line"


def test_a_booking_may_be_split_across_several_contra_accounts(fx):
    line_id = only_line(fx)
    with fx.tx(AGENT) as ctx:
        result = ln.book_line(ctx, line_id=line_id, narration="a mixed receipt", reason="two things on one line",
                              legs=[en.Leg(fx.acct(OFFICE), "USD", "1000000"),
                                    en.Leg(fx.acct(SALES), "USD", "2000000")])
    assert len(result.entry.legs) == 3
    assert state(fx, line_id).status == ln.BOOKED_STATUS


def test_one_audit_action_and_no_proposal_chain(fx):
    line_id = only_line(fx)
    result = book(fx, line_id)
    assert result.audit.kind == "line.booked"
    assert result.audit.subject_id == line_id
    assert result.audit.reason
    names = {r[0] for r in fx.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names.isdisjoint({"proposals", "decisions", "applications"})


def test_the_document_behind_the_line_becomes_evidence_for_the_entry_it_booked(fx):
    line_id = only_line(fx)
    result = book(fx, line_id)
    filed = fx.q("SELECT entry_id, document_id FROM evidence_links WHERE entry_id=?", (result.entry.id,))
    assert len(filed) == 1
    assert filed[0][1] == fx.scalar("SELECT document_id FROM statements WHERE id=?",
                                    (state(fx, line_id).statement_id,))


# linking a line
def test_linking_a_line_to_an_existing_posting_moves_no_money(fx):
    line_id = only_line(fx)
    entry = posted_against(fx)
    before = balances(fx)
    with fx.tx(AGENT) as ctx:
        result = ln.link_line(ctx, line_id=line_id,
                              parts=[ln.Part(cash_posting(fx, entry).id, "-3000000")],
                              reason="this payment is already in the books")
    assert result.entry is None, "linking posts nothing"
    assert balances(fx) == before, "an allocation changed a balance; it must not be able to"
    st = state(fx, line_id)
    assert st.status == ln.LINKED_STATUS and st.unexplained_micros == 0


def test_a_line_may_be_split_across_two_postings(fx):
    """One line, two postings: that is the split, and it needs no verb of its own."""
    line_id = only_line(fx, closing="-5000000", items=(("2026-03-05", "-5000000"),))
    a, b = posted_against(fx, amount="2000000"), posted_against(fx, amount="3000000")
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=line_id, reason="one payment settled two invoices",
                     parts=[ln.Part(cash_posting(fx, a).id, "-2000000"),
                            ln.Part(cash_posting(fx, b).id, "-3000000")])
    st = state(fx, line_id)
    assert st.status == ln.LINKED_STATUS and st.unexplained_micros == 0
    assert len(st.live.allocations) == 2


def test_a_partial_allocation_is_a_state_and_is_reported_as_one(fx):
    line_id = only_line(fx, closing="-5000000", items=(("2026-03-05", "-5000000"),))
    entry = posted_against(fx, amount="2000000")
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=line_id, parts=[ln.Part(cash_posting(fx, entry).id, "-2000000")],
                     reason="part of it, so far")
    st = state(fx, line_id)
    assert st.status == ln.PARTLY
    assert (st.allocated_micros, st.unexplained_micros) == (-2000000, -3000000)


@pytest.mark.parametrize("amount,code", [("-4000000", "overallocated"), ("3000000", "allocation_invalid")])
def test_a_line_cannot_be_over_allocated_or_allocated_against_its_own_sign(fx, amount, code):
    line_id = only_line(fx)
    entry = posted_against(fx, amount="4000000")
    e = refused(fx, lambda ctx: ln.link_line(ctx, line_id=line_id,
                                             parts=[ln.Part(cash_posting(fx, entry).id, amount)], reason="r"))
    assert e.code == code


def test_a_posting_cannot_be_explained_twice_over_by_two_lines(fx):
    """The bound the line side cannot see: one posting, two lines, each within its own line's amount."""
    first = statement(fx, closing="-6000000",
                      items=(("2026-03-05", "-3000000"), ("2026-03-06", "-3000000")))
    entry = posted_against(fx, amount="3000000")
    posting = cash_posting(fx, entry).id
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=first[0].id, parts=[ln.Part(posting, "-3000000")], reason="this one")
    e = refused(fx, lambda ctx: ln.link_line(ctx, line_id=first[1].id, parts=[ln.Part(posting, "-3000000")],
                                             reason="and this one too"))
    assert e.code == "overallocated"
    d = e.details[0]
    assert (d["already_allocated_micros"], d["posting_micros"]) == ("3000000", "3000000")


def test_a_line_may_only_be_linked_to_its_own_account_and_commodity(fx):
    line_id = only_line(fx)
    other = post(fx, "2026-03-05", "3000000", debit=OFFICE, credit=SALES)
    e = refused(fx, lambda ctx: ln.link_line(ctx, line_id=line_id,
                                             parts=[ln.Part(other.legs[0].id, "-3000000")], reason="r"))
    assert e.code == "allocation_invalid"


# ignoring a line
def test_ignoring_a_line_explains_it_without_booking_anything(fx):
    line_id = only_line(fx)
    before = balances(fx)
    with fx.tx(AGENT) as ctx:
        result = ln.ignore_line(ctx, line_id=line_id,
                                reason="an internal transfer we account for on the other side")
    assert result.entry is None and balances(fx) == before
    st = state(fx, line_id)
    assert st.status == ln.IGNORED_STATUS
    assert st.unexplained_micros == 0, "an ignored line is explained in full, or the close gates would hold"
    assert st.live.reason == "an internal transfer we account for on the other side"


def test_a_reason_is_required_because_the_reason_is_the_whole_decision(fx):
    line_id = only_line(fx)
    for bad in (None, "", "   "):
        assert refused(fx, lambda ctx, b=bad: ln.ignore_line(ctx, line_id=line_id, reason=b)).code == "validation"


# one line, one current status, and its history
def test_a_line_that_is_already_explained_refuses_a_second_opinion(fx):
    line_id = only_line(fx)
    book(fx, line_id)
    e = refused(fx, lambda ctx: ln.ignore_line(ctx, line_id=line_id, reason="on second thoughts"))
    assert e.code == "immutable"
    assert e.details[0]["remedy"] == "reexplain_line"


def test_the_status_is_derived_and_the_history_is_kept(fx):
    line_id = only_line(fx)
    with fx.tx(AGENT) as ctx:
        ln.ignore_line(ctx, line_id=line_id, reason="thought it was a duplicate")
    with fx.tx(HUMAN) as ctx:
        ln.reexplain_line(ctx, line_id=line_id, kind="booked", reason="it is ours after all",
                          legs=[en.Leg(fx.acct(OFFICE), "USD", "3000000")], narration="coffee")
    st = state(fx, line_id)
    assert st.status == ln.BOOKED_STATUS
    assert [x.kind for x in st.history] == ["ignored", "booked"]
    assert st.history[1].supersedes_id == st.history[0].id
    assert st.live.id == st.history[1].id
    assert st.history[0].created_by == AGENT and st.history[1].created_by == HUMAN


def test_the_chain_is_a_chain_and_not_a_tree(fx):
    """``explanations.supersedes_id`` is UNIQUE, so two people cannot both supersede one explanation."""
    line_id = only_line(fx)
    with fx.tx(AGENT) as ctx:
        first = ln.ignore_line(ctx, line_id=line_id, reason="not ours")
    with fx.tx(AGENT) as ctx:
        ln.reexplain_line(ctx, line_id=line_id, kind="ignored", reason="still not ours, for a better reason")
    with pytest.raises(HaddonError):
        with fx.tx(AGENT) as ctx:
            ctx.insert(t.explanations, None, "x", line_id=line_id, kind="ignored", reason="a third opinion",
                       entry_id=None, correction_id=None, supersedes_id=first.explanation.id)


def test_reexplaining_a_booked_line_corrects_the_entry_rather_than_editing_it(fx):
    line_id = only_line(fx)
    booked = book(fx, line_id, contra=OFFICE)
    with fx.tx(HUMAN) as ctx:
        again = ln.reexplain_line(ctx, line_id=line_id, kind="booked", reason="it was a sale, not a cost",
                                  legs=[en.Leg(fx.acct(SALES), "USD", "3000000")], narration="a sale")
    assert again.correction is not None
    assert again.correction.original_id == booked.entry.id
    assert again.entry is not None and again.entry.id == again.correction.replacement_id
    # the original is untouched and still posted: a correction stands beside it
    with fx.reader() as conn:
        original = en.get_entry(conn, fx.entity_id, booked.entry.id)
    assert original.status == "posted"
    st = state(fx, line_id)
    assert st.status == ln.BOOKED_STATUS and st.live.entry_id == again.entry.id


def test_reexplaining_a_booked_line_as_ignored_reverses_what_it_booked(fx):
    line_id = only_line(fx)
    before = balances(fx)
    book(fx, line_id)
    with fx.tx(HUMAN) as ctx:
        result = ln.reexplain_line(ctx, line_id=line_id, kind="ignored",
                                   reason="the bank reversed it; it never happened")
    assert result.correction is not None and result.correction.replacement_id is None
    # every account the booking touched is back to nothing.  (The accounts themselves now appear in the trial
    # balance at zero, because they have postings; what must be unchanged is the money.)
    assert [b for b in balances(fx) if b[2] != "0"] == [b for b in before if b[2] != "0"]
    assert state(fx, line_id).status == ln.IGNORED_STATUS


def test_a_line_with_nothing_said_about_it_cannot_be_reexplained(fx):
    line_id = only_line(fx)
    e = refused(fx, lambda ctx: ln.reexplain_line(ctx, line_id=line_id, kind="ignored", reason="r"))
    assert e.code == "immutable" and "no explanation to supersede" in e.message


# the period lock, closed
def test_a_locked_line_cannot_be_explained_at_all(fx):
    post(fx, "2026-01-05", "1000000", debit=OFFICE, credit=SALES)  # a journal with no posting cannot be closed
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),))
    lock_books(fx, "2026-03-31")
    for call in (lambda ctx: ln.ignore_line(ctx, line_id=rows[0].id, reason="r"),
                 lambda ctx: ln.book_line(ctx, line_id=rows[0].id, narration="n", reason="r",
                                          legs=[en.Leg(fx.acct(OFFICE), "USD", "3000000")])):
        e = refused(fx, call)
        assert e.code == "period_locked", e.code


def test_a_link_to_a_posting_in_a_locked_period_is_refused_even_when_the_line_is_open(fx):
    """Both dates matter: the line's own, and the date of the entry the allocation points at."""
    entry = posted_against(fx, dt="2026-02-10", amount="3000000")
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),))
    lock_books(fx, "2026-02-28")
    e = refused(fx, lambda ctx: ln.link_line(ctx, line_id=rows[0].id, reason="r",
                                             parts=[ln.Part(cash_posting(fx, entry).id, "-3000000")]))
    assert e.code == "period_locked"


def test_re_explaining_may_not_release_an_allocation_against_a_locked_posting(fx):
    """The other direction of the same rule: releasing an allocation is checked against the lock too.

    Superseding an explanation retires its allocations, and a ``linked`` explanation has no ``entry_id``, so the
    dates of its postings have to be looked up.  Without that, a March line linked to a February posting could
    be re-explained after February closed, and February's posting would quietly stop being explained.
    """
    entry = posted_against(fx, dt="2026-02-10", amount="3000000")
    posting = cash_posting(fx, entry).id
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),))
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=rows[0].id, parts=[ln.Part(posting, "-3000000")],
                     reason="this line is the february payment")
    lock_books(fx, "2026-02-28")
    for kind in ("ignored", "linked", "booked"):
        e = refused(fx, lambda ctx, k=kind: ln.reexplain_line(
            ctx, line_id=rows[0].id, kind=k, reason="it was not that payment after all",
            parts=[] if k != "linked" else [],
            legs=None if k != "booked" else [en.Leg(fx.acct(OFFICE), "USD", "3000000")]))
        assert e.code == "period_locked", (kind, e.code)
        assert e.details[0]["posting_id"] == posting and e.details[0]["remedy"] == "reopen"
    # the allocation is still live, and February is still explained
    with fx.reader() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM live_allocations WHERE posting_id=:p"),
                            {"p": posting}).scalar() == 1


def test_the_locked_release_refusal_is_the_databases_and_not_only_the_repositorys(fx, monkeypatch):
    """The D6 positive control: with the Python pre-check removed, ``explanations_insert`` still refuses."""
    entry = posted_against(fx, dt="2026-02-10", amount="3000000")
    posting = cash_posting(fx, entry).id
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),))
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=rows[0].id, parts=[ln.Part(posting, "-3000000")], reason="the payment")
    lock_books(fx, "2026-02-28")
    monkeypatch.setattr(ln, "_check_allocations_open", lambda *a, **k: None)
    e = refused(fx, lambda ctx: ln.reexplain_line(ctx, line_id=rows[0].id, kind="ignored", reason="r"))
    assert e.code == "period_locked", e.code
    assert e.details[0] == {"db_error": "period_locked"}, "the trigger refused, not the Python pre-check"
    with fx.reader() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM live_allocations WHERE posting_id=:p"),
                            {"p": posting}).scalar() == 1


def test_re_explaining_is_allowed_once_the_period_holding_the_posting_is_open(fx):
    """The control: the refusal above is about the lock, not about re-explaining a linked line."""
    entry = posted_against(fx, dt="2026-03-10", amount="3000000")
    posting = cash_posting(fx, entry).id
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),))
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=rows[0].id, parts=[ln.Part(posting, "-3000000")], reason="the payment")
    with fx.tx(AGENT) as ctx:
        ln.reexplain_line(ctx, line_id=rows[0].id, kind="ignored", reason="it was not that payment after all")
    assert state(fx, rows[0].id).status == ln.IGNORED_STATUS


# a replaced statement takes its lines with it
def test_replacing_a_statement_retires_its_lines_and_frees_the_postings_they_held(fx):
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),), tag="mar-a")
    entry = posted_against(fx)
    posting = cash_posting(fx, entry).id
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=rows[0].id, parts=[ln.Part(posting, "-3000000")], reason="this payment")
    replacement = load(fx, closing="-3000000", rows=[row("2026-03-05", "-3000000")], tag="mar-b",
                       replaces_id=rows[0].statement_id)
    assert state(fx, rows[0].id).status == ln.SUPERSEDED_INPUT
    # the posting is free again, because the input that claimed it is no longer the live one
    new_line = lines_of(fx, replacement.id)[0]
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=new_line.id, parts=[ln.Part(posting, "-3000000")],
                     reason="the corrected statement says the same thing")
    assert state(fx, new_line.id).status == ln.LINKED_STATUS


def test_a_line_of_a_replaced_statement_cannot_be_explained_again(fx):
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),), tag="mar-a")
    load(fx, closing="-3000000", rows=[row("2026-03-05", "-3000000")], tag="mar-b",
         replaces_id=rows[0].statement_id)
    e = refused(fx, lambda ctx: ln.ignore_line(ctx, line_id=rows[0].id, reason="r"))
    assert e.code == "immutable" and "has been replaced" in e.message


def test_a_statement_may_not_be_replaced_while_its_lines_hold_a_locked_posting(fx):
    """Replacement retires the predecessor's explanations, so it releases their allocations too.

    ``_check_replaced`` checks the predecessor's own period; that says nothing about where the postings its
    lines are *linked to* are dated.  A March statement whose line explains a February posting could be replaced
    after February closed, unexplaining February without anybody reopening it.
    """
    entry = posted_against(fx, dt="2026-02-10", amount="3000000")
    posting = cash_posting(fx, entry).id
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),), tag="mar-a")
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=rows[0].id, parts=[ln.Part(posting, "-3000000")],
                     reason="this line is the february payment")
    lock_books(fx, "2026-02-28")
    with pytest.raises(HaddonError) as ei:
        load(fx, closing="-3000000", rows=[row("2026-03-05", "-3000000")], tag="mar-b",
             replaces_id=rows[0].statement_id)
    assert ei.value.code == "period_locked", ei.value.code
    d = ei.value.details[0]
    assert (d["posting_id"], d["dt"], d["remedy"]) == (posting, "2026-02-10", "reopen")
    with fx.reader() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM live_allocations WHERE posting_id=:p"),
                            {"p": posting}).scalar() == 1


def test_that_replacement_refusal_is_the_databases_too(fx, monkeypatch):
    """D6 again: with the Python pre-check removed, ``statements_insert`` refuses the same replacement."""
    from haddon.repository import statements as st_repo

    entry = posted_against(fx, dt="2026-02-10", amount="3000000")
    posting = cash_posting(fx, entry).id
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),), tag="mar-a")
    with fx.tx(AGENT) as ctx:
        ln.link_line(ctx, line_id=rows[0].id, parts=[ln.Part(posting, "-3000000")], reason="the payment")
    lock_books(fx, "2026-02-28")
    monkeypatch.setattr(st_repo, "_refuse_locked_allocations", lambda *a, **k: None)
    with pytest.raises(HaddonError) as ei:
        load(fx, closing="-3000000", rows=[row("2026-03-05", "-3000000")], tag="mar-b",
             replaces_id=rows[0].statement_id)
    assert ei.value.code == "period_locked", ei.value.code
    assert ei.value.details[0] == {"db_error": "old_period_locked"}, "the trigger refused, not the pre-check"


def test_a_statement_whose_line_is_booked_cannot_be_replaced_until_the_booking_is_undone(fx):
    rows = statement(fx, closing="-3000000", items=(("2026-03-05", "-3000000"),), tag="mar-a")
    book(fx, rows[0].id)
    with pytest.raises(HaddonError) as ei:
        load(fx, closing="-3000000", rows=[row("2026-03-05", "-3000000")], tag="mar-b",
             replaces_id=rows[0].statement_id)
    assert ei.value.code == "immutable" and "still booked" in ei.value.message
    # undo the booking and the replacement goes through
    with fx.tx(HUMAN) as ctx:
        ln.reexplain_line(ctx, line_id=rows[0].id, kind="ignored", reason="the statement itself was wrong")
    replacement = load(fx, closing="-3000000", rows=[row("2026-03-05", "-3000000")], tag="mar-c",
                       replaces_id=rows[0].statement_id)
    assert replacement.replaces_id == rows[0].statement_id


# what a draft revision may not do
def test_a_draft_revision_may_not_move_a_posting_an_allocation_rests_on(fx):
    """The core's guard, supplied by this extension through ``entries.register_allocation_guard`` (issue 0004).

    The journal core does not know what an allocation is; it asks, and with the extension absent it is told
    "nothing is allocated" and a draft revises freely.
    """
    line_id = only_line(fx)
    with fx.tx(AGENT) as ctx:
        draft = en.create_draft(ctx, dt="2026-03-05", narration="n", origin="manual",
                                legs=[en.Leg(fx.acct(CASH), "USD", "-3000000"),
                                      en.Leg(fx.acct(OFFICE), "USD", "3000000")])
        sealed = en.seal(ctx, draft.id, expected_version=draft.version)
        ln.link_line(ctx, line_id=line_id, parts=[ln.Part(sealed.legs[0].id, "-3000000")], reason="the same money")
    with fx.reader() as conn:
        held = ln._allocated_postings_of_entry(type("C", (), {"conn": conn, "entity_id": fx.entity_id})(),
                                               sealed.id)
    assert held == {sealed.legs[0].id}


# reading back
def test_the_line_state_reads_the_same_facts_from_the_database_as_the_write_returned(fx):
    line_id = only_line(fx)
    result = book(fx, line_id)
    st = state(fx, line_id)
    stored = fx.q("SELECT kind, reason, entry_id FROM explanations WHERE line_id=?", (line_id,))
    assert stored == [(st.live.kind, st.live.reason, result.entry.id)]
    allocated = fx.q("SELECT posting_id, amount_micros FROM allocations WHERE line_id=?", (line_id,))
    assert allocated == [(a.posting_id, a.amount_micros) for a in st.live.allocations]


def test_a_line_this_entity_does_not_hold_is_a_404(fx):
    with fx.reader() as conn:
        with pytest.raises(HaddonError) as ei:
            ln.line_state(conn, fx.entity_id, "sl-nope")
    assert (ei.value.code, ei.value.status) == ("not_found", 404)


# an entry the line booked, corrected on its own
# A line booked an entry, then the entry was corrected.  ``POST /entries/{id}/correct`` moves the line onto the
# replacement.  A correction written past the command leaves the line on the mirrored original, shown as
# unbooked, and ``reexplain`` has to repair that without mirroring the original a second time.

def _corrected_behind_the_lines_back(fx, entry_id: str, *, legs) -> "corrections_repo.Correction":
    """A correction written past the command, so nothing moved the line along with it."""
    with fx.tx(HUMAN) as ctx:
        original = en.get_entry(ctx.conn, fx.entity_id, entry_id)
        return corrections_repo.reverse_entry(
            ctx, original_id=entry_id, dt=original.dt, reason="split out the VAT",
            replacement=None if legs is None else corrections_repo.Replacement(
                dt=original.dt, narration=original.narration, origin="manual", legs=legs))


def _split_legs(fx):
    return [en.Leg(fx.acct(CASH), "USD", "-3000000"), en.Leg(fx.acct(OFFICE), "USD", "2500000"),
            en.Leg(fx.acct(SALES), "USD", "500000")]


def test_correcting_a_booked_entry_moves_the_line_onto_the_replacement(fx):
    line_id = only_line(fx)
    booked = book(fx, line_id)
    with fx.tx(HUMAN) as ctx:
        corrected = commands.correct_entry(
            ctx, original_id=booked.entry.id, reason="split out the VAT",
            replacement=commands.Replacement(dt="2026-03-05", narration="office supplies", origin="manual",
                                             legs=[commands.LegSpec(CASH, "USD", "-3.00"),
                                                   commands.LegSpec(OFFICE, "USD", "2.50"),
                                                   commands.LegSpec(SALES, "USD", "0.50")]))
    c = corrected.correction
    st = state(fx, line_id)
    assert st.status == ln.BOOKED_STATUS
    assert st.live.entry_id == c.replacement_id and st.live.correction_id == c.id
    assert st.live.supersedes_id == booked.explanation.id
    # the line holds the replacement's cash posting, for the whole line
    with fx.reader() as conn:
        replacement = en.get_entry(conn, fx.entity_id, c.replacement_id)
    assert [(a.posting_id, a.amount_micros) for a in st.live.allocations] == \
        [(cash_posting(fx, replacement).id, -3000000)]
    # nothing was rewritten: the old explanation is still in the history, and the one audit action names the move
    assert [x.id for x in st.history][0] == booked.explanation.id and len(st.history) == 2
    assert corrected.audit.result["explanations"][0]["line_id"] == line_id


def test_a_correction_that_drops_the_lines_money_is_refused_and_points_at_reexplain(fx):
    line_id = only_line(fx)
    booked = book(fx, line_id)
    e = refused(fx, lambda ctx: commands.correct_entry(
        ctx, original_id=booked.entry.id, reason="it was 2.50, not 3.00",
        replacement=commands.Replacement(dt="2026-03-05", narration="office supplies", origin="manual",
                                         legs=[commands.LegSpec(CASH, "USD", "-2.50"),
                                               commands.LegSpec(OFFICE, "USD", "2.50")])), actor=HUMAN)
    assert e.code == "immutable" and e.details[0]["remedy"] == f"POST /lines/{line_id}/reexplain"
    assert state(fx, line_id).live.entry_id == booked.entry.id


def test_a_stuck_line_can_be_pointed_at_the_standing_replacement_without_posting_anything(fx):
    line_id = only_line(fx)
    booked = book(fx, line_id)
    c = _corrected_behind_the_lines_back(fx, booked.entry.id, legs=_split_legs(fx))
    with fx.reader() as conn:
        replacement = en.get_entry(conn, fx.entity_id, c.replacement_id)
    before, entries_before = balances(fx), fx.q("SELECT count(*) FROM entries")
    with fx.tx(HUMAN) as ctx:
        result = ln.reexplain_line(ctx, line_id=line_id, kind="linked", reason="the line is the corrected entry",
                                   parts=[ln.Part(cash_posting(fx, replacement).id, "-3000000")])
    assert result.correction is None
    assert balances(fx) == before and fx.q("SELECT count(*) FROM entries") == entries_before
    # recorded as booked by the replacement: the line still owns it, so changing its mind later corrects it
    st = state(fx, line_id)
    assert st.status == ln.BOOKED_STATUS and st.live.kind == ln.BOOKED and st.live.entry_id == replacement.id
    with fx.tx(HUMAN) as ctx:
        ignored = ln.reexplain_line(ctx, line_id=line_id, kind="ignored", reason="not ours after all")
    assert ignored.correction.original_id == replacement.id


def test_a_stuck_line_may_not_be_split_across_its_own_replacement(fx):
    line_id = only_line(fx)
    booked = book(fx, line_id)
    c = _corrected_behind_the_lines_back(fx, booked.entry.id, legs=_split_legs(fx))
    with fx.reader() as conn:
        replacement = en.get_entry(conn, fx.entity_id, c.replacement_id)
    e = refused(fx, lambda ctx: ln.reexplain_line(
        ctx, line_id=line_id, kind="linked", reason="half of it",
        parts=[ln.Part(cash_posting(fx, replacement).id, "-1000000")]), actor=HUMAN)
    assert e.code == "immutable" and e.details[0]["entry_id"] == replacement.id


def test_rebooking_a_stuck_line_corrects_the_standing_replacement_not_the_original(fx):
    line_id = only_line(fx)
    booked = book(fx, line_id)
    c = _corrected_behind_the_lines_back(fx, booked.entry.id, legs=_split_legs(fx))
    with fx.tx(HUMAN) as ctx:
        again = ln.reexplain_line(ctx, line_id=line_id, kind="booked", reason="it was a sale",
                                  legs=[en.Leg(fx.acct(SALES), "USD", "3000000")], narration="a sale")
    assert again.correction.original_id == c.replacement_id
    assert state(fx, line_id).live.entry_id == again.correction.replacement_id


def test_ignoring_a_stuck_line_reverses_the_standing_replacement(fx):
    line_id = only_line(fx)
    before = balances(fx)
    booked = book(fx, line_id)
    c = _corrected_behind_the_lines_back(fx, booked.entry.id, legs=_split_legs(fx))
    with fx.tx(HUMAN) as ctx:
        result = ln.reexplain_line(ctx, line_id=line_id, kind="ignored", reason="the bank reversed it")
    assert result.correction.original_id == c.replacement_id and result.correction.replacement_id is None
    assert [b for b in balances(fx) if b[2] != "0"] == [b for b in before if b[2] != "0"]


def test_a_line_whose_booking_was_reversed_books_afresh_when_reexplained(fx):
    line_id = only_line(fx)
    booked = book(fx, line_id)
    _corrected_behind_the_lines_back(fx, booked.entry.id, legs=None)
    with fx.tx(HUMAN) as ctx:
        again = ln.reexplain_line(ctx, line_id=line_id, kind="booked", reason="it was ours after all",
                                  legs=[en.Leg(fx.acct(OFFICE), "USD", "3000000")])
    # nothing stands to correct, so the new booking is an ordinary posting
    assert again.correction is None and again.entry.status == "posted"
    assert state(fx, line_id).live.entry_id == again.entry.id
