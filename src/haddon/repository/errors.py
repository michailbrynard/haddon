"""Error-code mapping for database refusals (review D6).

SQLite fires BEFORE triggers most-recently-created first.  When two triggers would both refuse a statement, the
message a caller sees therefore depends on creation order: a bad-quantum posting into a locked draft says
`parent_or_lock`, not `precision`.  The decision (docs/design/decisions.md D6) is that the repository pre-validates
each write in a fixed order so the API error code is deterministic.  The trigger text below is only the fallback
mapping, for anything that reaches the database unvalidated.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy.exc import DBAPIError

# Trigger RAISE text -> (HTTP status, API error code), from the spec's 409/422 table.
TRIGGER_CODES: dict[str, tuple[int, str]] = {
    "immutable": (409, "immutable"),
    "append_only": (409, "immutable"),
    "retain_entry": (409, "immutable"),
    "retain_operation": (409, "immutable"),
    "retain_book": (409, "immutable"),
    "retain_lock": (409, "immutable"),
    "archive_account": (409, "immutable"),
    "referenced_chart_immutable": (409, "immutable"),
    "chart_identity": (409, "immutable"),
    "book_identity": (409, "immutable"),
    "exists": (409, "immutable"),
    "period_locked": (409, "period_locked"),
    "old_period_locked": (409, "period_locked"),
    "parent_or_lock": (409, "period_locked"),
    "old_parent_or_lock": (409, "period_locked"),
    "new_parent_or_lock": (409, "period_locked"),
    "period_overlap": (409, "period_overlap"),
    "document_already_loaded": (409, "document_already_loaded"),
    "assertion_overlap": (409, "period_overlap"),
    "version_conflict": (412, "stale_version"),
    "transition": (409, "immutable"),
    "draft_required": (422, "validation"),
    "two_legs_required": (422, "unbalanced"),
    "unbalanced": (422, "unbalanced"),
    "account_invalid": (422, "account_unknown"),
    "precision": (422, "precision"),
    "allocation_invalid": (422, "allocation_invalid"),
    "overallocated": (422, "overallocated"),
    "explanation_not_live": (409, "immutable"),
    "already_explained": (409, "immutable"),
    "not_live": (409, "already_superseded"),
    "statement_replaced": (409, "immutable"),
    "lines_still_booked": (409, "immutable"),
    "outside_period": (422, "validation"),
    "supersedes_subject": (422, "validation"),
    "replaces_subject": (422, "validation"),
    "correction_invalid": (422, "validation"),
    "replacement_invalid": (422, "validation"),
    "settlement_invalid": (422, "validation"),
    "lock_invalid": (409, "immutable"),
    "close_record_required": (409, "approval_required"),
    "operation_state": (409, "immutable"),
}


def classify(exc: BaseException) -> tuple[int, str]:
    """Fallback classification of a database error into (http status, code)."""
    orig = exc.orig if isinstance(exc, DBAPIError) else exc
    text = str(orig)
    if text in TRIGGER_CODES:
        return TRIGGER_CODES[text]
    if isinstance(orig, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return 503, "busy"
    if isinstance(orig, (sqlite3.IntegrityError, OverflowError)):
        if "overflow" in text.lower() or isinstance(orig, OverflowError):
            return 422, "overflow"
        return 422, "validation"
    return 500, "internal_error"


def wrap_db_error(exc: BaseException):
    """Convert a database refusal into the spec's ``HaddonError`` (fallback path of D6)."""
    from haddon.domain.errors import HaddonError  # local import: domain must not depend on the repository layer

    status, code = classify(exc)
    orig = exc.orig if isinstance(exc, DBAPIError) else exc
    return HaddonError(code, f"database refused the write: {orig}", details=[{"db_error": str(orig)}],
                       retryable=(code == "busy"), status=status)
