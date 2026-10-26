"""Append-only event log: monotonically increasing seq, paging in seq order, high-water mark."""

from __future__ import annotations

import sqlite3

import pytest

from haddon.domain.errors import HaddonError
from haddon.repository import events as ev

from repo_helpers import AGENT, HUMAN


def test_append_event_returns_increasing_seq(fx):
    with fx.reader() as conn:
        assert ev.high_water(conn, fx.entity_id) == 0
        assert ev.read_events(conn, fx.entity_id) == []
    seqs = []
    with fx.tx(now="2026-09-10T12:00:00Z") as ctx:
        for i in range(5):
            seqs.append(ev.append_event(ctx, kind="entry.posted", subject_kind="entry", subject_id=f"e{i}", before=None,
                                        after={"status": "posted", "i": i}, reason="r" if i % 2 else None))
    assert seqs == sorted(seqs) and len(set(seqs)) == 5 and all(isinstance(s, int) for s in seqs)
    assert seqs[0] >= 1 and seqs[-1] == seqs[0] + 4
    with fx.tx(HUMAN) as ctx:
        later = ev.append_event(ctx, kind="lock.closed", subject_kind="lock", subject_id="lock")
    assert later > seqs[-1]
    assert fx.q("SELECT count(*), min(seq), max(seq) FROM events") == [(6, seqs[0], later)]
    assert fx.q("SELECT created_at, created_by FROM events WHERE seq=?", (seqs[0],)) == [("2026-09-10T12:00:00Z", AGENT)]


def test_seq_survives_rollback_without_reuse(fx):
    """AUTOINCREMENT: a rolled-back seq is never handed out again (the log has no gaps filled from the past)."""
    with fx.tx() as ctx:
        a = ev.append_event(ctx, kind="k", subject_kind="s", subject_id="1")
    with pytest.raises(RuntimeError):
        with fx.tx() as ctx:
            ev.append_event(ctx, kind="k", subject_kind="s", subject_id="2")
            raise RuntimeError("abort")
    with fx.tx() as ctx:
        c = ev.append_event(ctx, kind="k", subject_kind="s", subject_id="3")
    assert c > a and fx.scalar("SELECT count(*) FROM events") == 2


def test_read_events_pages_in_seq_order(fx):
    with fx.tx() as ctx:
        seqs = [ev.append_event(ctx, kind="k", subject_kind="s", subject_id=str(i), after=[i], request_id=None) for i in range(7)]
    with fx.reader() as conn:
        page1 = ev.read_events(conn, fx.entity_id, since=0, limit=3)
        assert [e.seq for e in page1] == seqs[:3] and [e.after for e in page1] == [[0], [1], [2]]
        page2 = ev.read_events(conn, fx.entity_id, since=page1[-1].seq, limit=3)
        assert [e.seq for e in page2] == seqs[3:6]
        page3 = ev.read_events(conn, fx.entity_id, since=page2[-1].seq, limit=3)
        assert [e.seq for e in page3] == seqs[6:]
        assert ev.read_events(conn, fx.entity_id, since=page3[-1].seq) == []
        everything = ev.read_events(conn, fx.entity_id, limit=500)
        assert [e.seq for e in everything] == seqs and everything[0].before is None and everything[0].created_by == AGENT
        assert ev.high_water(conn, fx.entity_id) == seqs[-1]
        for bad in (0, 501, -1):
            with pytest.raises(HaddonError) as ei:
                ev.read_events(conn, fx.entity_id, limit=bad)
            assert ei.value.code == "validation"


def test_append_event_validation(fx):
    with fx.tx() as ctx:
        for kw in ({"kind": ""}, {"subject_kind": None}, {"subject_id": 3}):
            args = dict(kind="k", subject_kind="s", subject_id="x")
            args.update(kw)
            with pytest.raises(HaddonError) as ei:
                ev.append_event(ctx, **args)
            assert ei.value.code == "validation"
        ev.append_event(ctx, kind="k", subject_kind="s", subject_id="x")  # positive control after the refusals
    assert fx.scalar("SELECT count(*) FROM events") == 1


def test_event_with_unknown_request_id_fails_at_commit_and_leaves_nothing(fx):
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with fx.tx() as ctx:
            ev.append_event(ctx, kind="k", subject_kind="s", subject_id="x", request_id="rq-ghost")
    assert fx.scalar("SELECT count(*) FROM events") == 0


def test_events_are_append_only_in_the_database(fx):
    with fx.tx() as ctx:
        s = ev.append_event(ctx, kind="k", subject_kind="s", subject_id="x")
    con = fx.raw()
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        con.execute("UPDATE events SET kind='tampered' WHERE seq=?", (s,))
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        con.execute("DELETE FROM events WHERE seq=?", (s,))
    con.close()
