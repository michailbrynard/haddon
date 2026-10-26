"""``POST /close`` and ``POST /reopen``: the whole close workflow over HTTP, with no internal ids anywhere.

Closing is a command (issue 0005).  A date, a reason, the lock version and, where a check is blocking, an
override naming it are all that cross the wire.  The caller never manufactures a snapshot or decision id.

Four properties, each of which would be a real defect to lose:

* a blocked month is refused with 409 and told exactly what is outstanding, and the refusal leaves nothing
  behind: no snapshot, no close record, no moved lock, not even a burnt idempotency key;
* an override is a check name and a sentence, is stored on the close it let through, and is refused when it
  answers nothing;
* the same close retried under the same ``Idempotency-Key`` replays the first result rather than attempting a
  second close, while a different attempt at the same date fails on the lock version instead; and
* reopening works from a boundary or from the close being undone, and is not gated on the checks.
"""

from __future__ import annotations

import json
import uuid

import pytest

from test_api_documents import write_entry


def key() -> str:
    return str(uuid.uuid4())


def checks(api, through: str) -> dict:
    return api.json(f"/close-checks?through={through}")


def blocking_overrides(api, through: str, reason: str = "the bank has not sent this statement yet") -> list[dict]:
    return [{"check": g, "reason": reason} for g in checks(api, through)["blocking_checks"]]


def lock_version(api) -> int:
    for e in api.json("/v1/entities")["items"]:
        if e["entity_id"] == api.entity_id:
            return e["lock_version"]
    raise AssertionError("this entity is not in the listing")


@pytest.fixture
def booked(api_small) -> str:
    """One posted entry, so the continuous journal has a window a first close can cover."""
    return write_entry(api_small, dt="2026-03-10")


def test_a_blocked_month_is_refused_with_what_is_outstanding_and_writes_nothing(api_small, booked):
    before = api_small.scalar("SELECT max(seq) FROM events")
    r = api_small.post("/close", key=key(),
                       json={"through": "2026-03-31", "reason": "March close", "expected_version": 1})
    assert r.status_code == 409, r.text
    body = r.json()["error"]
    assert body["code"] == "close_not_eligible"
    assert {d["check"] for d in body["details"]} == set(checks(api_small, "2026-03-31")["blocking_checks"])
    for detail in body["details"]:
        assert detail["blocking"] is True and detail["requirement"] and detail["reason"]
    assert api_small.scalar("SELECT count(*) FROM close_records") == 0
    assert api_small.scalar("SELECT count(*) FROM snapshots") == 0
    assert api_small.scalar("SELECT through FROM period_locks") is None
    assert api_small.scalar("SELECT max(seq) FROM events") == before
    # A refused write does not burn the caller's intent key either.
    assert api_small.scalar("SELECT count(*) FROM requests WHERE path LIKE '%/close'") == 0


def test_a_close_needs_no_internal_ids_and_computes_its_own_snapshot(api_small, booked):
    overrides = blocking_overrides(api_small, "2026-03-31")
    r = api_small.post("/close", key=key(),
                       json={"through": "2026-03-31", "reason": "March closes on a recorded exception",
                             "expected_version": 1, "overrides": overrides})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "close" and body["lock"] == {"through": "2026-03-31", "version": 2,
                                                        "close_id": body["close_id"]}
    # The snapshot is the server's: it was never in the request, and it is taken through the close date.
    assert body["snapshot"]["through"] == "2026-03-31" and len(body["snapshot"]["state_hash"]) == 64
    assert body["checks"]["eligible"] is False and body["checks"]["may_close"] is True
    assert body["audit_seq"] > 0
    assert api_small.scalar("SELECT state_hash FROM snapshots WHERE id=?", (body["snapshot"]["id"],)) == \
        body["snapshot"]["state_hash"]
    # The overrides are on the close record, with the actor the credential resolves to.
    from conftest import API_ACTOR

    stored = json.loads(api_small.scalar("SELECT overrides FROM close_records WHERE id=?", (body["close_id"],)))
    assert {w["check"] for w in stored} == {o["check"] for o in overrides}
    assert {w["actor"] for w in stored} == {API_ACTOR}
    # One audit action, naming the same thing.
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='period.closed'") == 1


def test_an_override_that_answers_nothing_is_a_different_refusal_from_a_blocked_month(api_small, booked):
    overrides = blocking_overrides(api_small, "2026-03-31")
    r = api_small.post("/close", key=key(),
                       json={"through": "2026-03-31", "reason": "March close", "expected_version": 1,
                             "overrides": [*overrides, {"check": "nothing_in_draft", "reason": "pre-emptive"}]})
    assert r.status_code == 422, r.text
    detail = r.json()["error"]["details"][0]
    assert detail["reason"] == "override_not_blocking" and detail["checks"] == ["nothing_in_draft"]
    assert api_small.scalar("SELECT count(*) FROM close_records") == 0


def test_an_override_must_name_a_check_and_say_why(api_small, booked):
    for override, status in (({"check": "make_it_go", "reason": "please"}, 422),
                             ({"check": "statement_coverage", "reason": ""}, 422)):
        r = api_small.post("/close", key=key(),
                           json={"through": "2026-03-31", "reason": "March close", "expected_version": 1,
                                 "overrides": [override]})
        assert r.status_code == status, r.text
    assert api_small.scalar("SELECT count(*) FROM close_records") == 0


def test_closing_twice_under_one_key_replays_and_a_second_attempt_fails_on_the_version(api_small, booked):
    """The two retry stories, which are not the same story.

    A dropped connection is answered by the idempotency key: the same intent under the same key replays the
    original response and writes nothing new.  A second close of the same date is a different intent, and the
    lock version refuses it.  That is what stops two agents closing the same books from two readings of them.
    """
    body = {"through": "2026-03-31", "reason": "March close", "expected_version": 1,
            "overrides": blocking_overrides(api_small, "2026-03-31")}
    k = key()
    first = api_small.post("/close", key=k, json=body)
    assert first.status_code == 201, first.text
    replay = api_small.post("/close", key=k, json=body)
    assert replay.status_code == 201 and replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json() == first.json()
    assert api_small.scalar("SELECT count(*) FROM close_records") == 1

    stale = api_small.post("/close", key=key(), json=body)
    assert stale.status_code == 412 and stale.json()["error"]["code"] == "stale_version"
    assert api_small.scalar("SELECT count(*) FROM close_records") == 1


def test_reopen_takes_a_boundary_or_the_close_to_undo_and_is_not_gated_on_the_checks(api_small, booked):
    closed = api_small.post("/close", key=key(),
                            json={"through": "2026-03-31", "reason": "March close", "expected_version": 1,
                                  "overrides": blocking_overrides(api_small, "2026-03-31")}).json()
    # Nothing about the books got better, so the checks would still refuse a close.  A reopen works anyway.
    assert checks(api_small, "2026-04-30")["may_close"] is False
    reopened = api_small.post("/reopen", key=key(),
                              json={"through": "2026-02-28", "reason": "a March invoice arrived late",
                                    "expected_version": 2})
    assert reopened.status_code == 201, reopened.text
    assert reopened.json()["lock"] == {"through": "2026-02-28", "version": 3,
                                       "close_id": reopened.json()["close_id"]}
    # The close it reopened is untouched, and the reopen waives nothing.
    assert api_small.scalar("SELECT count(*) FROM close_records WHERE kind='close'") == 1
    assert api_small.scalar("SELECT overrides FROM close_records WHERE kind='reopen'") == "[]"
    assert api_small.scalar("SELECT count(*) FROM events WHERE kind='period.reopened'") == 1

    # The other way of naming it: undo that close, which resolves to the boundary it advanced from.
    again = api_small.post("/close", key=key(),
                           json={"through": "2026-03-31", "reason": "closed again", "expected_version": 3,
                                 "overrides": blocking_overrides(api_small, "2026-03-31")}).json()
    undo = api_small.post("/reopen", key=key(),
                          json={"close_id": again["close_id"], "reason": "undo it", "expected_version": 4})
    assert undo.status_code == 201, undo.text
    assert undo.json()["reopened_close_id"] == again["close_id"]
    assert undo.json()["lock"]["through"] == "2026-02-28"


def test_reopen_refuses_to_name_neither_target_or_both(api_small, booked):
    api_small.post("/close", key=key(),
                   json={"through": "2026-03-31", "reason": "March close", "expected_version": 1,
                         "overrides": blocking_overrides(api_small, "2026-03-31")})
    for body in ({"reason": "x", "expected_version": 2},
                 {"through": "2026-02-28", "close_id": "cr-anything", "reason": "x", "expected_version": 2}):
        r = api_small.post("/reopen", key=key(), json=body)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["details"][0]["reason"] == "target_required"


def test_close_and_reopen_are_published_and_require_an_idempotency_key(api_small):
    writes = api_small.json("/capabilities")["writes"]
    base = f"/v1/entities/{api_small.entity_id}"
    assert f"POST {base}/close" in writes and f"POST {base}/reopen" in writes
    assert "/proposals" not in " ".join(api_small.json("/capabilities")["reads"])
    r = api_small.post("/close", json={"through": "2026-03-31", "reason": "r", "expected_version": 1})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["reason"] == "idempotency_key_required"


# Reading a past close back to its snapshot (0005 #6)
def test_a_completed_close_is_readable_and_links_to_the_snapshot_it_was_taken_against(api_small, booked):
    """0005's sixth acceptance criterion: a completed close is linked to its immutable snapshot.

    The path has to work from a standing lock, not only from the 201 that set it.  From the lock it is two hops:
    the entity summary carries ``lock_close_id``, and the close record carries ``links.snapshot``.
    """
    waived = blocking_overrides(api_small, "2026-03-31")
    closed = api_small.post("/close", key=key(),
                            json={"through": "2026-03-31", "reason": "March close", "expected_version": 1,
                                  "overrides": waived}).json()
    base = f"/v1/entities/{api_small.entity_id}"

    summary = next(e for e in api_small.json("/v1/entities")["items"] if e["entity_id"] == api_small.entity_id)
    assert summary["lock_close_id"] == closed["close_id"]

    record = api_small.json(f"/closes/{summary['lock_close_id']}")
    assert record["id"] == closed["close_id"] and record["kind"] == "close"
    assert (record["through"], record["prior_through"]) == ("2026-03-31", None)
    assert record["snapshot_id"] == closed["snapshot"]["id"]
    assert record["reason"] == "March close" and record["created_by"]
    # The overrides this close was let through on come back with it, not just their count.
    assert {w["check"] for w in record["overrides"]} == {o["check"] for o in waived}
    assert all(w["reason"] == o["reason"] for w, o in zip(sorted(record["overrides"], key=lambda w: w["check"]),
                                                         sorted(waived, key=lambda o: o["check"])))
    assert record["links"]["self"] == f"{base}/closes/{closed['close_id']}"
    assert record["links"]["snapshot"] == f"{base}/snapshots/{closed['snapshot']['id']}"
    assert closed["links"]["self"] == record["links"]["self"]

    # The link resolves to the snapshot the close actually hashed, not merely to a snapshot.
    snapshot = api_small.json(f"/snapshots/{record['snapshot_id']}")
    assert snapshot["state_hash"] == closed["snapshot"]["state_hash"] and snapshot["through"] == "2026-03-31"


def test_a_reopen_reads_back_the_same_way_and_the_close_it_undid_still_does(api_small, booked):
    """Nothing is undone by a reopen, so both records stay readable and both still name their own snapshot."""
    closed = api_small.post("/close", key=key(),
                            json={"through": "2026-03-31", "reason": "March close", "expected_version": 1,
                                  "overrides": blocking_overrides(api_small, "2026-03-31")}).json()
    # Reopen to a boundary, not by naming the close: undoing the first close would clear the lock, which is
    # refused.
    reopened = api_small.post("/reopen", key=key(),
                              json={"through": "2026-02-28", "reason": "an invoice arrived late",
                                    "expected_version": 2}).json()
    still_there = api_small.json(f"/closes/{closed['close_id']}")
    assert still_there["snapshot_id"] == closed["snapshot"]["id"] and still_there["kind"] == "close"
    undo = api_small.json(f"/closes/{reopened['close_id']}")
    assert undo["kind"] == "reopen" and undo["overrides"] == [] and undo["prior_through"] == "2026-03-31"
    assert undo["snapshot_id"] == reopened["snapshot"]["id"]


def test_the_close_reader_is_published_read_only_and_scoped_to_its_own_entity(api_small, booked, api_two_entities):
    """A read route earns nothing by existing unless it is on the published surface and cannot be written to."""
    base = f"/v1/entities/{api_small.entity_id}"
    assert f"{base}/closes/{{close_id}}" in api_small.json("/capabilities")["reads"]
    assert not [w for w in api_small.json("/capabilities")["writes"] if "/closes/" in w]

    before = api_small.scalar("SELECT max(seq) FROM events")
    closed = api_small.post("/close", key=key(),
                            json={"through": "2026-03-31", "reason": "March close", "expected_version": 1,
                                  "overrides": blocking_overrides(api_small, "2026-03-31")}).json()
    after_close = api_small.scalar("SELECT max(seq) FROM events")
    api_small.json(f"/closes/{closed['close_id']}")
    assert api_small.scalar("SELECT max(seq) FROM events") == after_close != before   # reading wrote nothing
    assert api_small.post(f"/closes/{closed['close_id']}", key=key(), json={}).status_code == 405

    assert api_small.get("/closes/cr-does-not-exist").status_code == 404
    # An id belonging to another entity is not-found here, never a leak.
    inc, _bv = api_two_entities
    assert inc.get(f"/closes/{closed['close_id']}").status_code == 404
