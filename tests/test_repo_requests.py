"""Durable request results (idempotency) and operations (durable jobs)."""

from __future__ import annotations

import pytest

from haddon import execution
from haddon.domain.errors import HaddonError
from haddon.repository import requests as rq
from haddon.repository.events import append_event

from repo_helpers import AGENT


def err(ei) -> HaddonError:
    assert isinstance(ei.value, HaddonError)
    return ei.value


def record(ctx, caller="agent", key="k1", h="h1", **kw):
    args = dict(caller=caller, intent_key=key, method="POST", path="/v1/entries", request_hash=h, http_status=201, response={"id": "e1"})
    args.update(kw)
    return rq.record_request(ctx, **args)


def test_same_key_same_hash_replays_stored_request(fx):
    with fx.tx() as ctx:
        assert rq.check_idempotency(ctx.conn, fx.entity_id, caller="agent", intent_key="k1", request_hash="h1") is None
        r = record(ctx)
    assert (r.caller, r.intent_key, r.http_status, r.response, r.operation_id) == ("agent", "k1", 201, {"id": "e1"}, None)
    with fx.reader() as conn:
        replay = rq.check_idempotency(conn, fx.entity_id, caller="agent", intent_key="k1", request_hash="h1")
        assert replay == r
        assert rq.find_request(conn, fx.entity_id, caller="agent", intent_key="k1") == r
        assert rq.find_request(conn, fx.entity_id, caller="agent", intent_key="k2") is None


def test_same_key_different_hash_is_409_idempotency_conflict(fx):
    with fx.tx() as ctx:
        r = record(ctx)
    with fx.reader() as conn:
        with pytest.raises(HaddonError) as ei:
            rq.check_idempotency(conn, fx.entity_id, caller="agent", intent_key="k1", request_hash="h2")
    e = err(ei)
    assert (e.code, e.status) == ("idempotency_conflict", 409) and e.details[0]["request_id"] == r.id
    with fx.tx() as ctx:  # recording a second result under the same key is refused too (the DB has UNIQUE as well)
        with pytest.raises(HaddonError) as ei:
            record(ctx, h="h2")
        assert err(ei).code == "idempotency_conflict"
    assert fx.scalar("SELECT count(*) FROM requests") == 1


def test_different_caller_same_key_is_a_separate_row(fx):
    with fx.tx() as ctx:
        a = record(ctx, caller="agent")
        b = record(ctx, caller="human:reviewer", h="h9", http_status=409, response={"error": {"code": "unbalanced"}})
    assert a.id != b.id
    assert fx.q("SELECT caller, intent_key, http_status FROM requests ORDER BY caller") == [("agent", "k1", 201), ("human:reviewer", "k1", 409)]
    with fx.reader() as conn:
        assert rq.check_idempotency(conn, fx.entity_id, caller="human:reviewer", intent_key="k1", request_hash="h9").response == {"error": {"code": "unbalanced"}}


def test_record_request_shape(fx):
    with fx.tx() as ctx:
        for kw in ({"caller": ""}, {"intent_key": None}, {"method": ""}, {"path": ""}, {"request_hash": ""}, {"http_status": 99}, {"http_status": "201"}):
            with pytest.raises(HaddonError) as ei:
                record(ctx, **kw)
            assert err(ei).code == "validation", kw
        with pytest.raises(HaddonError) as ei:  # operation must exist: pre-validated, so a 404 and no tainted transaction
            record(ctx, operation_id="op-ghost")
        assert (err(ei).code, err(ei).status) == ("not_found", 404)
        record(ctx)  # positive control: the transaction is still usable after the refusals
    assert fx.scalar("SELECT count(*) FROM requests") == 1


def test_request_linked_to_operation(fx):
    with fx.tx() as ctx:
        op = rq.create_operation(ctx, kind="import", intent={"file": "a.csv"})
        r = record(ctx, operation_id=op.id, http_status=202, response={"operation_id": op.id})
    assert r.operation_id == op.id


def test_execute_once_links_events_and_replays_for_any_transport(fx):
    calls = 0

    def work(ctx):
        nonlocal calls
        calls += 1
        append_event(ctx, kind="entry.posted", subject_kind="entry", subject_id="e1", after={"id": "e1"})
        return 201, {"id": "e1"}

    first = execution.execute_once(fx.engine, entity_id=fx.entity_id, caller="agent", actor=AGENT,
                                   intent_key="intent-1", method="COMMAND", path="post_entry",
                                   request_hash="hash-1", work=work)
    second = execution.execute_once(fx.engine, entity_id=fx.entity_id, caller="agent", actor=AGENT,
                                    intent_key="intent-1", method="COMMAND", path="post_entry",
                                    request_hash="hash-1", work=work)
    assert (first.replayed, second.replayed, calls) == (False, True, 1)
    assert first.response == second.response == {"id": "e1"}
    assert fx.q("SELECT requests.id, events.request_id FROM requests JOIN events ON events.request_id=requests.id") \
        == [(first.request_id, first.request_id)]


def test_execute_once_rolls_back_an_event_and_request_when_work_fails(fx):
    before = fx.counts()

    def work(ctx):
        append_event(ctx, kind="entry.posted", subject_kind="entry", subject_id="never-committed")
        raise HaddonError("validation", "no")

    with pytest.raises(HaddonError):
        execution.execute_once(fx.engine, entity_id=fx.entity_id, caller="agent", actor=AGENT,
                               intent_key="failed-intent", method="COMMAND", path="post_entry",
                               request_hash="hash-failed", work=work)
    assert fx.counts() == before


# operations
def test_operation_lifecycle_queued_running_succeeded(fx):
    with fx.tx() as ctx:
        op = rq.create_operation(ctx, kind="import", intent={"file": "a.csv"})
        assert (op.state, op.attempts, op.version, op.counts, op.errors, op.result, op.lease_until) == ("queued", 0, 1, {}, [], None, None)
        op = rq.transition_operation(ctx, op.id, expected_version=1, state="running", lease_until="2026-09-10T00:05:00Z")
        assert (op.state, op.attempts, op.version, op.lease_until) == ("running", 1, 2, "2026-09-10T00:05:00Z")
        op = rq.transition_operation(ctx, op.id, expected_version=2, state="running", counts={"rows": 10})  # heartbeat / progress
        assert (op.state, op.attempts, op.version, op.counts) == ("running", 1, 3, {"rows": 10})
        op = rq.transition_operation(ctx, op.id, expected_version=3, state="succeeded", counts={"rows": 20}, result={"entries": 3})
        assert (op.state, op.attempts, op.version, op.result, op.counts) == ("succeeded", 1, 4, {"entries": 3}, {"rows": 20})
    assert fx.q("SELECT state, attempts, version, result FROM operations") == [("succeeded", 1, 4, '{"entries": 3}')]
    with fx.reader() as conn:
        assert rq.get_operation(conn, fx.entity_id, op.id) == op
        assert rq.get_operation(conn, fx.entity_id, "ghost") is None


def test_attempts_increment_only_on_queued_to_running(fx):
    with fx.tx() as ctx:
        op = rq.create_operation(ctx, kind="import", intent={})
        op = rq.transition_operation(ctx, op.id, expected_version=1, state="running")
        assert op.attempts == 1
        op = rq.transition_operation(ctx, op.id, expected_version=2, state="running")
        op = rq.transition_operation(ctx, op.id, expected_version=3, state="running")
        assert op.attempts == 1
        op = rq.transition_operation(ctx, op.id, expected_version=4, state="failed", errors=[{"code": "boom"}])
        assert op.attempts == 1 and op.errors == [{"code": "boom"}]
        op2 = rq.create_operation(ctx, kind="import", intent={})
        op2 = rq.transition_operation(ctx, op2.id, expected_version=1, state="failed", errors=["never started"])
        assert op2.attempts == 0 and op2.state == "failed"


@pytest.mark.parametrize("terminal", ["succeeded", "failed"])
def test_terminal_operation_is_immutable(fx, terminal):
    with fx.tx() as ctx:
        op = rq.create_operation(ctx, kind="import", intent={})
        op = rq.transition_operation(ctx, op.id, expected_version=1, state="running")
        op = rq.transition_operation(ctx, op.id, expected_version=2, state=terminal)
        for state in ("running", "queued", "succeeded", "failed"):
            with pytest.raises(HaddonError) as ei:
                rq.transition_operation(ctx, op.id, expected_version=3, state=state)
            assert (err(ei).code, err(ei).status) == ("immutable", 409), state
    assert fx.q("SELECT state, version FROM operations") == [(terminal, 3)]
    # and the database agrees even for a raw UPDATE
    import sqlite3
    con = fx.raw()
    with pytest.raises(sqlite3.IntegrityError, match="operation_state"):
        con.execute("UPDATE operations SET state='running', version=4 WHERE id=?", (op.id,))
    con.close()


def test_illegal_transition_is_refused(fx):
    with fx.tx() as ctx:
        op = rq.create_operation(ctx, kind="import", intent={})
        with pytest.raises(HaddonError) as ei:
            rq.transition_operation(ctx, op.id, expected_version=1, state="succeeded")  # queued -> succeeded skips running
        assert (err(ei).code, err(ei).status) == ("immutable", 409) and err(ei).details[0] == {"id": op.id, "from": "queued", "to": "succeeded"}
        with pytest.raises(HaddonError) as ei:
            rq.transition_operation(ctx, op.id, expected_version=1, state="queued")
        assert err(ei).code == "immutable"
        with pytest.raises(HaddonError) as ei:
            rq.transition_operation(ctx, op.id, expected_version=1, state="paused")
        assert err(ei).code == "validation"
        with pytest.raises(HaddonError) as ei:
            rq.transition_operation(ctx, "ghost", expected_version=1, state="running")
        assert err(ei).status == 404
        op = rq.transition_operation(ctx, op.id, expected_version=1, state="running")
        with pytest.raises(HaddonError) as ei:
            rq.transition_operation(ctx, op.id, expected_version=2, state="queued")  # running -> queued
        assert err(ei).code == "immutable"
    assert fx.q("SELECT state, version, attempts FROM operations") == [("running", 2, 1)]


def test_stale_version_is_412(fx):
    with fx.tx() as ctx:
        op = rq.create_operation(ctx, kind="import", intent={})
        with pytest.raises(HaddonError) as ei:
            rq.transition_operation(ctx, op.id, expected_version=2, state="running")
        e = err(ei)
        assert (e.code, e.status) == ("stale_version", 412) and e.details[0]["current_version"] == 1
        assert rq.transition_operation(ctx, op.id, expected_version=1, state="running").version == 2
    with fx.tx() as ctx:
        with pytest.raises(HaddonError) as ei:
            rq.create_operation(ctx, kind="", intent={})
        assert err(ei).code == "validation"
