"""Transaction fault injection (spec: "Faults must leave no leaked draft, partial batch, success event or request
success"; review: RAISE(ABORT) aborts the statement, not the transaction, and a leaked draft can be committed
after a caught error).

One representative write walks the spec's stages: insert draft + legs -> seal -> explain (explanation +
allocation) -> record events/request -> commit.  A fault is injected at each stage and the database must be
byte-for-byte unchanged in every count that matters.  The success path is the positive control.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from haddon.db.connection import make_engine, sqlite_connect
from haddon.repository.errors import classify
from haddon.repository.transaction import TransactionAlreadyEnded, TransactionTainted, write_transaction

C = "entity_id,created_at,created_by"
CV = ("E", "t", "who")

STAGES = ["before_insert", "after_draft", "after_seal", "after_allocation", "after_events", "at_commit"]


class InjectedFault(Exception):
    pass


@pytest.fixture
def engine(tmp_path, templates):
    path = tmp_path / "entity.db"
    shutil.copy(templates[False], path)
    eng = make_engine(path, busy_timeout_ms=200)
    yield eng
    eng.dispose()


def snapshot(engine) -> dict:
    con = sqlite_connect(engine.url.database)
    try:
        q = lambda sql: con.execute(sql).fetchall()
        return {
            "entries_by_status": q("SELECT status, count(*) FROM entries GROUP BY status ORDER BY status"),
            "entry_ids": q("SELECT id FROM entries ORDER BY id"),
            "postings": q("SELECT count(*) FROM postings")[0][0],
            "explanations": q("SELECT id,kind FROM explanations ORDER BY id"),
            "allocations": q("SELECT count(*) FROM allocations")[0][0],
            "events": q("SELECT count(*), max(seq) FROM events")[0],
            "event_seq_counter": q("SELECT seq FROM sqlite_sequence WHERE name='events'"),
            "requests": q("SELECT count(*) FROM requests")[0][0],
            "fk_check": q("PRAGMA foreign_key_check"),
        }
    finally:
        con.close()


def book_and_allocate(conn, *, fault: str | None = None, eid: str = "fx1") -> None:
    """The representative write.  `fault` names the stage after which InjectedFault is raised."""
    x = lambda sql, params=(): conn.exec_driver_sql(sql, params)
    if fault == "before_insert":
        raise InjectedFault("before insert")
    # drafts and legs
    x(f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES(?,?,?,?,?,?,?)", (eid, *CV, "2026-03-20", "fault test", "agent"))
    x(f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES(?,?,?,?,?,?,?,?,?)", (f"{eid}.p1", *CV, eid, 1, "cash", "USD", -5_000_000))
    x(f"INSERT INTO postings(id,{C},entry_id,seq,account_id,commodity,amount_micros) VALUES(?,?,?,?,?,?,?,?,?)", (f"{eid}.p2", *CV, eid, 2, "exp", "USD", 5_000_000))
    if fault == "after_draft":
        raise InjectedFault("after draft insert")
    # seal
    x("UPDATE entries SET status='posted', version=2 WHERE id=?", (eid,))
    if fault == "after_seal":
        raise InjectedFault("after seal")
    # explanation: r4 (-5,000,000 cash, open period, unexplained in the seed) against the new cash leg
    x(f"INSERT INTO explanations(id,{C},line_id,kind,reason) VALUES(?,?,?,?,?,?,?)", (f"x_{eid}", *CV, "r4", "linked", "the same money"))
    x(f"INSERT INTO allocations(id,{C},explanation_id,line_id,posting_id,amount_micros) VALUES(?,?,?,?,?,?,?,?)", (f"al_{eid}", *CV, f"x_{eid}", "r4", f"{eid}.p1", -5_000_000))
    if fault == "after_allocation":
        raise InjectedFault("after allocation")
    # events and the durable request result
    request_id = "rq_ghost" if fault == "at_commit" else f"rq_{eid}"
    x(f"INSERT INTO events({C},kind,subject_kind,subject_id,after,request_id) VALUES(?,?,?,?,?,?,?,?)", (*CV, "entry.posted", "entry", eid, "{}", request_id))
    x(f"INSERT INTO events({C},kind,subject_kind,subject_id,after,request_id) VALUES(?,?,?,?,?,?,?,?)", (*CV, "line.linked", "statement_line", "r4", "{}", request_id))
    if fault == "after_events":
        raise InjectedFault("after events")
    # the deferred events->requests FK is what makes 'at_commit' fail: rq_ghost never gets inserted
    x(f"INSERT INTO requests(id,{C},caller,intent_key,method,path,request_hash,http_status,response) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"rq_{eid}", *CV, "agent", f"ik_{eid}", "POST", "/v1/entities/E/entries", "rh", 201, "{}"))


def test_success_path_commits_everything_and_runs_after_commit(engine):
    before = snapshot(engine)
    fired = []
    with write_transaction(engine, after_commit=lambda: fired.append(1)) as conn:
        book_and_allocate(conn)
    after = snapshot(engine)
    assert fired == [1]
    assert ("fx1",) in after["entry_ids"]
    assert after["postings"] == before["postings"] + 2
    assert after["allocations"] == before["allocations"] + 1
    assert after["events"][0] == before["events"][0] + 2
    assert after["requests"] == before["requests"] + 1
    assert len(after["explanations"]) == len(before["explanations"]) + 1
    assert after["fk_check"] == []
    con = sqlite_connect(engine.url.database)
    assert con.execute("SELECT status FROM entries WHERE id='fx1'").fetchone()[0] == "posted"
    assert con.execute("SELECT kind FROM explanations WHERE id='x_fx1'").fetchone()[0] == "linked"
    con.close()


@pytest.mark.parametrize("stage", STAGES)
def test_injected_fault_leaves_no_trace(engine, stage):
    before = snapshot(engine)
    fired = []
    expected = IntegrityError if stage == "at_commit" else InjectedFault
    with pytest.raises(expected):
        with write_transaction(engine, after_commit=lambda: fired.append(1)) as conn:
            book_and_allocate(conn, fault=stage)
    after = snapshot(engine)
    assert fired == [], "after_commit ran although the transaction failed"
    assert after == before, f"stage {stage} leaked state: {_diff(before, after)}"
    # explicit spellings of the spec's four guarantees
    con = sqlite_connect(engine.url.database)
    assert con.execute("SELECT count(*) FROM entries WHERE id='fx1'").fetchone()[0] == 0, "leaked draft"
    assert con.execute("SELECT count(*) FROM explanations WHERE id='x_fx1'").fetchone()[0] == 0, "partial batch"
    assert con.execute("SELECT count(*) FROM events WHERE subject_id='fx1'").fetchone()[0] == 0, "success event"
    assert con.execute("SELECT count(*) FROM requests WHERE id='rq_fx1'").fetchone()[0] == 0, "request success"
    con.close()
    # and the connection is reusable: the next write succeeds
    with write_transaction(engine) as conn:
        book_and_allocate(conn, eid="fx2")
    assert ("fx2",) in snapshot(engine)["entry_ids"]


def test_at_commit_failure_is_a_real_deferred_fk_violation(engine):
    with pytest.raises(IntegrityError) as ei:
        with write_transaction(engine) as conn:
            book_and_allocate(conn, fault="at_commit")
    assert "FOREIGN KEY" in str(ei.value.orig)
    assert classify(ei.value) == (422, "validation")


def test_caught_trigger_error_taints_transaction_and_forces_rollback(engine):
    """The review's demonstrated hazard: RAISE(ABORT) leaves the transaction open; caller catches and commits."""
    before = snapshot(engine)
    with pytest.raises(TransactionTainted):
        with write_transaction(engine) as conn:
            conn.exec_driver_sql(f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('leak','E','t','who','2026-03-01','half-done','manual')")
            try:
                conn.exec_driver_sql("UPDATE entries SET narration='x', version=2 WHERE id='e_posted'")
            except DBAPIError as e:
                assert str(e.orig) == "immutable"
                # transaction still open, exactly as the review observed ...
                assert conn.connection.driver_connection.in_transaction
            # ... caller "recovers" and lets the context manager commit
    after = snapshot(engine)
    assert after == before
    assert ("leak",) not in after["entry_ids"]


def test_caught_non_database_error_with_successful_sql_still_commits(engine):
    """Taint is about database errors only; application-level exceptions the caller handles are its business."""
    with write_transaction(engine) as conn:
        try:
            raise KeyError("not a db error")
        except KeyError:
            pass
        book_and_allocate(conn, eid="fx3")
    assert ("fx3",) in snapshot(engine)["entry_ids"]


def test_exception_after_successful_seal_rolls_back_the_sealed_entry(engine):
    """Spec: 'Use repository rollback on every error, including failures after successful sealing.'"""
    before = snapshot(engine)
    with pytest.raises(InjectedFault):
        with write_transaction(engine) as conn:
            book_and_allocate(conn, fault="after_seal")
            # unreachable
    assert snapshot(engine) == before


def test_manual_commit_inside_write_transaction_is_refused(engine):
    with pytest.raises(TransactionAlreadyEnded):
        with write_transaction(engine) as conn:
            conn.exec_driver_sql(f"INSERT INTO entries(id,{C},dt,narration,origin) VALUES('manual','E','t','who','2026-03-01','n','manual')")
            conn.connection.driver_connection.commit()  # caller must not do this
    # the row did get committed by the caller's illegal commit; the point is the repository shouts about it
    con = sqlite_connect(engine.url.database)
    assert con.execute("SELECT count(*) FROM entries WHERE id='manual'").fetchone()[0] == 1
    con.close()


def test_second_writer_gets_busy_not_partial_state(engine):
    """Two BEGIN IMMEDIATE writers: the second times out with a retryable busy error and writes nothing."""
    before = snapshot(engine)
    other = make_engine(engine.url.database, busy_timeout_ms=100)
    try:
        with write_transaction(engine) as conn:
            book_and_allocate(conn, eid="w1")
            with pytest.raises(DBAPIError) as ei:
                with write_transaction(other) as conn2:
                    book_and_allocate(conn2, eid="w2")
            assert classify(ei.value) == (503, "busy")
    finally:
        other.dispose()
    after = snapshot(engine)
    assert ("w1",) in after["entry_ids"] and ("w2",) not in after["entry_ids"]
    assert after["fk_check"] == []
    assert len(after["entry_ids"]) == len(before["entry_ids"]) + 1


def _diff(a: dict, b: dict) -> dict:
    return {k: (a[k], b[k]) for k in a if a[k] != b[k]}
