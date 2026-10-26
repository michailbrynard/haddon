"""Period lock, close records and verification snapshots.

The lock row is provisioned once.  ``close`` appends a ``close_records`` row and moves ``through`` forward.
``reopen`` appends a reasoned record and moves it back, but never to "no lock" (U7).  The ``locks_update`` trigger
re-derives the same rule from the close record, so this module and the trigger must agree.

``haddon.repository.close_checks`` owns what "complete" means and what the refusal says.  This module asks it last,
after the lock version, "close must advance" and the snapshot have all held.  ``reopen`` is deliberately not gated
on eligibility.  Reopening is how an incomplete month gets fixed, and requiring completeness to reopen would trap
the books shut (issue 0005, pitfall 1).

The snapshot is computed here and never accepted from a caller (issue 0005, pitfall 2).  ``verification_snapshot``
reads the posted journal through the close date, checks that it balances per commodity, and hashes the result.  A
caller that could hand in a state hash could hand in the hash of a state that never existed, and one that could
hand in an eligibility verdict could hand in a pass.  Neither is representable.

This is not the exporter's snapshot.  That one renders hledger journals and runs them through the pinned binary.
It is an artifact extension (``docs/architecture.md``), which the journal core may not import.  The two share the
``snapshots`` table and its "one state is one snapshot" rule (``state_hash`` is UNIQUE), which is why both look the
row up by hash before writing one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.canonical import canonical_json, sha256_hex
from haddon.domain.dates import parse_date
from haddon.domain.errors import IMMUTABLE, INTERNAL_ERROR, STALE_VERSION, HaddonError, not_found, validation
from haddon.repository import tables as t
from haddon.repository.close_checks import CloseChecks, Override, checks_for_audit, require_eligible
from haddon.repository.ctx import WriteCtx
from haddon.repository.events import append_event

#: Bumped when the hashed body of a verification snapshot changes shape, so two snapshots are never compared
#: across a change in what "the state" means.
SNAPSHOT_SCHEMA = "haddon.close-snapshot/1"


@dataclass(frozen=True)
class Lock:
    id: str
    through: str | None
    version: int
    close_id: str | None


@dataclass(frozen=True)
class Snapshot:
    id: str
    snapshot_seq: int
    state_hash: str
    through: str


def get_lock(conn: Connection, entity_id: str) -> Lock:
    r = conn.execute(sa.select(t.period_locks).where(t.period_locks.c.entity_id == entity_id)).first()
    if r is None:
        raise HaddonError(INTERNAL_ERROR, "period lock row missing: entity not provisioned")
    return Lock(r.id, r.through, r.version, r.close_id)


def create_snapshot(ctx: WriteCtx, *, through: str, state_hash: str, manifest: Any, report: Any, checks: Any,
                    id: str | None = None) -> Snapshot:
    parse_date(through, path="through")
    if not isinstance(state_hash, str) or not state_hash:
        raise validation("state_hash is required", path="state_hash", reason="required")
    seq = ctx.conn.execute(sa.select(sa.func.coalesce(sa.func.max(t.snapshots.c.snapshot_seq), 0))
                           .where(t.snapshots.c.entity_id == ctx.entity_id)).scalar_one() + 1
    sid = ctx.insert(t.snapshots, id, "snap", snapshot_seq=seq, state_hash=state_hash, through=through,
                     manifest=json.dumps(manifest, sort_keys=True), report=json.dumps(report, sort_keys=True),
                     checks=json.dumps(checks, sort_keys=True))
    return Snapshot(sid, seq, state_hash, through)


def get_snapshot(conn: Connection, entity_id: str, snapshot_id: str) -> Snapshot | None:
    r = conn.execute(sa.select(t.snapshots).where(t.snapshots.c.entity_id == entity_id, t.snapshots.c.id == snapshot_id)).first()
    return None if r is None else Snapshot(r.id, r.snapshot_seq, r.state_hash, r.through)


def snapshot_by_state_hash(conn: Connection, entity_id: str, state_hash: str) -> Snapshot | None:
    """Return the snapshot already taken of this state, if any.

    ``snapshots.state_hash`` is UNIQUE, so one state is one snapshot.  A caller that re-derives the same hash, such
    as a repeated export of an unchanged book, reuses the row instead of asking the database to refuse a duplicate."""
    r = conn.execute(sa.select(t.snapshots).where(t.snapshots.c.entity_id == entity_id,
                                                  t.snapshots.c.state_hash == state_hash)).first()
    return None if r is None else Snapshot(r.id, r.snapshot_seq, r.state_hash, r.through)


def verification_snapshot(ctx: WriteCtx, *, through: str, id: str | None = None) -> Snapshot:
    """Compute, verify and store the snapshot a close is recorded against.

    The hashed body is the posted journal as at ``through``: every (account, commodity) balance, with the chart
    and commodity counts beside it.  Two identical books produce the same hash and a changed book cannot, which
    is what makes the hash on a close record worth anything.

    The verification is per commodity (issue 0002): each commodity's balances must net to zero across the whole
    chart.  They always should, because every entry is refused unless it balances.  That is why the check is worth
    making.  If it ever fails, the invariant was lost somewhere the entry path cannot see, and a close is the last
    moment to notice.
    """
    from haddon.reports.balances import trial_balance  # local: reports imports repository

    parse_date(through, path="through")
    lines = trial_balance(ctx.conn, ctx.entity_id, through=through, include_zero=False).lines
    balances = [{"account_id": ln.account_id, "path": ln.path, "type": ln.type, "commodity": ln.commodity,
                 "amount_micros": str(int(ln.amount))} for ln in lines]
    totals: dict[str, int] = {}
    for ln in lines:
        totals[ln.commodity] = totals.get(ln.commodity, 0) + int(ln.amount)
    unbalanced = {c: v for c, v in totals.items() if v != 0}
    if unbalanced:
        raise HaddonError(INTERNAL_ERROR,
                          "the journal does not balance per commodity through this date, so no verified snapshot "
                          "can be taken",
                          details=[{"path": "through", "through": through, "commodity": c,
                                    "difference_micros": str(v)} for c, v in sorted(unbalanced.items())])
    counts = {
        "accounts": len({ln.account_id for ln in lines}),
        "commodities": len(totals),
        "balance_lines": len(lines),
        "entries": int(ctx.conn.execute(sa.select(sa.func.count()).select_from(t.entries)
                                        .where(t.entries.c.entity_id == ctx.entity_id,
                                               t.entries.c.status == "posted",
                                               t.entries.c.dt <= through)).scalar_one()),
        "postings": int(ctx.conn.execute(
            sa.select(sa.func.count()).select_from(t.postings.join(
                t.entries, sa.and_(t.entries.c.entity_id == t.postings.c.entity_id,
                                   t.entries.c.id == t.postings.c.entry_id)))
            .where(t.postings.c.entity_id == ctx.entity_id, t.entries.c.status == "posted",
                   t.entries.c.dt <= through)).scalar_one()),
    }
    manifest = {"schema": SNAPSHOT_SCHEMA, "entity_id": ctx.entity_id, "through": through,
                "balances": balances, "counts": counts}
    digest = sha256_hex(canonical_json(manifest))
    report = {"per_commodity_totals": {c: str(v) for c, v in sorted(totals.items())}, "counts": counts}
    verification = {"kind": "per_commodity_equilibrium", "result": "pass",
                    "commodities": sorted(totals), "through": through}
    existing = snapshot_by_state_hash(ctx.conn, ctx.entity_id, digest)
    if existing is not None:
        # One state is one snapshot (``state_hash`` is UNIQUE).  Closing through a date, reopening, and closing
        # through it again on an unchanged book re-derives the same hash, so reuse the row.  The ``through`` guard
        # stops an unrelated state colliding into it unnoticed.
        if existing.through != through:
            raise HaddonError(INTERNAL_ERROR,
                              f"snapshot {existing.id!r} already carries this state hash through "
                              f"{existing.through}, not through {through}",
                              details=[{"path": "through", "snapshot_id": existing.id, "through": through,
                                        "snapshot_through": existing.through}])
        return existing
    return create_snapshot(ctx, through=through, state_hash=digest, manifest=manifest, report=report,
                           checks=verification, id=id)


def close(ctx: WriteCtx, *, through: str, reason: str, expected_version: int,
          overrides: Sequence[Override] = (), id: str | None = None) -> tuple[Lock, CloseChecks, str, Snapshot]:
    """Advance the lock to ``through``.  Returns the new lock, the checks, the close id and the snapshot.

    Each of ``overrides`` is a check name and a reason in the caller's own words (``close_checks.Override``), and
    must answer a check that is blocking this evaluation.  Without one, a period that is not complete is refused
    with 409 ``close_not_eligible``, naming every blocking check and what is outstanding in it.

    The order is deliberate and pinned by a test.  The cheap refusals come first (the lock version, "close must
    advance", a missing reason).  Then the snapshot is computed.  Eligibility is evaluated last, so an incomplete
    month is only reported once everything else has held.  Nothing here is durable until the whole transaction
    commits, so a refused close leaves no snapshot and no record.
    """
    parse_date(through, path="through")
    lock = get_lock(ctx.conn, ctx.entity_id)
    if lock.version != expected_version:
        raise HaddonError(STALE_VERSION, "lock changed", details=[{"current_version": lock.version}])
    if lock.through is not None and through <= lock.through:
        raise validation(f"close must advance the lock beyond {lock.through}", path="through", reason="not_advancing",
                         current_through=lock.through, through=through)
    if not isinstance(reason, str) or not reason.strip():
        raise validation("a close needs a reason", path="reason", reason="required")
    snap = verification_snapshot(ctx, through=through)
    # The precondition, applied last so an incomplete month is only reported once everything else holds.
    checks = require_eligible(ctx.conn, ctx.entity_id, through=through, overrides=overrides, actor=ctx.actor)
    cid = ctx.insert(t.close_records, id, "close", through=through, prior_through=lock.through, snapshot_id=snap.id,
                     kind="close", reason=reason, overrides=json.dumps(_waivers(checks), sort_keys=True))
    # The close record says a month was closed.  This event says on what evidence.  Any override that made the
    # close possible is named both here and on the record itself.
    append_event(ctx, kind="close.eligibility", subject_kind="close_record", subject_id=cid,
                 after=checks_for_audit(checks), reason=reason)
    return _move(ctx, lock, through, cid), checks, cid, snap


def reopen(ctx: WriteCtx, *, through: str, reason: str, expected_version: int,
           id: str | None = None) -> tuple[Lock, str, Snapshot]:
    """Move the lock back.  There is no eligibility check, by design.

    Reopening an incomplete month is how it gets completed, so gating it on completeness would leave a wrongly
    closed month shut for good.

    Nothing is undone.  The close record that shut the period, and the snapshot it was taken against, stay where
    they are and stay readable.  This appends a second record saying the lock moved back and why.
    """
    parse_date(through, path="through")
    lock = get_lock(ctx.conn, ctx.entity_id)
    if lock.version != expected_version:
        raise HaddonError(STALE_VERSION, "lock changed", details=[{"current_version": lock.version}])
    if lock.through is None:
        raise HaddonError(IMMUTABLE, "nothing is closed", details=[{"reason": "no_lock"}])
    if through >= lock.through:
        raise validation(f"reopen must move the lock before {lock.through}", path="through", reason="not_reopening")
    if not isinstance(reason, str) or not reason.strip():
        raise validation("a reopen needs a reason", path="reason", reason="required")
    snap = verification_snapshot(ctx, through=through)
    cid = ctx.insert(t.close_records, id, "reopen", through=through, prior_through=lock.through, snapshot_id=snap.id,
                     kind="reopen", reason=reason, overrides="[]")
    return _move(ctx, lock, through, cid), cid, snap


def close_record(conn: Connection, entity_id: str, close_id: str):
    r = conn.execute(sa.select(t.close_records).where(t.close_records.c.entity_id == entity_id,
                                                      t.close_records.c.id == close_id)).first()
    return r


def _waivers(checks: CloseChecks) -> list[dict[str, Any]]:
    """The overrides this close actually used, as they will be stored on the close record.

    They live on the record rather than in a table of their own.  An override has no life apart from the close it
    let through, and a row that outlived its close would be a standing permission, which is what the fail-closed
    rule exists to prevent.
    """
    return [g.waiver.as_json() for g in checks.checks if g.waiver is not None]


def _move(ctx: WriteCtx, lock: Lock, through: str, close_id: str) -> Lock:
    res = ctx.execute(sa.update(t.period_locks).where(t.period_locks.c.id == lock.id, t.period_locks.c.version == lock.version)
                      .values(through=through, version=lock.version + 1, close_id=close_id))
    if res.rowcount != 1:
        raise HaddonError(STALE_VERSION, "lock changed during the transaction", details=[{"id": lock.id}])
    return get_lock(ctx.conn, ctx.entity_id)
