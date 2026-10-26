# 0005 — Complete the period-close workflow

**Kind:** product completion + internal simplification  
**Risk:** medium–high  
**Effort:** M

## Problem

Haddon exposes `GET /close-checks`, and the repository can close or reopen a period, but there is no public
close/reopen command. The low-level functions require callers to supply snapshot and decision IDs plus lock
versioning details. The README consequently promises an ability an agent or reviewer cannot actually perform.

Separately, `close_checks.py` uses an extensible gate class/registry framework for three statically installed
checks. Simplifying that file is worthwhile, but only after the product-level close contract is complete.

## Desired workflow

`close_period` accepts a through-date, reason, expected lock version and optional explicit overrides. In one
transaction the server:

1. evaluates the applicable checks;
2. rejects on failed or unevaluable checks unless an authorized, reasoned override names them;
3. computes and stores its own verification snapshot;
4. advances the scalar lock; and
5. writes the audit action containing checks, overrides and snapshot hash.

`reopen_period` takes a target boundary or close action plus a reason and expected version. It preserves the
old close action and snapshot, moves the lock backwards deliberately, and emits its own audit action. Callers
must not manufacture internal IDs.

## Extension policy

Core close checks cover journal invariants. Reconciliation coverage checks apply only when reconciliation is
enabled and configured for the relevant accounts/period. “No statement configuration exists” must be an
explicit policy result, not accidentally interpreted as either success or failure.

## Work

1. Freeze the current useful behavior: named failures, `unevaluable` taking precedence, allocation checks,
   bounded diagnostic output, override scoping and audit serialization.
2. Add typed close/reopen command handlers and HTTP routes, both idempotent. Return the new lock state, audit
   action and snapshot summary.
3. Add UI affordances for viewing checks, closing, confirming overrides and reopening. Hide actions the
   authenticated role cannot perform.
4. Replace the close-gate registry/classes with explicit functions in a fixed policy. Preserve fail-closed
   evaluation; target file size is a guide, not an acceptance criterion.
5. Update documentation and capabilities so they describe the workflow that actually exists.

## Acceptance criteria

- An authorized agent can close or reopen a period with one public command and no pre-created IDs.
- Snapshot, lock change and audit action commit atomically.
- A raised, malformed or unevaluable check blocks close by default.
- Each override names a current failure and has a stored actor and reason; blanket bypass is impossible.
- Closing twice with the same idempotency key is harmless; stale lock versions fail clearly.
- The UI can show why close is blocked and link a completed close to its immutable snapshot.
- The old registry/base-class machinery is gone without weakening the existing close eligibility tests.

## Delivered

All of it, with three readings recorded in [`decisions.md`](../decisions.md) § "Issue 0005" rather than silently
taken.

**Corrected after review.** That claim was false about half of criterion 6 when it was written: *"link a
completed close to its immutable snapshot"* was returned in the close response and rendered nowhere, and
`close_records` had no reader at all, so a past close could only be reached by walking `/events`. It is true now
— `GET /closes/{id}`, `lock_close_id` on the entity summary, and the Close page showing the evidence behind the
standing lock. The review's other finding, a blind spot in `nothing_in_draft` at the start of the first close
window, is recorded in `decisions.md` § "The 0005 review, and the two things it found".

The three readings:

- **"authorized" means recorded, not enforced.** Haddon has no role model and this did not add one; the acting
  principal is stored on every override, and the UI gates on the write-vs-read credential that exists.
- **the idempotency key and `expected_version` are not one acceptance criterion.** They answer "is this a retry?"
  and "am I closing the books I was looking at?", and a close needs both.
- **`nothing_unexplained` became two checks.** It answered a core question (a draft entry) and an extension
  question (an unexplained statement line) at once, which is why a switched-off extension had to fake a pass for
  half of it.  They are `nothing_in_draft` and `lines_explained` now.

## Pitfalls

1. Reopen is exceptional but necessary; do not make it silently gated by the checks used to close.
2. Do not trust a snapshot hash or eligibility result supplied by the caller.
3. Preserve per-commodity verification and the continuous-journal semantics from issue 0002.
4. Do not require reconciliation artifacts when that extension is not enabled.
