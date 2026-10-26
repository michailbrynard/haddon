"""The authorisation model: the record says who did what; it does not decide who may.

Two properties are under test here.

1. The agent can run the whole lifecycle unaided.  Provision, post, close, reopen and correct, with no second
   party anywhere and no human-shaped actor in sight.  There is no approval to record (issue 0005): the audit
   action is the whole trail.
2. A human working on top still works, and is attributed to the human.  The owner or accountant inspects and
   edits; nothing they do takes a different code path from what the agent does, and every row carries the actor
   that wrote it.

Nothing here asserts that an actor string means anything.  ``agent:`` / ``human:`` is a readability convention
in the values written; if any code ever starts reading it, these tests are where that shows up.
"""

from __future__ import annotations

import json

import pytest

from haddon import commands
from haddon.domain.errors import HaddonError
from haddon.repository import closing as cl
from haddon.repository import corrections as co
from haddon.repository import entries as en

from repo_helpers import AGENT, HUMAN, blocking_overrides, pair, post

OWNER = "human:owner"
BOT = "agent:alpha"


def close(ctx, through: str, reason: str):
    """Close on the acting principal's own authority: one command, no second party, and an override per
    blocking check because no statement has been loaded."""
    lock = cl.get_lock(ctx.conn, ctx.entity_id)
    return commands.close_period(ctx, through=through, reason=reason, expected_version=lock.version,
                                 overrides=blocking_overrides(ctx.conn, ctx.entity_id, through,
                                                              reason="no statement is loaded for this month"))


def test_agent_completes_the_whole_lifecycle_unaided(fx):
    """Post, close, reopen, correct and close again: one actor, start to finish, with nothing to approve.

    There is no approval to mint (issue 0005).  The audit action is the record, and it carries the actor without
    anything reading it.
    """
    e1 = post(fx, "2026-02-10", "1250000", narration="february rent", actor=BOT)
    assert fx.q("SELECT created_by FROM entries WHERE id=?", (e1.id,)) == [(BOT,)]

    with fx.tx(BOT) as ctx:
        closed = close(ctx, "2026-02-28", "february close")
    assert closed.lock.through == "2026-02-28" and closed.audit.actor == BOT

    # The lock it just set binds it exactly as it binds anyone: no self-exemption.
    with pytest.raises(HaddonError) as ei:
        post(fx, "2026-02-11", "10000", actor=BOT)
    assert ei.value.code == "period_locked"

    # Reopen, on its own authority again.
    with fx.tx(BOT) as ctx:
        reopened = commands.reopen_period(ctx, through="2026-01-31", reason="a february invoice arrived late",
                                          expected_version=closed.lock.version)
    assert reopened.lock.through == "2026-01-31" and reopened.audit.actor == BOT

    # Correct its own posting: an opposite entry, with the original left standing.
    with fx.tx(BOT) as ctx:
        c = co.reverse_entry(ctx, original_id=e1.id, dt="2026-02-12",
                             reason="rent was 1,200, not 1,250",
                             replacement=co.Replacement("2026-02-12", "rent, corrected", "manual", pair(fx, "1200000")))
    assert c.original_id == e1.id and c.replacement_id is not None
    with fx.reader() as conn:
        assert en.get_entry(conn, fx.entity_id, e1.id).status == "posted"       # the original still stands
        assert en.leg_multiset(en.get_entry(conn, fx.entity_id, c.mirror_id)) == co.negated_multiset(
            en.get_entry(conn, fx.entity_id, e1.id))                            # and an opposite entry sits beside it

    # Close again, over the corrected period.
    with fx.tx(BOT) as ctx:
        assert close(ctx, "2026-02-28", "re-close after the rent correction").lock.through == "2026-02-28"

    # Every actor on the trail is the agent, and every one of them was recorded.
    assert {r[0] for r in fx.q("SELECT DISTINCT created_by FROM entries")} == {BOT}
    assert {r[0] for r in fx.q("SELECT DISTINCT created_by FROM close_records")} == {BOT}
    assert fx.scalar("SELECT count(*) FROM events WHERE created_by<>?", (BOT,)) == 0
    # The overrides on those closes name the agent as the principal that decided them.
    overrides = [w for row in fx.q("SELECT overrides FROM close_records") for w in json.loads(row[0])]
    assert overrides and {w["actor"] for w in overrides} == {BOT}


def test_a_human_edit_on_top_of_agent_work_is_attributed_to_the_human(fx):
    """The owner takes the same paths the agent takes, and the record separates the two hands cleanly."""
    agent_entry = post(fx, "2026-03-05", "500000", narration="agent posting", actor=BOT)

    # The owner disagrees with what the agent booked and corrects it: a correction, not a veto.
    with fx.tx(OWNER) as ctx:
        posted = commands.correct_entry(
            ctx, original_id=agent_entry.id, dt="2026-03-06",
            reason="the owner disagrees with the amount the agent booked",
            replacement=commands.Replacement(dt="2026-03-06", narration="amount corrected", origin="manual",
                                             legs=[commands.LegSpec("expenses:office", "USD", "4.00"),
                                                   commands.LegSpec("assets:cash", "USD", "-4.00")]))
    c = posted.correction
    assert fx.q("SELECT created_by FROM entries WHERE id=?", (agent_entry.id,)) == [(BOT,)]
    assert fx.q("SELECT created_by FROM entries WHERE id=?", (c.mirror_id,)) == [(OWNER,)]
    assert fx.q("SELECT created_by FROM entries WHERE id=?", (c.replacement_id,)) == [(OWNER,)]
    assert fx.q("SELECT created_by FROM corrections WHERE id=?", (c.id,)) == [(OWNER,)]
    # The reason for the disagreement survives on the audit action, which is the whole record.
    assert posted.audit.actor == OWNER and posted.audit.kind == "entry.corrected"
    assert fx.scalar("SELECT reason FROM events WHERE kind='entry.corrected'") == \
        "the owner disagrees with the amount the agent booked"

    # The owner closes.  The close is theirs; the agent's earlier work is untouched and still attributed to it.
    with fx.tx(OWNER) as ctx:
        close(ctx, "2026-03-31", "march close")
    assert fx.q("SELECT created_by FROM close_records") == [(OWNER,)]
    assert fx.q("SELECT created_by FROM entries WHERE id=?", (agent_entry.id,)) == [(BOT,)]


def test_the_actor_is_carried_through_unchanged_whatever_its_shape(fx):
    """An actor is one opaque string.  Nothing parses it, so nothing about its shape changes what happens."""
    shapes = ["agent:alpha", "human:reviewer", "human:", "service:reconciler/v3", "  padded  ", "x", "人", AGENT, HUMAN]
    for i, actor in enumerate(shapes):
        e = post(fx, "2026-04-0%d" % (i % 9 + 1), "10000", narration=f"n{i}", actor=actor)
        assert fx.q("SELECT created_by FROM entries WHERE id=?", (e.id,)) == [(actor,)]


@pytest.mark.parametrize("actor", ["", "   ", "\t\n", None, 7])
def test_a_missing_or_blank_actor_is_the_only_thing_refused(fx, actor):
    with pytest.raises(HaddonError) as ei:
        with fx.tx(actor) as ctx:
            en.post_entry(ctx, dt="2026-04-01", narration="n", origin="manual", legs=pair(fx, "10000"))
    assert ei.value.details[0]["reason"] == "actor_required"
    assert fx.scalar("SELECT count(*) FROM entries") == 0


def test_no_code_reads_an_actor_prefix(fx):
    """A structural guard: no code in the package branches on actor type.

    Fails if somebody reintroduces a human/agent branch anywhere in the package.
    """
    import pathlib
    src = pathlib.Path(en.__file__).resolve().parents[1]
    offenders = []
    for py in sorted(src.rglob("*.py")):
        text = py.read_text()
        for n, line in enumerate(text.splitlines(), 1):
            if any(tok in line for tok in ("is_human", "HUMAN_PREFIX", "human_only", "HUMAN_ONLY")):
                offenders.append(f"{py.relative_to(src)}:{n}: {line.strip()}")
            # A literal actor prefix may appear as a value (importing.ACTOR) but never in a comparison.
            if '"human:"' in line or "'human:'" in line:
                offenders.append(f"{py.relative_to(src)}:{n}: {line.strip()}")
    assert offenders == []
