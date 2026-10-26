"""The immutable ``exports`` record: what was rendered, from which snapshot, and what the verifier said.

An export row is written after its artifacts are on disk and have passed ``hledger check --strict``.  The row is
the durable claim that those exact bytes existed and verified.  Triggers ``exports_u`` and ``exports_d`` refuse any
update or delete, so the writer pre-validates its references (snapshot, operation, digest shapes) here rather than
letting a foreign key taint the transaction (D6).

``artifacts`` is the per-file list (``path``, ``sha256``, ``bytes``), byte-ordered by path.  ``verifier`` is the
pinned binary's identity and verdict.  This table stores only the manifest's sha256.  The manifest itself is kept
whole on the snapshot (``snapshots.manifest``) and in the ``export.completed`` event.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from haddon.domain.canonical import canonical_json
from haddon.domain.errors import not_found, validation
from haddon.repository import tables as t
from haddon.repository.closing import get_snapshot
from haddon.repository.ctx import WriteCtx
from haddon.repository.requests import get_operation

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Export:
    id: str
    snapshot_id: str
    operation_id: str
    manifest_sha256: str
    artifacts: Any
    verifier: Any


def _artifact_ok(item: Any, i: int) -> None:
    if not isinstance(item, dict):
        raise validation(f"artifacts[{i}] must be an object", path=f"artifacts[{i}]", reason="object_required")
    for key, typ in (("path", str), ("sha256", str), ("bytes", int)):
        if key not in item or isinstance(item[key], bool) or not isinstance(item[key], typ):
            raise validation(f"artifacts[{i}].{key} is required", path=f"artifacts[{i}].{key}", reason="required")
    if not _SHA256.match(item["sha256"]):
        raise validation(f"artifacts[{i}].sha256 is not a sha256 hex digest", path=f"artifacts[{i}].sha256", reason="digest_shape")
    if item["bytes"] < 0:
        raise validation(f"artifacts[{i}].bytes is negative", path=f"artifacts[{i}].bytes", reason="range")


def record_export(ctx: WriteCtx, *, snapshot_id: str, operation_id: str, manifest_sha256: str, artifacts: list,
                  verifier: dict, id: str | None = None) -> Export:
    """Insert the export row.

    References and digest shapes are checked first, so an unknown snapshot is a clean 404 and a malformed digest a
    422, never a tainting constraint failure."""
    if not isinstance(manifest_sha256, str) or not _SHA256.match(manifest_sha256):
        raise validation("manifest_sha256 must be a sha256 hex digest", path="manifest_sha256", reason="digest_shape")
    if not isinstance(artifacts, list) or not artifacts:
        raise validation("an export records at least one artifact", path="artifacts", reason="required")
    for i, item in enumerate(artifacts):
        _artifact_ok(item, i)
    if not isinstance(verifier, dict) or not verifier:
        raise validation("verifier is required", path="verifier", reason="required")
    if get_snapshot(ctx.conn, ctx.entity_id, snapshot_id) is None:
        raise not_found("snapshot", snapshot_id)
    if get_operation(ctx.conn, ctx.entity_id, operation_id) is None:
        raise not_found("operation", operation_id)
    eid = ctx.insert(t.exports, id, "exp", snapshot_id=snapshot_id, operation_id=operation_id,
                     manifest_sha256=manifest_sha256, artifacts=canonical_json(artifacts), verifier=canonical_json(verifier))
    return get_export(ctx.conn, ctx.entity_id, eid)  # type: ignore[return-value]


def get_export(conn: Connection, entity_id: str, export_id: str) -> Export | None:
    r = conn.execute(sa.select(t.exports).where(t.exports.c.entity_id == entity_id, t.exports.c.id == export_id)).first()
    if r is None:
        return None
    return Export(r.id, r.snapshot_id, r.operation_id, r.manifest_sha256, json.loads(r.artifacts), json.loads(r.verifier))
