"""The error contract of the spec's "Error and credential contracts" table, as one exception type.

Repositories raise ``HaddonError`` with the spec's HTTP status and code; ``haddon.api`` renders the envelope.
Nothing here imports the database: trigger-text fallbacks are mapped in ``haddon.repository.errors``.
"""

from __future__ import annotations

from typing import Any

# 409
APPROVAL_REQUIRED = "approval_required"
IMMUTABLE = "immutable"
PERIOD_LOCKED = "period_locked"
PERIOD_OVERLAP = "period_overlap"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
ALREADY_SUPERSEDED = "already_superseded"
CLOSE_NOT_ELIGIBLE = "close_not_eligible"
#: rule 4: one document is one statement.  ``documents.sha256`` is UNIQUE, so a second submission of the same
#: bytes is refused by content rather than by filename.  A renamed re-download is the same statement.
DOCUMENT_ALREADY_LOADED = "document_already_loaded"
#: issue 0007: the document row is here but its bytes cannot be served.  They may be held elsewhere, kept outside the
#: configured evidence root, missing from it, or not hash to what was recorded.  That is one code because it is one
#: answer to a reviewer ("this content is not available here"); ``details[0].reason`` says which, and no variant of
#: it ever carries a filesystem path.
EVIDENCE_UNAVAILABLE = "evidence_unavailable"
CURSOR_EXPIRED = "cursor_expired"
EVENT_GAP = "event_gap"
# 412
STALE_VERSION = "stale_version"
# 422
VALIDATION = "validation"
UNBALANCED = "unbalanced"
ACCOUNT_UNKNOWN = "account_unknown"
ACCOUNT_ARCHIVED = "account_archived"
PRECISION = "precision"
OVERFLOW = "overflow"
#: issue 0004: an allocation that disagrees with the line or the posting it points at, and one that
#: would take either past what there is to explain.
ALLOCATION_INVALID = "allocation_invalid"
OVERALLOCATED = "overallocated"
# 401/403/404/405
UNAUTHENTICATED = "unauthenticated"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
#: a path this service serves, addressed with a verb it does not.  A client error with its own code, because
#: "you used the wrong verb" and "something broke in here" are not the same thing to say to a caller.
METHOD_NOT_ALLOWED = "method_not_allowed"
# 503/500
BUSY = "busy"
INTERNAL_ERROR = "internal_error"

STATUS_OF: dict[str, int] = {
    APPROVAL_REQUIRED: 409, IMMUTABLE: 409, PERIOD_LOCKED: 409, PERIOD_OVERLAP: 409,
    IDEMPOTENCY_CONFLICT: 409, ALREADY_SUPERSEDED: 409, CURSOR_EXPIRED: 409, EVENT_GAP: 409,
    CLOSE_NOT_ELIGIBLE: 409, DOCUMENT_ALREADY_LOADED: 409, EVIDENCE_UNAVAILABLE: 409,
    STALE_VERSION: 412,
    VALIDATION: 422, UNBALANCED: 422, ACCOUNT_UNKNOWN: 422, ACCOUNT_ARCHIVED: 422, PRECISION: 422,
    OVERFLOW: 422, ALLOCATION_INVALID: 422, OVERALLOCATED: 422,
    UNAUTHENTICATED: 401, FORBIDDEN: 403, NOT_FOUND: 404, METHOD_NOT_ALLOWED: 405,
    BUSY: 503, INTERNAL_ERROR: 500,
}


class HaddonError(Exception):
    """A refused operation: ``status`` and ``code`` come from the spec's table, ``details`` carry paths/ids/sums."""

    def __init__(self, code: str, message: str, *, details: list[dict[str, Any]] | None = None,
                 retryable: bool = False, status: int | None = None) -> None:
        if code not in STATUS_OF:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.status = STATUS_OF[code] if status is None else status
        self.message = message
        self.details = list(details or [])
        self.retryable = retryable
        super().__init__(f"{self.status} {code}: {message}")

    def envelope(self, request_id: str | None = None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable,
                               "details": self.details}
        if request_id is not None:
            err["request_id"] = request_id
        return {"error": err}


def validation(message: str, **detail: Any) -> HaddonError:
    return HaddonError(VALIDATION, message, details=[detail] if detail else None)


def not_found(kind: str, id_: str) -> HaddonError:
    return HaddonError(NOT_FOUND, f"{kind} {id_!r} not found", details=[{"kind": kind, "id": id_}])
