# 0008 — Harden and consolidate the surviving product

**Kind:** correctness + operations + focused refactor  
**Risk:** low–medium  
**Effort:** L, split into independent changes  
**Status:** done — A–E as below; the one plan-level item left open is the migration squash (see "Not done here").

## Why this comes last

The previous review correctly found API drift, duplicated parsing and presentation logic, an oversized
classification module, stale documentation and missing UI tests. Those are real issues, but refactoring code
that issues 0002–0004 will remove would create churn rather than simplicity.

Apply the workstreams below independently after the affected domain surface is stable. Correctness fixes that
do not depend on the redesign can land immediately.

## A — Protocol and operational correctness

- Map unsupported HTTP methods to a stable client error rather than `internal_error`.
- Use a configured, durable cursor-signing secret (with rotation/versioning) rather than a fresh process-local
  value that invalidates pagination after restart.
- Apply derived filters before pagination where practical. Where a materialized state makes that unreasonable,
  make the short-page-with-next-cursor behavior explicit and test clients following the cursor; the current
  statement-line `state` filter can otherwise look like an incomplete result.
- Declare and validate response models for write routes, or correct documentation that claims all responses
  are validated.
- Derive advertised capabilities from the actual command/route registry plus actor permissions and enabled
  extensions. Test reads as well as writes so endpoints cannot silently disappear from discovery.
- Keep current 200/201 behavior unless a client-visible inconsistency causes harm. Status-code aesthetic churn
  is lower value than a stable compact contract.

## B — One source of truth for report presentation

- Serve a canonical shaped statement/report view from Python instead of independently implementing ordering,
  sign, period wording, result labels and footnotes in Python PDF code and TypeScript.
- Make the balance sheet’s synthetic current-period result and reconciliation difference first-class report
  output so JSON, PDF and UI cannot disagree mid-period.
- Generate or validate TypeScript transport types from the API schemas rather than maintaining partial manual
  mirrors.

## C — Focused code consolidation

- Keep one strict request-body parser, including rejection of JSON numbers for exact-money fields.
- Split `repository/classification.py` by stable responsibilities only if it remains large after issue 0004.
  Prefer plain functions and small records; do not introduce a service hierarchy merely to shorten a file.
- Expose account archive through the task-oriented command surface and include it in discovery.
- Delete compatibility helpers, dead tables and adapters with the changes that supersede them.

## D — Documentation and operations

- Replace the long chronological design narrative with a short current architecture, invariants, command
  quickstart and targeted decisions. Git preserves the history.
- Generate volatile inventories such as routes, schemas, tables and triggers rather than hand-maintaining
  counts in prose.
- Before the first persistent deployment, document SQLite-safe backup/restore, WAL checkpoint expectations,
  integrity verification and retention.
- Add a recovery drill for the deployment-ready schema that restores a backup, opens it read-only, verifies
  integrity and compares an exported journal or snapshot hash.

## E — Tests at workflow boundaries

- Add component or browser tests for the reviewer’s critical loop: period → line → entry/evidence → correction
  → reconciliation → close.
- Test partial network failure and retries for any non-atomic batch UI.
- Keep repository invariant tests and hledger/oracle comparisons, but avoid pinning incidental route counts or
  serialized implementation objects when task behavior is the real contract.

## Acceptance criteria

- Restarting an API process does not invalidate otherwise-valid cursors under the configured key policy.
- 405 and other expected client failures have stable non-500 error envelopes.
- Capability output matches the usable, authorized surface automatically.
- JSON, UI and PDF balance-sheet/statement presentation agree on signs, ordering and totals.
- The main README supports a new user without requiring the chronological decision log.
- Backup restoration and the primary reviewer workflow are exercised automatically.
- Refactors produce fewer implementations or concepts; line movement alone does not count.

## What shipped

- **A — protocol and operational correctness.** Unsupported methods map to a stable client error; cursors are
  signed with a configured keyring rather than a process-local secret; the one filter that cannot run before the
  page says so; write replies are validated against their declared model; capabilities are derived from the
  mounted routers. (`decisions.md` § "Issue 0008 section A".)
- **B — one source of truth for report presentation.** `/reports/statements` publishes the arrangement
  `haddon.statements.shape` decides, and both the interface and the PDF are built from it; the balance sheet's
  result row and its balance check are first-class output. `web/src/lib/statement.ts` no longer implements any
  accounting rule. (`decisions.md` § "Issue 0008 section B".)
- **C — focused code consolidation.** One strict request-body parser (`api/wire.parse_body`); the oversized
  classification module deleted by issue 0004; account archive and restore published through the command
  surface; compatibility helpers and dead code removed with the changes that superseded them.
- **D — documentation and operations.** `README.md`, `docs/architecture.md`, `docs/operations.md` and the
  decision log describe the current product; volatile inventories are generated or derived rather than counted
  in prose; SQLite-safe backup/restore and the recovery drill are documented and exercised.
- **E — tests at workflow boundaries.** The reviewer's critical loop runs in a real browser
  (`browser/`), the backup is restored rather than assumed (`tests/test_backup_restore.py`), and the shaped
  statements are compared across JSON, the interface's types and the PDF.

## Not done here

- **The development migration chain is not squashed.** The plan's data policy suggests collapsing `0001`–`0010`
  into a clean initial schema once the target model settles, but this issue is not the place to do it and the
  phase 6 exit gate does not require it: the schema already contains no transitional structures (every one was
  dropped by the migration that superseded it), and a forward-only chain from the current head is what "ready to
  preserve deployed data" actually needs. The squash is a repository-history cleanup with its own risk to a
  suite that rebuilds fixtures from the migrations; it should be done deliberately, on its own, if at all.

## Deliberately dropped from the previous issue set

- Do not optimize pagination for the generic proposal endpoint; issue 0003 removes that endpoint.
- Do not preserve the close-gate registry merely to support hypothetical gates; issue 0005 uses an explicit
  policy.
- Do not split classification before its simplified data model is known.
- Do not make annual-book layout or corpus checkout paths permanent documented constraints; issues 0002 and 0006
  remove them from the product contract.
