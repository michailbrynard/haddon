"""Reversals (spec "For reversals, create all mirror/replacement drafts first, seal them together, and insert an
immutable correction relation").

Mirror legs are the exact negatives of the original's legs, with the same account, commodity, memo and tax code.
An original gets one mirror.  A second correction must target the replacement instead.  The whole thing runs in one
``write_transaction``, so a failure anywhere, even after the mirror is sealed, leaves nothing behind.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.dates import parse_date
from haddon.domain.errors import IMMUTABLE, INTERNAL_ERROR, HaddonError, not_found, validation
from haddon.repository import tables as t
from haddon.repository.ctx import WriteCtx
from haddon.repository.entries import Entry, Leg, create_draft, get_entry, leg_multiset, seal, validate_header, validate_legs


@dataclass(frozen=True)
class Replacement:
    dt: str
    narration: str
    origin: str
    legs: list[Leg]
    code: str = ""      # hledger tcode.  Carry the original's forward unless the caller says otherwise.
    comment: str = ""
    tags: tuple = ()


@dataclass(frozen=True)
class Correction:
    id: str
    original_id: str
    mirror_id: str
    replacement_id: str | None


def mirror_legs(original: Entry) -> list[Leg]:
    return [Leg(p.account_id, p.commodity, -p.amount, p.memo, p.tax_code) for p in original.legs]


def correction_for(conn: Connection, entity_id: str, original_id: str) -> Correction | None:
    r = conn.execute(sa.select(t.corrections).where(t.corrections.c.entity_id == entity_id,
                                                    t.corrections.c.original_id == original_id)).first()
    return None if r is None else Correction(r.id, r.original_id, r.mirror_id, r.replacement_id)


def standing_entry(conn: Connection, entity_id: str, entry_id: str) -> str | None:
    """Follow ``entry_id``'s corrections to the entry that stands in its place today.

    An entry nobody corrected stands for itself.  A corrected one is replaced by its replacement, which may have
    been corrected in turn, so this walks the chain to its end.  ``None`` means the chain ended in a plain
    reversal: nothing stands in the original's place any more.
    """
    seen: set[str] = set()
    current: str | None = entry_id
    while current is not None and current not in seen:
        seen.add(current)
        correction = correction_for(conn, entity_id, current)
        if correction is None:
            return current
        current = correction.replacement_id
    return current


def replaced_chain(conn: Connection, entity_id: str, entry_id: str) -> list[str]:
    """``entry_id`` and every entry it replaced, newest first: the chain ``standing_entry`` walks, read backwards."""
    out = [entry_id]
    while True:
        prior = conn.execute(sa.select(t.corrections.c.original_id)
                             .where(t.corrections.c.entity_id == entity_id,
                                    t.corrections.c.replacement_id == out[-1])).scalar()
        if prior is None or prior in out:
            return out
        out.append(prior)


#: What else must move when an entry is corrected through ``POST /entries/{id}/correct``.  The core does not know
#: what a statement line is, so the reconciliation extension registers here (``lines.follow_correction``) and the
#: dependency keeps pointing from the extension to the core.  The hook returns whatever it wrote, for the audit
#: result.
_CORRECTION_FOLLOWER: Callable[[WriteCtx, "Correction", str], list[dict]] | None = None


def register_correction_follower(follower) -> None:
    """Install the extension hook run inside a correction command, after the correction is written."""
    global _CORRECTION_FOLLOWER
    _CORRECTION_FOLLOWER = follower


def follow_correction(ctx: WriteCtx, correction: "Correction", reason: str) -> list[dict]:
    return [] if _CORRECTION_FOLLOWER is None else _CORRECTION_FOLLOWER(ctx, correction, reason)


def reverse_entry(ctx: WriteCtx, *, original_id: str, dt: str, reason: str,
                  narration: str | None = None, replacement: Replacement | None = None, id: str | None = None,
                  mirror_id: str | None = None, replacement_id: str | None = None) -> Correction:
    """Reverse a posted entry, optionally replacing it.  ``reason`` is required for the caller's audit action.

    There is no authorising decision to pass.  The record of authority is the enclosing command's append-only audit
    action (issue 0003, completed for the last holder in 0005).  This function only changes the correction graph.
    The command records exactly one audit action once all of its writes succeed.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise validation("a correction records why somebody disagreed, not only what changed", path="reason",
                         reason="required")
    original = get_entry(ctx.conn, ctx.entity_id, original_id)
    if original is None:
        raise not_found("entry", original_id)
    if original.status != "posted":
        raise validation(f"only a posted entry can be reversed; {original_id!r} is {original.status}", path="original_id",
                         reason="original_not_posted", status=original.status)
    prior = correction_for(ctx.conn, ctx.entity_id, original_id)
    if prior is not None:
        raise HaddonError(IMMUTABLE, f"entry {original_id!r} already has a mirror; correct its replacement instead",
                          details=[{"original_id": original_id, "mirror_id": prior.mirror_id, "replacement_id": prior.replacement_id}])
    parse_date(dt)
    if narration is None:
        narration = f"Reversal of {original_id}: {original.narration}"
    if replacement is not None:
        # Validate the replacement before any statement is issued, as every other write does.  A malformed
        # replacement must not leave a mirror draft behind for a caller that handles the 422 inside its transaction.
        if not isinstance(replacement, Replacement):
            raise validation("replacement must be a Replacement", path="replacement", reason="shape")
        validate_header(ctx, replacement.dt, replacement.narration, replacement.origin, allow_reserved=False,
                        code=replacement.code, comment=replacement.comment, tags=replacement.tags)
        validate_legs(ctx, replacement.legs)
    # 1. All drafts first.
    mirror = create_draft(ctx, dt=dt, narration=narration, origin="reversal", legs=mirror_legs(original), id=mirror_id,
                          _allow_reserved_origin=True)
    repl = None
    if replacement is not None:
        repl = create_draft(ctx, dt=replacement.dt, narration=replacement.narration, origin=replacement.origin,
                            legs=replacement.legs, id=replacement_id, code=replacement.code,
                            comment=replacement.comment, tags=replacement.tags)
    # 2. Seal them together.
    mirror = seal(ctx, mirror.id, expected_version=mirror.version)
    if repl is not None:
        repl = seal(ctx, repl.id, expected_version=repl.version)
    # 3. Repository gate: the mirror's canonical leg multiset must equal the negated original's.
    if leg_multiset(mirror) != negated_multiset(original):
        raise HaddonError(INTERNAL_ERROR, "mirror legs are not the exact negative of the original",
                          details=[{"original_id": original_id, "mirror_id": mirror.id}])
    # 4. The immutable correction relation.
    cid = ctx.insert(t.corrections, id, "cor", original_id=original_id, mirror_id=mirror.id,
                     replacement_id=repl.id if repl else None)
    return Correction(cid, original_id, mirror.id, repl.id if repl else None)


def negated_multiset(e: Entry) -> Counter:
    return Counter((p.account_id, p.commodity, -p.amount.value, p.memo, p.tax_code) for p in e.legs)

