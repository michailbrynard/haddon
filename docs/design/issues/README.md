# Haddon simplification roadmap

These issues are the completed backlog behind the [simplification plan](../simplification-plan.md). They are
kept for the reasoning, and the code reflects their outcome. Where the result differs from what an issue first
asked for, the issue's own status or delivery note says so, and the [design log](../decisions.md) records the
decisions taken while working through them.

## North star

Haddon is a **small continuous double-entry journal with auditability, plus optional reconciliation and review
extensions**.

That sentence is the test for the issues in this directory. A change should either make the journal smaller
and clearer, protect an accounting invariant, or complete a workflow that agents and reviewers genuinely use.
It should not add a general framework in anticipation of future requirements.

The higher-level sequencing, release gates and success measures are in the
[Haddon simplification plan](../simplification-plan.md). This directory is the detailed backlog behind that
plan.

## Product principles

1. **The journal is the product.** Accounts, commodities, entries, postings, corrections, period locks,
   idempotency and an intelligible audit trail form the dependable core.
2. **Time is continuous.** Fiscal years and report periods are query parameters and views, not storage
   partitions that callers must manage.
3. **Common actions are commands.** An agent should post, correct, reconcile and close using task-oriented
   inputs. Internal IDs and storage records remain available for inspection, not as prerequisites for every
   action.
4. **Auditability is not an approval framework.** Record actor, time, reason, request identity and the durable
   accounting result. Add a review queue only when a real workflow needs one, and make it end to end.
5. **Extensions do not define the core.** Statements, evidence, review queues, exports and local tax workflows
   may enrich the journal without becoming requirements for posting an ordinary entry.
6. **Exact arithmetic stays non-negotiable.** Never accept binary floating-point money. Friendly decimal
   strings may be converted at the boundary to integer minor or micro units using commodity precision.
7. **Prefer complete narrow workflows to broad surfaces.** A few well-named commands are better for agents
   and humans than many routes exposing intermediate persistence objects.
8. **Preserve history; simplify ceremony.** Posted accounting facts are corrected or reversed, not silently
   edited. This does not require every write to manufacture proposal, decision and application records.

The current one-SQLite-file-per-entity deployment can remain. It is an operational isolation choice, not a
reason for `book_year` to leak through the public model. Reconsider the duplicated `entity_id` design only in
a future schema version; it is not high-value simplification now. Multi-currency reporting and FX translation
also remain out of scope, while per-commodity double-entry remains supported.

## Issue map and dependencies

| Issue | Outcome | Depends on |
|---|---|---|
| [0001](0001-establish-core-and-extension-boundaries.md) | Define the stable core and move business-specific concerns behind extension boundaries | — |
| [0002](0002-make-the-journal-continuous.md) | Remove calendar-year books from the public and reporting model | 0001 |
| [0003](0003-collapse-self-approval-into-audit.md) | Replace automatic proposal ceremony with direct audited commands | 0001 |
| [0004](0004-simplify-statement-reconciliation.md) | Reduce a statement import to document, statement, lines and allocations | 0001, 0003 |
| [0005](0005-complete-period-close.md) | Expose one complete close/reopen workflow and simplify its checks | 0002, 0003 |
| [0006](0006-design-the-agent-command-surface.md) | Provide a compact API and generic initialization path | 0002–0005 |
| [0007](0007-complete-the-review-surface.md) | Let people inspect entries, evidence, audit history and corrections | 0003, 0004, 0006 |
| [0008](0008-harden-and-consolidate.md) | Remove duplication, fix protocol/operations gaps and add UI workflow tests | after affected surfaces settle |

The dependencies are directional rather than a demand for large sequential pull requests. Haddon is
pre-production, so schema and API replacements should be direct: rebuild development databases from source
fixtures, verify accounting results, and delete superseded structures without compatibility layers.

## Definition of done for this roadmap

- A fresh entity can be initialized without company-specific source code or a calendar-year book object.
- An agent can post and correct an entry using account paths and exact decimal amounts in one request.
- Loading a statement and classifying its lines is optional; ordinary journal operation does not depend on it.
- A reviewer can inspect an entry, its source evidence, its changes and the reason/actor for each action.
- Closing a period is either a complete public workflow or absent from the promised product surface.
- The audit trail explains durable business actions without forcing callers to understand internal workflow
  records.
- Documentation describes the current product, its explicit boundaries and a short set of supported tasks.

## Rules for issue work

- Preserve double-entry balance, commodity precision, append-only posted facts, idempotent writes and
  fail-closed locking.
- Prefer removing tables, routes and concepts over renaming or wrapping them.
- Do not build compatibility abstractions for undeployed schemas or APIs. Replace them and rebuild fixtures.
- Keep specialized corpus import/oracle adapters available for verification, but outside the generic runtime
  contract.
- Do not perform the old code-organization refactors in isolation. Consolidate only code that survives the
  domain reductions above.
