# 0004 — Simplify the statement and reconciliation model

**Kind:** extension redesign + schema/API replacement  
**Risk:** high  
**Effort:** L  
**Status:** done (Phase 3) — migration 0009; see `docs/design/decisions.md` § "Issue 0004"

## Problem

Loading one statement currently creates a source file, an often synthetic source configuration, statement
scope, source rows, assertion/proposal/decision records, evidence links and events. Classification then adds
match sets, matches and dispositions with several overlapping notions of “current”.

Much of that structure exists to satisfy triggers or anticipated import formats rather than the task a human
or agent understands: preserve this document, record its opening/closing balance and lines, then explain how
those lines relate to journal postings.

The model is also incomplete where correctness matters most. Statements contain supersession concepts, but
there is no complete public replacement/correction workflow for a statement loaded incorrectly.

## Desired model

Keep reconciliation optional and express it with four primary concepts:

1. **Document** — immutable source bytes or an external reference, media type, content hash and provenance.
2. **Statement** — account path/ID, commodity, date range, opening and closing balance, source document and
   replacement relationship.
3. **Statement line** — date, description, signed exact amount, stable source key and preserved raw fields.
4. **Allocation/disposition** — an append-only explanation linking some or all of a line to one or more
   postings, or recording why it is intentionally not booked.

A source-format configuration is useful only when the user actually defines a reusable importer. Do not
create empty configuration/directory records to make an ordinary statement valid.

## Work

1. Specify the minimal records and their invariants. Keep statement-balance arithmetic, commodity precision,
   source hash uniqueness, allocation bounds and locked-period protection.
2. Make statement creation one idempotent transaction. It may store or reference a document, but should not
   expose the intermediate persistence records as a caller workflow.
3. Add `replace_statement` as a first-class command. It must retain the old statement, point to the
   replacement, prevent two live successors and refuse replacement when the affected period is locked.
4. Express line classification in task language: book a line, link it to existing posting(s), split it, ignore
   it with a reason, or undo/replace a prior classification. Let the repository choose allocation grouping
   IDs.
5. Rebuild reconciliation fixtures from their source documents into the simpler view. Preserve source
   bytes/hashes and useful provenance, not IDs belonging to the discarded schema.
6. Rewrite close coverage and reconciliation summaries against the new records, then remove synthetic source
   configs, redundant opening assertions and obsolete matching machinery.

## Acceptance criteria

- A statement can be loaded with account, commodity, dates, balances, lines and optional document/evidence in
  one command.
- The resulting user model contains no mandatory empty source configuration or hidden opening-balance
  proposal.
- A mistaken statement can be replaced through the public API and is visibly linked to its predecessor.
- Each line has one clear current status and a complete append-only history of how it reached that status.
- Partial and split allocations cannot over-allocate a statement line or posting.
- Reconciliation is removable/disableable without preventing core journal entry and reporting workflows.

## Pitfalls

1. “Simpler” must not discard raw source data or source hashes needed to prove provenance.
2. Statement balance and reconciliation status are different: a mathematically valid statement can still have
   unexplained lines.
3. Replacement and classification changes in locked periods must fail closed.
4. Avoid replacing the current class/registry complexity with a new generic workflow engine.
