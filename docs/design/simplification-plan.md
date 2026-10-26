# Haddon simplification plan

This is the roadmap Haddon was built against after its first working version, and every phase in it is
complete. It is kept for the reasoning behind the current design; the code reflects its outcome, and where the
two differ the code and the current documents in `docs/` are right. The decisions taken while carrying it out
are in the [design log](decisions.md).

## Objective

Build Haddon as a small continuous double-entry journal that is easy for an agent to maintain and easy for a
person to review. Auditability, exact arithmetic and safe period locking are core. Reconciliation, evidence
review, rendered artifacts and company-specific workflows are optional extensions.

Haddon is still pre-production. There are no customer databases or external API clients to preserve. The
detailed backlog lives in [`docs/design/issues`](issues/README.md); this plan describes how to deliver it without
carrying the current design forward as compatibility baggage.

## Delivery approach

Use **design → replace → verify → delete**:

1. **Design** the smallest complete target workflow and its invariants.
2. **Replace** the existing path directly, including its schema and public contract.
3. **Verify** accounting results against focused invariant tests, representative source data and hledger.
4. **Delete** the superseded tables, routes, abstractions, tests and documentation immediately.

Vertical slices still matter, but for correctness and feedback—not backward compatibility. Each slice should
leave one supported implementation of a useful task. Do not dual-write, version an unused API, add deprecation
adapters or retain legacy audit views.

### Data and migration policy during development

- Treat Haddon development databases as disposable and rebuild them from source journals, documents or test
  fixtures after schema changes.
- Preserve valuable accounting source data and expected results, not accidental internal row shapes or IDs.
- Change the current migrations freely while the product has no deployed databases. Squash them into a clean
  initial schema after the target model settles. (Delivered through the target model settling, but the squash
  itself was **not** done with phase 6 and is recorded as a deliberate deferral in
  [issue 0008](issues/0008-harden-and-consolidate.md) § "Not done here": the schema already carries no
  transitional structures, and collapsing the chain is a separate, riskier change to do on its own.)
- If an expensive local dataset cannot be regenerated, use a one-off development conversion script. Do not
  make that script or its compatibility structures part of the runtime product.
- Begin forward-only production migrations only when a persistent shared or customer environment exists.

## Phase 0 — Fix the target and baseline the invariants ✅

**Issue:** [0001 — core and extension boundaries](issues/0001-establish-core-and-extension-boundaries.md)

Agree on the core vocabulary and dependency direction. Capture representative source journals, documents,
hledger exports, report totals and period-lock cases as reproducible fixtures. These protect accounting
behavior while allowing the implementation and schema to change freely.

Deliverables:

- a short architecture decision defining core and extensions;
- dependency rules preventing the journal core from importing extensions;
- a generic sample entity and source journal; and
- golden balance, report, correction and lock expectations.

**Exit gate:** the team can distinguish accounting facts, audit facts and optional workflow state, and the
fixture suite detects changes to the core invariants.

## Phase 1 — Build the small journal vertical slice ✅

**Issues:** [0003 — collapse self-approval into audit](issues/0003-collapse-self-approval-into-audit.md),
[0006 — agent command surface](issues/0006-design-the-agent-command-surface.md)

Implement one end-to-end path through the intended architecture: initialize a generic entity, post an entry
with account paths and exact decimal strings, correct or reverse it, read a report and inspect its audit action.
Use typed application commands shared by HTTP and any future agent-tool adapter.

Replace the corresponding proposal/decision/application paths as each command lands. Do not operate both
architectures beyond a short-lived development branch comparison.

Deliverables:

- generic entity initialization with a small sample chart;
- `post_entry`, `correct_entry` and `reverse_entry` commands;
- direct audit actions with no synthetic self-approval chain; and
- a minimal quickstart and command contract tests.

**Exit gate:** a fresh database can complete the journal loop without a corpus checkout, caller-visible annual
book, statement configuration or proposal concepts.

## Phase 2 — Make time continuous ✅

**Issue:** [0002 — continuous journal](issues/0002-make-the-journal-continuous.md)

Replace the annual-book schema with dated journal facts and arbitrary report ranges. Remove carry-forward
machinery and calendar-year validation in the same body of work. Rebuild fixture databases and compare
same-period and cross-year output with the hledger source journals.

Deliverables:

- entries and related facts keyed by entity/date rather than book year;
- arbitrary report ranges (**delivered**) and optional fiscal-period presets (**deferred** — reasoned in
  [issue 0002](issues/0002-make-the-journal-continuous.md), "Delivered, and what was deferred"; the trigger is
  the first entity whose financial year does not start on 1 January);
- direct continuous balance queries; and
- a clean rebuilt schema with annual book/carry structures deleted.

**Exit gate:** December-to-January posting and reporting works without opening a book or executing a carry
operation, and all balance/precision/lock fixtures pass per commodity.

## Phase 3 — Replace reconciliation with the smaller extension ✅

**Issue:** [0004 — statement and reconciliation model](issues/0004-simplify-statement-reconciliation.md)
**Landed as:** migration 0009, `repository/{statements,lines,reconcile}.py`, `haddon/extensions.py`

Replace the current source config/scope/assertion/match machinery with the document → statement → line →
allocation/disposition model. Implement statement replacement as part of the first complete version, then
rebuild reconciliation fixtures directly from their source documents.

Deliverables:

- one-command statement loading and replacement;
- task-oriented booking, linking, splitting and ignoring of lines;
- preserved document bytes/hashes and append-only classification history; and
- reconciliation summaries and optional close coverage based on the new records.

**Exit gate:** the core journal works with reconciliation disabled, while an enabled extension can explain
every configured statement line and safely replace incorrect input. *(Met: `tests/test_reconciliation_optional.py`
runs the whole journal loop with `HADDON_RECONCILIATION=off` and proves the switch is not a no-op;
`tests/test_line_explanations.py` covers explaining and replacing.)*

## Phase 4 — Complete the operational command set ✅

**Issues:** [0005 — period close](issues/0005-complete-period-close.md),
[0006 — agent command surface](issues/0006-design-the-agent-command-surface.md)

Finish the compact command set around the stable model. Closing and reopening become high-level atomic
operations; callers never pre-create snapshots, decisions or other internal IDs. Publish only commands
available to the current actor and enabled extensions.

Deliverables:

- atomic `close_period` and `reopen_period` commands;
- explicit core versus reconciliation close policies;
- account lifecycle and remaining reconciliation commands; and
- generated, permission-aware command discovery.

**Exit gate:** an agent can maintain a normal period using the published commands alone, including safe retry
after a timeout and recovery from validation errors.

## Phase 5 — Complete the reviewer loop ✅

**Issue:** [0007 — human review surface](issues/0007-complete-the-review-surface.md)

Orient the UI around verifying agent work rather than exposing repository objects. Make entries, evidence,
audit actions and correction chains first-class. Complete every accounting task for which the current UI sends
the reviewer to the API.

Deliverables:

- entry detail, audit timeline and source/correction navigation;
- authorized evidence viewing and download;
- manual entry, correction, split classification, statement replacement and close UI; and
- accurate atomic or per-item batch behavior.

**Exit gate:** a reviewer can understand, correct, reconcile and close a period without command-line access or
knowledge of internal workflow IDs.

## Phase 6 — Consolidate and prepare for first deployment ✅

**Issue:** [0008 — hardening and consolidation](issues/0008-harden-and-consolidate.md)

Consolidate request parsing, response validation, report shaping and generated client types after the domain
deletions. Fix protocol and pagination correctness, shorten current documentation and add browser coverage for
the critical reviewer loop. Establish backup/restore and forward-only migration practice now, for the first
persistent deployment rather than as a constraint on pre-production redesign.

Deliverables:

- one canonical report presentation used by JSON, UI and PDF;
- stable pagination, error envelopes and capabilities;
- concise current architecture and task documentation;
- browser coverage for the critical reviewer workflow; and
- a clean initial schema plus production-readiness backup/recovery procedure.

**Exit gate:** there is one supported implementation for each core task, the schema contains no transitional
structures, and Haddon is ready to start preserving deployed data from this point forward.

## Verification gates for every phase

- A fresh database provisions and loads all representative fixtures.
- Double-entry balance, commodity precision, append-only correction and period-lock invariants pass.
- Relevant hledger exports and report totals match, except for explicitly approved product changes.
- Failure and retry tests prove each command is atomic and idempotent.
- Removed concepts are absent from schemas, routes, capabilities, code, tests and current documentation.
- A change that affects accounting meaning is recorded as a product decision, not hidden in refactoring.

## Success measures

The simplification has succeeded when:

- an ordinary entry requires one command and no workflow-object knowledge;
- correction, reconciliation and close are each one typed command;
- no public command or stored journal fact requires a calendar-year book;
- normal writes create no proposal/decision/application chain;
- a generic entity runs without corpus or reconciliation configuration;
- a reviewer can trace every posted entry to actor, reason, source and correction history;
- the core can be tested independently of optional extensions; and
- the number of supported concepts, routes, tables and duplicated presentation rules materially decreases.

Record the current counts before Phase 1 and compare them after Phase 6. Reduced complexity is an outcome to
verify, not an assumption based on moving code between modules.
