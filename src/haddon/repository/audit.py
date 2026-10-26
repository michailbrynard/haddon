"""The audit action: the durable record that a business command ran, who ran it and why.

Haddon has no approval precondition (see ``docs/design/decisions.md`` § Authorisation), so the audit action is the
only record of authority the product keeps.  That is why it has a fixed shape rather than being a free-form event.

It is a thin wrapper over the append-only event log (``repository/events.py``).  The log already enforces actor,
time, kind and subject.  This module fixes the payload convention, so every command's history reads the same way and
a reviewer can see who did what, when and why without following a chain of approval records.

One command writes exactly one audit action, in the same transaction as its domain rows.  A failed domain write
therefore cannot leave an audit action claiming success, and a successful one cannot be silent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from haddon.domain.canonical import canonical_json, sha256_hex
from haddon.domain.errors import validation
from haddon.repository.ctx import WriteCtx
from haddon.repository.events import append_event


@dataclass(frozen=True)
class AuditAction:
    """A recorded business action.  ``seq`` is the event log's high-water mark for it."""

    seq: int
    kind: str
    subject_kind: str
    subject_id: str
    reason: str
    input_hash: str
    result: dict[str, Any]
    actor: str
    at: str

    def as_json(self) -> dict[str, Any]:
        return {"audit_seq": self.seq, "kind": self.kind, "subject_kind": self.subject_kind,
                "subject_id": self.subject_id, "reason": self.reason, "input_hash": self.input_hash,
                "result": dict(self.result), "actor": self.actor, "at": self.at}


def record_action(ctx: WriteCtx, *, action: str, subject_kind: str, subject_id: str, reason: str,
                  result: dict[str, Any], input: Any = None, request_id: str | None = None,
                  before: Any = None, evidence_refs: Iterable[str] = ()) -> AuditAction:
    """Append one audit action for a command, in the caller's transaction.

    ``input`` is the command's canonical input.  Only its hash is stored, so a large body never bloats the log, yet
    the hash still shows if reviewed content changed.  The ids a reader follows live in ``result``.  ``request_id``
    links the action to the idempotency record, which answers a different question: was this a retry?
    """
    if not isinstance(action, str) or not action.strip():
        raise validation("an audit action names its kind", path="action", reason="required")
    if not isinstance(reason, str) or not reason.strip():
        raise validation("an audit action records why", path="reason", reason="required")
    input_hash = sha256_hex(canonical_json(input)) if input is not None else None
    after = {"action": action, "result": result, "input_hash": input_hash,
             "evidence_refs": list(evidence_refs)}
    seq = append_event(ctx, kind=action, subject_kind=subject_kind, subject_id=subject_id, before=before,
                       after=after, reason=reason, request_id=request_id)
    return AuditAction(seq=seq, kind=action, subject_kind=subject_kind, subject_id=subject_id, reason=reason,
                       input_hash=input_hash or "", result=result, actor=ctx.actor, at=ctx.now)
