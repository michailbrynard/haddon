# Haddon design log

This is the decision record kept while Haddon was built, in chronological order. Each section is named after the
build step that produced it ("chunk C1") or the design issue in [`issues/`](issues/README.md) it implemented
("issue 0004"), and code comments cite sections by those names. A later section can supersede an earlier one, and
some describe behaviour that has since been removed. For how Haddon behaves today, read the documents in `docs/`
first ([`architecture.md`](../architecture.md), [`concepts.md`](../concepts.md),
[`agent-guide.md`](../agent-guide.md), [`operations.md`](../operations.md)) and come here for the reasoning
behind them.

## Chunk A — the schema and the review's defects

Inputs: the original design spec (an internal action plan, not published with this repository) and an
independent review of it. Numbers D1–D7 are the review's verified defects; U1–U8 are its "Under-specified" items. Each SQL fix is marked with its number in
`src/haddon/db/migrations/sql/0001/*.sql`.

## Review defects

- **D1 (REPLACE bypasses DELETE triggers).** `PRAGMA recursive_triggers=ON` is a mandatory per-connection setting
  next to `foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=5000` (`haddon/db/connection.py`). Defence in depth:
  BEFORE INSERT on `entries`, `match_sets`, `period_locks`, `events` refuses an existing key (`RAISE(ABORT,'exists')`),
  so those four survive even a misconfigured connection. `tests/test_no_replace_in_src.py` fails CI if `OR REPLACE`
  or `REPLACE INTO` appears anywhere under `src/`.
- **D2 (global `overallocated` check).** `sets_validate` now checks only the rows and postings referenced by the set
  being sealed, and only when the new status is `sealed` (voiding never creates over-allocation). The `overallocated`
  view is kept unchanged as a verifier-level query, never called from a per-write trigger.
- **D3 (settlements had no lock guard).** New `settlements_insert`: both entries must be posted, distinct, same
  entity, and both dated after the lock. Consequence: settling a locked-period invoice requires a reviewed reopen,
  which is the same rule the spec applies to matching a later statement to a locked cash posting.
- **D4 (lock advance strands drafts).** Resolution chosen: **allow `draft → void` regardless of the lock, and keep
  the lock advance unblocked.** A draft (entry or match set) is not accounting; voiding it changes no balance, and
  blocking the close on agent scratch work would let any stale proposal hold a month open. The exemption is narrow:
  for entries only `status`/`version` may change (dt, narration, origin, book_year, created_* must be identical);
  for match sets only `draft → void`. `sealed → void` under a lock, seal of a locked draft, and every other edit of
  a locked draft remain refused. The matrix covers the positive and negative sides.
- **D5 (supersession ignores subject).** `supersedes_id` must point at a row with the same `account_id` and
  `commodity` on `statement_assertions`, `statement_scopes`, `opening_assertions` (`supersedes_subject`).
- **D6 (error code depends on trigger firing order).** The repository pre-validates each write in a fixed order and
  maps to the spec's 409/412/422 codes deterministically; trigger text is the fallback mapping
  (`haddon/repository/errors.py`). Full pre-validation lands with the repository layer; this chunk ships the mapping.
- **D7 (FK-only guards).** `assert_connection_safe` checks `foreign_keys=1`, `recursive_triggers=1`,
  `ignore_check_constraints=0` (and WAL on file databases) on every connection hand-out and again at the start of
  every `write_transaction`; a failing connection is closed and never handed out.

## Under-specified items (review's suggested reading taken unless noted)

- **U1** `postings` → `account_commodities` is the three-column FK `(entity_id,account_id,commodity)`; the two-column
  form has no matching UNIQUE.
- **U2** `commodity_styles UNIQUE(book_year,commodity)`, `source_configs UNIQUE(book_year,source,account_id,commodity,version)`
  and `journal_mappings UNIQUE(book_year,source_revision,source_index)` stay exactly as the spec writes them, without
  `entity_id`: one file is one entity (`entities.singleton` enforces it), so the extra column adds nothing.
- **U3** "A dated economic row must fall within its book" is a CHECK on: `entries.dt`, `source_rows.dt`,
  `statement_scopes.start_dt/end_dt`, `statement_assertions.start_dt/end_dt`, `snapshots.through`;
  `opening_assertions.as_of` is already pinned to 1 January of its book.
- **U4** `decisions`: `CHECK((proposal_id IS NOT NULL)+(batch_id IS NOT NULL)=1 OR kind='migration_accept')`.
- **U5** A multi-commodity entry whose legs net to zero **per commodity** seals successfully (hledger permits it).
- **U6** `-9223372036854775808` is excluded by CHECK on **every** micros column: `postings.amount_micros`,
  `source_rows.amount_micros`, `statement_assertions.closing_micros`, `opening_assertions.amount_micros`,
  `opening_verifications.computed_micros`, `matches.amount_micros`, `settlements.amount_micros`.
- **U7** Reopening to "no lock" is impossible by design (`close_records.through` NOT NULL, reopen requires
  `NEW.through < OLD.through`); the first close is permanent history and can only be moved earlier.
- **U8** Archived accounts are accepted on draft legs and refused at seal (`account_invalid`); the API maps the seal
  failure to 422 `account_archived`, so the error surfaces on the seal step, not on the leg.

## Other facts fixed here

- The spec's table list has **36** tables (the review's "37" counts SQLite's internal `sqlite_sequence`).
- Migration 0001 pins the sha256 of its SQL files; any schema/trigger change is a new numbered SQL directory
  plus a new Alembic revision, never an edit of 0001.
- Spec per-connection PRAGMAs are applied in `haddon/db/connection.py`, not in the migration (WAL cannot be set inside a
  transaction and pragmas are not schema).
- `write_transaction` refuses to commit if any database error was raised inside it, even if caught by caller code.
- The matrix runner counts a mutation as refused only for constraint-class errors (`IntegrityError`, Python
  `OverflowError`, or SQLite's `integer overflow`). Any other `OperationalError` (syntax error, unknown column, missing
  trigger) is reported as `MALFORMED-CASE`, and a failing setup statement as `SETUP-ERROR`; neither can count as a
  pass. Forty of the review-fix cases additionally pin *which* guard must fire (`expect=`), so a case cannot be
  satisfied by an unrelated refusal.

## Chunk B2 — year-scoped reports (`haddon.reports`)

- **B2 (retained mapping).** The carry-forward target is the entity's `equity:retained-earnings`, or
  `equity:retained-losses` when `entities.country` is `ES`. The rule lives once, in `provisioning.Entity.retained_path`
  / `verify_retained_mapping`; `create_book` verifies it when the book is created and `reports.carry_forward` re-verifies
  the **target** book's `retained_account_id` through the same function before folding anything into it.
- **Scoping is structural.** Every function in `haddon.reports` that takes a connection has a required keyword-only
  `book_year` / `years` / `from_year`; `tests/test_reports_year_scoping.py` walks the package and fails otherwise. There is
  no "as of date across all books" entry point, and `multi_year_movements` returns per-year columns with no total.
- **Refusals.** Unknown book → 404 `not_found`; malformed year → 422 `validation`; an explicit date outside the named
  book → 422 `validation` `out_of_book`; both `start` and `end` given and in different years → 422 `validation`
  `spans_book_years` with a `registers` list (one `{book_year,start,end}` per year) for the caller to request separately.
  Check order: shape → spans → out-of-book → `start <= end`.
- **Official = `posted`.** Drafts and voids are invisible to every report. Opening entries are those with
  `entries.origin='opening'`; movements exclude them.
- **Sums are Python ints** (`sum_micros`), never SQLite `SUM`: a per-account aggregate past int64 is 422 `overflow`
  (SQLite's own `SUM` would raise `integer overflow`, a 500-class database error that also taints the transaction).
- **Register order** is `(entries.dt, entries.created_at, entries.id, postings.seq)` — durable, unlike rowid (VACUUM may
  renumber rowids on tables without an INTEGER PRIMARY KEY). Entries created in the same second sort by id.
- **Carry-forward folds income/expense at their full closing balance** in the predecessor book, not only their movement.
  With a correct predecessor opening (zero for income/expense) the two are equal; folding the full balance is what keeps
  the expected opening balanced per commodity when the predecessor opening was itself wrong (which verification of the
  earlier pair reports separately).
- **`verify_carry_forward` returns, it does not raise.** `ok=False` / `status='failed'` with every unexplained
  `Difference` (kind `mismatch` / `missing` / `extra`, exact `actual - expected`). Documented closing reclassifications
  may be passed in; they must balance per commodity and explain a difference exactly, or the difference stays a failure.
  `to_year` must be `from_year + 1` (books are consecutive calendar years).
- **Zero handling.** A pair that nets to zero within the book is omitted unless `include_zero=True`; a pair with no
  posting never appears; `account_balance` returns `ZERO` for either. `trial_balance(...).get(...)` reads absent as `ZERO`.

## Chunk C1 — hledger import (`haddon.importing`)

- **C1 (verifier pin is a gate).** `vendor/hledger` (1.52.1, sha256 in `vendor/hledger.sha256`) is re-verified — sha256
  of the file *and* the `--version` string — before **every** invocation. The pin is **per platform**: release
  binaries are not byte-identical across architectures, so the pin file holds one `linux-x86_64` and one
  `mac-aarch64` sha256, and the version gate matches the `1.52.1` number (accepting the official
  `1.52.1-g<sha>-<date>` build suffix) while still refusing 1.52/1.52.3. A platform with no pin is refused. A
  system-installed `hledger` on `PATH` is never used. A nonzero exit carries hledger's verbatim stderr plus the parsed file and line; nothing is summarised.
- **Amounts** come from `aquantity.decimalMantissa` / `decimalPlaces` only, converted with `Decimal.scaleb` and
  `Micros.from_decimal` (no division, no float; JSON floats are loaded as strings). `decimalPlaces > 6`, and any posting
  whose places or applied style precision differ from the commodity directive's precision, are refused.
- **Declarations are the chart.** Commodities: `hledger commodities --declared`, precision read from the `commodity`
  directive in the files `hledger files` reports (never inferred from amounts). Accounts: `hledger accounts --declared`
  only; a posting on an undeclared account is a hard `account_unknown` error naming account and `tindex`. Account type is
  the first path segment; an unrecognised segment is an error. `is_cash` = `assets:cash` and `assets:cash:*`.
- **Conservative default (flagged):** every declared account allows **all** of the entity's declared commodities. The
  spec wants explicit per-account currencies; today each journal declares exactly one commodity so the sets coincide, but
  the rule is wider than "what the account uses" and must be tightened before a second commodity is declared.
- **Origin is economic, source identity is separate.** `origin` is derived once (`importer.origin_for`) from the
  transaction's source file: `*-opening.journal` → `opening`, else `source`. `journal_mappings` stores book year, source
  revision (checkout `git rev-parse HEAD`), `tindex`, the checkout-relative source file, `tstatus`, the entry id and a
  canonical JSON record of the transaction as imported (date, description, code, comment, tags, status, and every leg's
  account, commodity, integer-string micros, comment, tags in posting order) for the C2 oracle. `txn_id` stays NULL (C2).
- **Unrepresentable journal features are refused, not dropped:** cost/price annotations, multi-amount postings, virtual
  postings, posting-level dates, secondary dates, `decimal-mark` directives. Balance assertions are accepted (hledger has
  already checked them; they carry no economic content).
- **Memo** = the raw posting comment verbatim (trailing newline kept); tags hledger reports that are not literally in the
  comment are appended as `name: value` lines; `NULL` only when there is neither.
- **Blocked entities.** `import_entity` raises `ImportBlocked` when `check --strict` fails, before anything is written.
  `on_blocked="skip"` writes the entity row and one `import.blocked` event carrying the real error and returns a
  `BlockedResult` (`import_state='unavailable'`, zero counts). No chart or book can be built from a journal that failed
  the gate (a journal that declares no commodity, for example), so none is; a second run on that file is refused (`immutable`), so a skip can
  never turn into a silent success.
- **The reported defect is the first one in include order.** hledger stops at the first strict failure and the
  opening journal is included first, so a journal with two defects reports the one in its opening file even when
  the other is the one a reader expected. The error is passed through as hledger wrote it. Nothing in the corpus
  checkout is modified, ever.
- **Import target** is an empty, migrated database (one file = one entity); an already-provisioned file is refused. The
  import is left at `import_state='staging'`: no `migration_accept`, no lock, nothing verified.

## Chunk C2a — deterministic journal export (`haddon.exporting`)

The mirror image of C1: the book goes back out as hledger journal files, byte-for-byte reproducible, verified by
the pinned binary, recorded in an immutable `exports` row. This chunk is **not** the C2 oracle; it produces the
artifacts the oracle will reparse.

### What makes the bytes deterministic

Every ordering and rendering rule lives in one pure module (`exporting/render.py`), which takes a `BookState`
and returns `{filename: text}`. Nothing in it reads the clock, a uuid, a float or a set iteration order.

- **Transaction order is `(dt, entry_id)`** — one rule, total, no tie-break needed, because the entry id is
  unique. The plan offers "date then source index"; the entry id is *better* here and gives the same answer for
  imported entries, because C1 mints `e-<year>-<tindex:06d>`, so within a date the id order **is** `tindex`
  order. Store-native entries (reversals, manual postings) have no source index at all, so an id rule is the only
  one that is total over a real book. Postings keep their stored `seq`.
- **Amounts** are `Micros.to_decimal_string(commodity)`: integer micros plus the commodity's *declared*
  precision, exact or a 422 `precision`. Never a float, never a stored display string, never hledger's rendering.
- **Accounts, commodities and files** are byte-ordered; `accounts.journal` and the opening group are included
  first, then the remaining groups in byte order. Column widths are computed from the file's own longest account
  path and longest amount, so alignment is a function of content, not of insertion order.
- **`state_hash`** = sha256 of the canonical JSON of everything rendered (entity, book, chart, commodities, and
  every transaction with its postings). It goes in the main journal header, on the snapshot, and in the manifest.
- Two exports of one snapshot are byte-identical, in one process and across a fresh one
  (`test_two_exports_of_the_same_book_are_byte_identical`, `test_the_same_bytes_come_out_of_a_fresh_process`,
  and the same pair over the corpus's ExampleInc and ExampleBV books).

### The other rules

- **Only `posted`.** The read is `reports.postings.posted_postings`, which already filters `status='posted'` and
  one `book_year`; reusing it means the export cannot drift from what the reports call official. A committed
  draft and a void are both in the file and neither appears. A reversed original **does** appear, next to its
  reversal — that is what a correction is in this store.
- **One book year per export**, scoped through `reports.scope.scope`. An unknown year is a 404.
- **Source groups come from `journal_mappings.journal_path`**, the rows C1 wrote: `<year>-<group>.journal` →
  `<group>`. The group is validated to a lowercase `a-z0-9-` word, so a stored path can never steer a write out
  of the managed directory. Entries with no mapping row (reversals, manual postings — everything minted in the
  store rather than imported) go to **`<year>-haddon.journal`**: the plan has no bucket for them, and inventing
  one per origin would multiply files for no reader's benefit.
- **The transaction status mark** comes from `journal_mappings.source_status` (`Cleared` → `*`, `Pending` → `!`,
  `Unmarked` → none); a store-native entry is `*`, because posted is what the store means by cleared.
- **A 0-decimal commodity is declared `commodity JPY 1000.`** — with the trailing point. hledger 1.52 refuses a
  directive with no decimal mark at all, and `1000.` is its own documented "no decimals" example; C1's
  `precision_of_style` reads it back as 0. Digit-group marks are *not* emitted (`1000.00`, not `1,000.00`): the
  style is display-only and a mark we do not need is a mark we could get wrong.
- **Refused, not mangled.** A narration holding `;`, a newline or padding whitespace; an unmarked narration
  starting with `*`/`!`; an account path with a comment character, a tab or a double space; a commodity symbol
  that is not plain alphanumeric; a source filename that is not `<year>-<group>.journal`. Each raises
  `UnrenderableContent` before anything is written.

### The verifier is a gate, and the row comes last

`hledger check --strict` runs on the staged output through `Hledger` (so `resolve_binary` re-verifies sha256 and
`--version` before the run, as C1 requires). A failure is `ExportBlocked`: nothing is published and nothing is
recorded. The artifacts are then moved into place and **re-hashed on disk**; a mismatch is `ArtifactMismatch`,
again with no row. Only then does one short `write_transaction` write, all through the repository API: an
`export` operation (`queued → running → succeeded`), a snapshot, the `exports` row
(`repository/exports.py`, which pre-validates its references and digest shapes so an unknown snapshot is a clean
404 rather than a tainting FK failure), and an `export.completed` event carrying the whole manifest.

The verifier runs on the staged bytes rather than the published ones because a strict failure must leave no
debris; the published bytes are proven identical to the verified ones by the re-hash, and
`test_the_exported_journal_passes_the_strict_check_as_published` re-runs the check on what is actually on disk.

### Things worth knowing

- **One state is one snapshot.** `snapshots.state_hash` is `UNIQUE`, so re-exporting an unchanged book reuses the
  existing snapshot row (new read `closing.snapshot_by_state_hash`) instead of asking the database to refuse a
  duplicate. A side effect worth having: the manifest and its sha256 are then identical across re-exports too.
  `create_snapshot` itself was **not** given a pre-validation gate — `test_repo_closing` deliberately pins the
  current "the DB refuses the duplicate and taints the transaction" behaviour, and changing that is not C2a's call.
- **`tables.py` gained the `exports` declaration.** It was the one table in the schema with no Core declaration;
  the anti-drift test now covers it.
- **No proposal is recorded.** `propose_and_accept` is available and the plan allows it, but an export reads the
  book and writes files — it decides no accounting question, so there is nothing to propose. The `exports` row,
  the snapshot and the event *are* the record, each carrying `created_by` verbatim. Nothing branches on the actor.
- **The manifest is not written into the managed directory.** It carries the snapshot sequence, which differs
  between a first export and a later one, so a `manifest.json` next to the journals would break the byte-identity
  guarantee for the directory. It lives on the snapshot (`snapshots.manifest`), in the `export.completed` event,
  and in the `exports` row's `manifest_sha256`; `bin/haddon-export --manifest PATH` writes it anywhere outside.
- **The export directory is exclusively managed.** `<year>.journal`, `accounts.journal` and `<year>-*.journal`
  are the only names touched; an unmanaged file in the target refuses the whole export, and an existing export is
  only replaced with `--overwrite`. The corpus checkout and `vendor/` are refused as targets outright
  (`protected_root_for`, a pure predicate so the real paths can be tested without a write ever being aimed at them).
- **An empty book is refused** rather than exported as an empty directory.

### What the store cannot carry back (for C2)

`entries` has no column for a transaction-level **code**, **comment** or **tags**, and `postings` has no status
column; C1 captured those only inside `journal_mappings.canonical_txn`. In the corpus's 2025 books all posting
statuses are `Unmarked` and all codes empty, so nothing is lost economically, but hundreds of ExampleInc transactions
(and a handful of ExampleBV ones) carry a source comment or tags that the exported journal does not reproduce. The oracle must
therefore compare the *modelled* subset, or the schema must grow those columns — which C2a will not do, because
migration 0001's SQL is sha256-pinned. Posting comments **are** carried: C1 stores them in `postings.memo`
(with hledger-reported tags appended), and the export re-emits them as `;` comment lines.

### Evidence

The corpus books, exported from the C1 imports: for ExampleInc and ExampleBV the entry, posting, account-commodity pair and
declared-account counts are each re-derived by reparsing the export with the pinned binary, not just pinned in
the test. Reparsing the export yields the **same posting multiset** (date, account,
commodity, exact amount) and the **same balances** as the source journal, for both entities. An entity whose import was
blocked has no book, so it has no export (404), and the corpus checkout and `vendor/hledger` are asserted byte-identical afterwards.

## Chunk C2b — the independent oracle (`haddon.oracle`)

C1 imports, C2a exports; this closes the loop. The oracle reads the same books three ways — **pinned source →
store → reparsed export** — and **exits nonzero when anything disagrees**. It is shipped twice, as
`tests/test_oracle.py` and as `bin/haddon-oracle` (`make oracle`, `make test-oracle`), and both fail the same way.

### The shape: a failing command with no partial state

- **A run never imports into the caller's database.** It migrates, imports, exports and compares inside a private
  workspace under `/tmp`, and copies the verified database to `--into` **only** when the report is `ok`. That is
  what makes "leave the target entity unimported — no partial state" true for *every* corruption, not just the
  ones the strict gate catches: a failing run has nothing to publish. `test_a_passing_run_does_publish` is the
  control, so "nothing was published" is never a publish path that simply never works.
- **`OracleReport.ok` is "every required gate ran and passed", not "nothing recorded a failure".** A run that dies
  half way through has no *failing* gate at all; `REQUIRED_GATES` makes the missing ones the failure
  (`test_a_report_is_not_ok_when_a_required_gate_never_ran`).
- **A disagreement is a failed gate, never an exception.** Import refusals, export refusals, a malformed hledger
  response and a broken journal all become a named failed gate, so the exit code is the single source of truth.
- **One extractor for two of the three views.** `canonical.from_journal` reads the source *and* the reparsed
  export. "Source equals export" through two bespoke walks would be weak; through one walk, plus the store read
  via `reports.postings.posted_postings` (the same query the export and the reports use), the loop is closed.

### What is pinned, and why the revision is not

`src/haddon/oracle/baselines.json` pins, per entity: entries, postings, account/commodity pairs,
account/commodity/month pairs, accounts, commodities, source files, the four unmodelled-field counts, the posting
status histogram, and four sha256 content digests — the canonical multiset, the account/commodity totals, the
monthly totals, and the `journal_mappings.canonical_txn` records.

The **source revision is deliberately not pinned**. The checkout is read-only and what matters is its *content*;
pinning the checkout's commit would make the oracle fail on any unrelated commit that never touched a 2025 journal, and would
make every corruption test pass for the trivial reason that a throwaway copy has a different HEAD. The digests are
the real pin, and they are re-derived inside the tests from the journals with the pinned binary
(`test_the_pinned_digests_are_derived_not_asserted`), so a baseline that had drifted could not pass by agreeing
with itself. Regenerating a digest is a claim that the source changed, and the file says so.

Which gate catches what is worth being explicit about: the **three-way comparison** catches a pipeline bug (the
import, the store, the render or the reparse disagreeing); the **baseline** catches source tampering, because a
corrupted journal moves source, store and export together and they go on agreeing with each other. Both are
required, so either one failing fails the run.

### The five gates the plan asked for, and where they landed

1. **Totals.** `totals` and `monthly_totals` enumerate every `(account, commodity)` and
   `(account, commodity, YYYY-MM)` key present in *any* of the three and compare all three values explicitly; a key
   one view simply lacks is a mismatch, not a skipped row. The enumerated key list is in the gate's `detail`, so a
   passing gate proves it looked at something.
2. **Counts.** `counts`: transaction and posting counts per entity, in all three.
3. **The canonical multiset.** `canonical` compares a `Counter` of whole transactions — exact date, narration,
   source status, and complete ordered legs of (account, commodity, exact integer micros). This is the gate that
   distinguishes an oracle from a totals check, and it is tested as such: `_same_month_date_swap` and
   `_compensating_leg_change` are built from the **corpus** ExampleBV book and asserted to leave every account/commodity
   total *and* every monthly total exactly as they were, and the full run is then shown to go red with `totals`,
   `monthly_totals` and `counts` still green.
4. **Coverage.** `coverage` is bidirectional and names its four failure modes separately: a source transaction with
   no row, a row with no source transaction, two rows sharing an entry id or a source index, and a posted entry
   with no row. It also compares the *record*: the stored `canonical_txn` must equal the one recomputed from the
   source through `importer.canonical_txn`, which is the "and back" direction and covers the fields the canonical
   multiset drops.
5. **Deliberate corruption.** `TestDeliberateCorruption` corrupts one thing at a time — a date (moved *within* its
   month, so no monthly total changes), an account (retargeted to another *declared* account, so the strict gate
   still passes), an amount (changed on *both* legs, so hledger still accepts it), a transaction tag, an evidence
   hash (one byte in a published artifact), and a malformed hledger response (garbage JSON, truncated JSON, and a
   nonzero exit). Each fails, each publishes nothing, and each names the gate that caught it. The real checkout is
   never edited: every source corruption goes into a throwaway copy under `/tmp`.

`posting_memos` is a sixth comparison the plan did not enumerate but "compare everything the store does model"
requires: `postings.memo` *is* modelled and the export re-emits it, so it is compared rather than excluded.
Source → store is exact. Store → reparsed export is exact but for **one ExampleInc posting** where the source had no
posting comment and an inherited transaction tag, which C1 stores as a memo and the export writes as a real
comment — hledger terminates a comment it wrote with a newline. That single normalisation is pinned
(`memo_normalised`), so it can neither grow nor hide a different difference.

### The known gap: enumerated, measured, and load-bearing (closed in E1)

`entries` had no column for a transaction **code**, **comment** or **tags**, and `postings` has no **status**
column. C1 captured all four only inside `journal_mappings.canonical_txn`; the export renders from `entries` and
`postings`, so a reparsed export could not reproduce them. The canonical record therefore left them out — and a
comparison that merely *excluded* them would be the papering-over the plan forbids. `haddon.oracle.gap` and the
`unmodelled_fields` gate instead asserted, all four ways: the schema was still exactly that shape; every stored
`canonical_txn` still carried all four fields; the **extent** was pinned (how many transactions per entity
carry a source comment and how many carry tags; none carried a code; every posting was `Unmarked`, so
nothing *economic* was lost); and the reparsed export reproduced **none** of them.

**Chunk E1 closed the first three** (see below). `postings.status` is still unmodelled and the gate that replaced
`unmodelled_fields` keeps measuring it.

### Disagreement with the plan: fewer account/commodity pairs than it states

The plan states a "full baseline" count of account/commodity pairs across the three entities. That figure
counted an entity whose journal failed `hledger check --strict` at this point and therefore had **no book at
all**: C1 refuses to build one, and C2a has nothing to export. The real number across the two books that existed
was the sum of their pairs, with no overlap because the two entities declare different commodities, and it was
lower than the plan's. No data is invented for the blocked entity to reach the plan's figure, and
no pair is counted twice to make the arithmetic work: a test derives the distinct count from the two stores
rather than asserting the plan's.

### The 2025/2026 boundary, on the corpus books

`tests/test_reports_year_scoping.py` proves no carried-balance inflation on synthetic fixtures; `run_carry_oracle`
proves it on the imported ones. Both books of one entity have to be in one database for that, which needed a small,
explicit extension to C1 (below). Four gates: both books imported; the mapped carry-forward against the 2026
opening actually booked; **zero opening P&L**; and **no carried-balance inflation** — the 2026 balances are that
book's own opening plus its own movements, and the gate also counts how many of them *would* have been inflated by
a cross-book sum (all of them, for both entities), so it cannot pass vacuously.

**ExampleInc verifies exactly**: 0 differences, `status='verified'`. Retained earnings after the fold is the 2025 closing
retained balance plus the 2025 net result, to the micro.

**An opening that does not match the prior closing.** Take a 2026 opening journal that says it was carried
forward from the final 2025 closing balance sheet, where `liabilities:corporate-tax-payable` nevertheless opens
2026 a few cents away from where it closed 2025, with the offsetting amount on `equity:retained-earnings`. The
two net to zero, so it is a pure reclassification between two
balance-sheet lines (no inflation, no P&L effect), but it is **undocumented**, and the spec is explicit that
unexplained differences block verification.

Handling: `verify_carry_forward` keeps reporting `status='failed'` and both differences, and the report **prints
them even on a passing gate**. At this stage a gate could pass over such a book only when the differences were
*exactly* pinned known-open items, byte for byte; one more, one fewer, or one micro different failed
(`test_an_open_item_that_moved_by_one_micro_fails`). Chunk F3 replaced the pinned items with a test that puts
the defect into a copy of the journals. The differences are **not** passed in as a
`reports.types.Reclassification`: a reclassification must cite its document, and the journals document none.
Inventing one would have been exactly the papering-over this chunk exists to prevent. Such a difference is a
finding for whoever keeps the books, not a defect in Haddon.

If a `to_year` journal is missing the run says so plainly and fails, rather than manufacturing an opening
(`test_a_missing_2026_journal_is_said_plainly_not_manufactured`). Both entities do have a 2026 book in the
checkout, and both pass `hledger check --strict`, so the proof is on corpus data rather than a synthetic case.

### The one change to C1: `import_entity(..., into_existing=True)`

A carry-forward is a statement about two books of one entity, and `import_entity` targeted an **empty** migrated
database. Rather than relax that rule, it gained an explicit opt-in. The default is unchanged — a populated
database is still refused with "already holds an entity" — and `into_existing=True` still refuses a different
entity (`entity_mismatch`), a year that already has a book (`book_exists`) and an empty database
(`not_provisioned`). The commodity list and the chart are entity-wide and only ever **grow**: a declaration already
stored is reused, one that *disagrees* (different precision, type or cash rule) is an error rather than an
overwrite, and the declared chart of the year being imported is still the only chart that year's postings may use,
so C1's "a posting on an undeclared account is a hard error" is untouched. `_counts` is now scoped to the book
year, so importing 2026 beside 2025 reports 2026's numbers rather than a running total (identical for a
single-year database, which is why C1's pinned counts did not move).

### Safety

- The protected-root guard is **reused, never disabled**: `runner._safe_target` refuses a workspace, a `--keep`
  directory or an `--into` target inside the corpus checkout or `vendor/`. It is probed against a stand-in root under
  `tmp_path` and by asserting the refusal for the real roots *without aiming a write at them* — a previous run
  disabled the guard to see what would happen and wrote a directory into the read-only checkout.
- The corpus checkout and `vendor/hledger` are asserted byte-identical after every oracle and carry run, and every
  workspace is asserted to be gone.

## Rename: `ledger` → `haddon` (2026-09-11)

The project is called **Haddon**. Repository `ledger` → `haddon`, package `src/ledger` → `src/haddon`,
`bin/ledger-{import,migrate}` → `bin/haddon-{import,migrate}`, `LedgerError` → `HaddonError`, and the
distribution/console-script names in `pyproject.toml`. Rename only — no behaviour changed.

Three things deliberately kept their old spelling:

- **The sha256-pinned migration SQL** (`src/haddon/db/migrations/sql/0001/*.sql`) is byte-frozen: migration
  0001 refuses to run if either file changes. An earlier rename edited comment lines inside
  those files and broke the pins. This rename excludes them entirely, so the pins still hold; the two stale
  `Ledger`/`ledger/db/connection.py` mentions in their header comments are the price of the freeze and are
  not worth a new migration. (Verified by probe: editing one comment character turns
  `test_sql_files_are_pinned_by_sha256` red and errors the four migration tests.)
- **`SCHEMA_VERSION = "ledger-proposal-1"`** (`domain/canonical.py`) is hashed into every proposal digest.
  It is a schema identity, not a package reference; renaming it would silently change stored hashes. It
  changes when the canonical shape changes, not when the project does.
- **The literal path segment `"ledger"`** in `importer.origin_for` / the import tests — that is a directory
  in the source corpus's layout (`<Entity>/<year>/ledger/*.journal`), not ours to rename. Likewise the
  English noun "ledger" where it means a book of account.

## Authorisation: the model records, it does not enforce (2026-09-11)

Two decisions taken together, with the reasoning for each.

### (a) Approval is no longer a precondition

The agent may post, reconcile, correct and close **on its own authority**. Auto-accept is the default path.
The owner or accountant gets **inspect-and-edit**, not a required signature. The reason is that a mandatory
counter-signature on every write makes the agent useless as a bookkeeper — it would stall on a human for
routine work it is trusted to do — while providing no guarantee the triggers do not already provide.

This is a **demotion, not a teardown**. Proposals and decisions survive as concepts and as rows:

- `proposals.propose_and_accept()` is the new ordinary path — propose and accept in one call, one actor. What
  was proposed, and *why* (`proposals.reason`), goes into the record, and who accepted it and when goes into
  `decisions`. A human reading the books afterwards can see the agent's reasoning, revise it (`supersedes_id`,
  which makes the earlier proposal `superseded` and keeps both readable) or reverse the posting.
- Splitting the two calls — propose now, decide later, possibly as somebody else — still works, and is what a
  deliberate review looks like. It is no longer what an ordinary write has to look like.
- `close`/`reopen` and `reverse_entry` still require a `decision_id`, and still check its **kind**
  (`approve`/`override`, plus `migration_accept` for closing only). What is gone is the requirement that some
  *other kind of party* minted it. An approval the agent wrote for itself authorises the close.

### (b) No code branches on actor type

The requirement was that Haddon not distinguish between humans and agents. It keeps a record of who did
what, whichever kind of party that was, and needs no check that an actor is an identifiable human.

Deleted: `ctx.is_human` / `ctx.HUMAN_PREFIX`, the `HUMAN_ONLY` / `human_only` error code and its 403 mapping,
the gate in `proposals.decide`, and the `is_human(r.reviewer)` conjunct in `closing._decision_ok` and
`corrections._authorising_decision`. There was no schema `CHECK` to remove — the gate was Python only.

`actor` is now **one opaque string** on every event and state change, with exactly one constraint, in
`ctx.check_actor`: **present and non-empty**. Nothing else is validated and nothing is interpreted.
`agent:` / `human:` remains a readability convention *in the values written*; nothing reads it and nothing
enforces it. `tests/test_authorisation_model.py::test_no_code_reads_an_actor_prefix` is a structural guard that
fails if anyone reintroduces such a branch anywhere in the package.

### What this deliberately did not change

Removing the gate removes a checkpoint, not a guarantee. The diff for this change touches five source files,
all in `domain/errors.py` and `repository/{ctx,proposals,closing,corrections}.py`. `db/` (the trigger suite and
schema), `repository/entries.py`, `repository/transaction.py`, `repository/events.py` and `importing/` are
**byte-identical**. So every balance, immutability, period-lock and seal trigger, the whole forbidden-mutation
matrix, correction-posts-an-opposite-entry-with-the-original-left-standing, `void` only for never-booked drafts,
and the hledger verifier on every write all stand exactly as they did.

The audit trail matters **more** now, not less, because it is the only record of authority that exists. An actor
is never widened to a generic value to simplify a code path.

### The plan document is now out of date here

The original plan is the historical spec and is **not** edited. It is wrong wherever it
assumes a mandatory human gate — specifically its "identifiable human" requirement on approve / close /
chart-edit. Read it with this section as the correction.

## Chunk D1 — the REST spine and read surface (`haddon.api`)

`src/haddon/api/` serves `/v1` over the library: the spine (entity addressing, credentials, the error envelope,
idempotency, pagination, year scoping, OpenAPI), fifteen read endpoints, and exactly one write. The write/job
side (applications, operations, export-as-a-job, close/lock, uploads) is deliberately **not** here.

### Divergence from the plan: approval is not a precondition

The original plan describes the proposal surface as a gate — "human approval required",
"agents may prepare, humans accept", `/proposals/{id}/{approve,reject,override}` as decisions only an
identifiable human may mint, and a `human_only` 403. **That model was removed on 2026-09-11** (see
"Authorisation: the model records, it does not enforce" above). The plan is the historical spec and is not
edited; where it assumes a mandatory counter-signature it is out of date, and this chunk implements the current
model:

- `POST /v1/entities/{e}/proposals` takes `accept`, **defaulting to true**, and calls
  `proposals.propose_and_accept` — one actor, one call, proposal and decision both written. What was proposed,
  *why* (`reason`), and who accepted it and when, all land in the record.
- `"accept": false` calls `create_proposal` alone and leaves the proposal `awaiting_approval`. Splitting propose
  from decide still works and is what a deliberate review looks like; it is no longer what an ordinary write has
  to look like.
- No `/proposals/{id}/{approve,reject,override}` route exists yet. When one lands it will be a decision by
  whoever calls it, bound to the exact hash and version, with no actor-kind check — `proposals.decide` already
  behaves that way.
- Nothing in `haddon/api/` reads an actor string. `security.Principal` carries it and `WriteCtx` records it;
  `capabilities.actor_recorded_as` reports it back to its owner and nothing branches on it.
  `tests/test_authorisation_model.py::test_no_code_reads_an_actor_prefix` covers this package too.

### Three defects this chunk found and fixed

**Reads were taking the write lock.** `make_engine` installs a `begin` listener that emits `BEGIN IMMEDIATE` —
correct for `write_transaction`, wrong for a read surface built on the same engine: *every* `GET` took SQLite's
exclusive write lock, so concurrent reads serialised behind one another and a single held-open read connection
blocked the writer entirely. Found by a test fixture that held a reader open while the app served a request;
it failed with `database is locked`. `make_engine` now takes `immediate: bool = True` (existing behaviour
unchanged) and `Registry` opens **two** engines per entity: the writer, and a deferred reader that under WAL
gives a consistent snapshot without excluding anyone. `test_a_read_does_not_take_the_write_lock` and
`test_concurrent_reads_are_served_in_parallel` pin it.

**The oracle published a database by copying its file (chunk C2b).** `runner._publish` did
`shutil.copy(workspace/<entity>.db, into)`. The workspace database is in WAL mode, so pages committed since the
last checkpoint live in the `-wal` sibling and not in the file; a file copy leaves them behind. SQLite folds the
log back on last-connection close, and at a commit once it passes a thousand pages, but skips both while any
connection is reading — and whether one is reading depends on when the interpreter last collected a reference
cycle. So the oracle shipped a complete database almost always and an **empty** one occasionally, on a report
that said `ok`, because every gate read the workspace through a connection that could see main *and* WAL and
nothing noticed. This chunk's tests changed the allocation pattern enough to make it surface:
`test_a_passing_run_does_publish` failed with `KeyError: 'entries'` — `table_counts` on a published database
with no tables at all. Publishing now goes through `_copy_database`, which uses SQLite's **backup API**: it
reads through the pager, so the copy includes the WAL and is consistent against concurrent access, and no
checkpoint has to have happened. `PRAGMA wal_checkpoint` was rejected as the fix because it reports busy and
carries on when a reader is active, which is exactly the case that was failing.
`test_publishing_copies_the_database_and_not_merely_its_file` pins the condition directly — it holds a
connection open so the log is never folded back, shows a plain file copy yielding zero tables as the control,
and requires the backup copy to be complete — rather than waiting for the timing to recur.

**`proposal_hash` does not cover `reason`.** Two callers submitting proposals that differ only in `reason`
collide on `immutable` ("an identical proposal already exists") in `create_proposal`, *before* idempotency is
consulted. This is by design — a proposal is content-addressed by what it proposes, not by its prose — but it is
surprising at the HTTP edge, and it is why
`test_the_key_is_scoped_to_the_caller` varies `subject` rather than `reason`. Not changed: altering the hash
would change every stored `content_hash` and every decision bound to one.

### Two error codes added to `domain/errors.py`

The spec's 409 table lists `cursor_expired` and `event_gap`; `STATUS_OF` did not have them. Both are now in the
domain table at 409, next to the rest. Nothing else in `domain/errors.py` changed. `limit_exceeded` (413) and
`temporarily_unavailable` (503) are still absent because nothing in this chunk raises them: an over-sized page is
`422 validation` with the advertised maximum in `details` (consistent with `events.read_events`), and `/readyz`
answers 503 with a readiness body rather than an error envelope, because a probe wants checks, not a code.

### Pagination: the watermark and the probe

`{items, next_cursor, snapshot_seq}`; `snapshot_seq` is the entity's **event high-water mark** — the state
sequence, which moves on every write. Cursors are `base64url(canonical_json(payload)) + "." + HMAC-SHA256`,
signed with a key from the configured keyring (`cursor_keys`; superseded the process-local `cursor_secret`
default — see "Cursors are signed with a configured keyring" under issue 0008 section A). Signing is what makes
"opaque" enforceable: a cursor edited by anyone else is
refused rather than honoured, so a page cannot be widened, re-pointed at another entity or spliced onto another
query.

Two mechanisms, doing different jobs:

- **the watermark** — page one records the ordering key of the last row then present; every later page excludes
  anything sorting after it. So a row appended mid-listing is invisible to the walk in progress and visible to
  the next one, which is the case pagination must *survive* rather than refuse. It is the ordering key, not a
  rowid, because `VACUUM` renumbers rowids (the same reason `reports.postings` does not order by one).
- **the probe** — each page recomputes a fingerprint of exactly the rows the cursor claims to cover: their count,
  the sum of their `version` columns where the rows are mutable, and the row counts of tables a *derived* field
  reads. A mismatch is `409 cursor_expired` with a resync link. So archiving an account expires an in-flight
  `/accounts` cursor (its `version` moved), and writing a decision expires an in-flight `/proposals` cursor
  (`status` is derived from `decisions`/`applications`/successors, never stored), while appending an entry
  expires nothing — the watermark already excluded it.

Ordering is `(created_at, id)`. `created_at` is second-resolution and not unique, which is why the id is always
the last component of both the key and the watermark.

The **register** is the exception, and pre-page history is why. `reports.register` orders
`(dt, entry_id, posting_seq)` but breaks ties within a date on entry `created_at` *before* id — a stable order
that is not lexicographic in the published key. So the register is materialised for the whole scoped range and
sliced by the **position** of the cursor's key in that ordered list, never by "everything greater than it". The
range handed to `reports.register` always starts at the book's first day, whatever `from` says, so every line's
running balance was accumulated from the start of the book; `opening_micros` is the per-commodity running total
immediately before the window, i.e. exactly what the page does not show. A register recomputed from `from`, or
from the cursor position, produces a plausible column of numbers that is wrong from the second page onwards —
`test_running_balances_include_pre_page_history` and `test_a_window_reports_the_balance_carried_into_it` are the
two tests that catch it.

`/events` does not use cursors: it is `since`/`limit` with `next_since` and the high-water mark. `since` beyond
the high-water mark is `409 event_gap` with a resync URL — a follower holding a sequence this log does not
contain is reading from a restored or different file, and serving it an empty page would let it wait forever.
The "pruned" branch (`since` below the lowest sequence present) is checked too; this log never prunes, so it is
unreachable today and is **not** covered by a test.

### Year scoping at the edge

`year` is **required** on the register and on all three reports, because those produce totals and a query
spanning two books double-counts the carried opening. It is an optional **filter** on `/entries`, which is a list
and adds nothing up. A range crossing a book boundary is refused by `reports.scope.date_range_in_book`;
`api/scoping.py` turns its per-year breakdown into a URL per year, so the refusal is actionable. Dates are taken
inclusive and passed through untouched — the conversion to hledger's exclusive end stays in the export adapter.

### Idempotency

`repository/requests.py` is wired, not reimplemented. The hash binds canonical method, path, body, caller and
entity; the body is canonicalised as JSON so a retry that re-serialises the same intent still replays. Ordering
inside the route matters and is deliberate: **replay-or-conflict first, then the wire-number check, then the
schema.** A replay must return the stored original even if the body would no longer validate, and a second use
of one key with a *different* body must be `409 idempotency_conflict` rather than whatever that other body
happens to fail on. An unparseable body still hashes (as its own bytes), so the comparison is total.

The check and the write both happen under `BEGIN IMMEDIATE`, and `record_request` runs in the same transaction as
the mutation — both rows or neither. A refused write therefore leaves no request row and does **not** burn the
caller's key.

The header requirement is a **middleware**, not a per-route dependency, so it answers before routing: a mutating
verb on a read-only path reports the missing key rather than 405. That is the property that makes a mutating
route added later covered by default instead of by remembering, and
`test_the_idempotency_gate_is_ahead_of_routing_so_no_mutating_route_can_miss_it` is what fails if the middleware
is removed.

### Credentials

A static token maps to one opaque actor and an explicit entity list, loaded from the config file. Tokens are
compared with `hmac.compare_digest` against every configured credential without an early exit. Resolution order
is 401 (no usable credential) → 404 (an entity this service does not serve) → 403 (a real credential out of
scope), which keeps "not found" and "not yours" from leaking into each other. Inventing an identity provider was
explicitly out of scope; when one arrives it replaces `security.authenticate` and nothing downstream changes,
because nothing downstream reads the actor.

### Amounts

`amount_micros` is a base-10 integer string in **both** directions. Outbound, every amount goes through
`wire.micros`. Inbound, `wire.reject_wire_numbers` walks the whole body before anything else looks at it and
refuses a float anywhere (whatever the key) and any `*_micros` value that is not a canonical integer string —
which includes a JSON integer, because `12000000` and `"12000000"` are not the same claim. A sweep test walks
every response body of every endpoint and asserts no float appears and every amount is a string.

### Things deliberately not done

- **No artifact bytes.** `/exports/{id}` describes artifacts (path, sha256, byte count); it does not serve them.
  An authenticated download surface has its own rules and this chunk does not open one.
- **No `/sources`, `/source-files`, `/statement-lines`, `/matches`, `/rules`, `/reconciliation`,
  `/close-checks`, `/operations`.** They belong to the write/job chunk; several have no repository behind them yet.
- **`/entries` POST is not the proposal endpoint.** The plan overloads `POST /entries` as proposal submission;
  there is one proposal endpoint and it is `/proposals`.
- **The `status` filter on `/proposals` is applied to the derived status**, after the page is selected, because
  status is not a column. A page may therefore return fewer items than `limit` and still have a `next_cursor`;
  that is ordinary keyset behaviour, not a truncated result.
- **`/v1/entities` skips an entity whose file holds a different id** rather than failing the whole listing;
  `/readyz` is where that misconfiguration is reported.

## Chunk E1 — migration 0002: the transaction note fields

C1 imported the corpus journals exactly, but `entries` had no column for a transaction-level `code`,
`comment` or `tags`, so C1 could only keep them inside `journal_mappings.canonical_txn`. The deterministic export
(C2a) renders from `entries`/`postings`, so it could not reproduce them and **source → store → export was lossy**
for every transaction comment and tag set in the corpus books. Migration **0002** closes that.

### The representation, and why

| column | holds | empty form | why |
|---|---|---|---|
| `code` | hledger `tcode` | `''` | hledger's own model is an empty string, not an absent value: "no code" is not "unknown". |
| `comment` | hledger `tcomment`, verbatim | `''` | element 0 of its `\n`-separated lines is the comment on the transaction's own line, the rest are the comment lines above the first posting — the same shape `postings.memo` already uses. |
| `tags` | canonical JSON, `[["name","value"], …]` | `'[]'` | `json_valid` is a column CHECK. |

All three are `TEXT NOT NULL DEFAULT`, so `ALTER TABLE ADD COLUMN` is legal on the existing rows, every read
totals without a `COALESCE`, and there is no third "unknown" state to reason about.

**Tag order is part of the value.** hledger derives `ttags` by parsing the comment, so the order is a
deterministic function of `comment`; sorting or de-duplicating would make a reparse of the export disagree with
the store, and a transaction may legitimately carry the same tag name twice.
`test_probe_a_reordered_tag_list_is_caught` shows the disagreement a reordering produces.

**The export writes no tags of its own.** It writes the `comment`, and hledger re-derives the tags from it. That
is why the renderer does not re-implement hledger's tag parser: it refuses a stored tag whose name the comment
does not contain (there would be nowhere to write it) and the *sufficient* claim — that the comment reproduces
exactly these tags — is proved by reparsing the export with the pinned binary, in
`test_a_comment_reparses_to_exactly_what_was_stored` and in the oracle's `round_trip` and `canonical` gates.

### Two refusals the round trip forced

* A **narration starting with `(`** is now `UnrenderableContent`: written out it would reparse as the transaction
  *code* (`test_a_leading_paren_narration_really_would_be_read_as_a_code` shows hledger doing exactly that with a
  hand-written journal). Neither corpus book has one; this was a latent C2a hole that modelling `code` exposed.
* A **tag whose name the comment does not carry** is refused rather than dropped.

### Migrating an existing database

0002 runs three steps in one transaction: drop `entries_update`, add the columns, backfill, recreate
`entries_update`. The trigger must go first, because it refuses **every** update of a non-draft entry — which is
exactly the immutability this migration must not weaken, and which would otherwise refuse the backfill of a book
whose entries are all posted.

The backfill reads `journal_mappings.canonical_txn` — the full-fidelity record C1 wrote — and re-renders the tags
with the same `canonical_json` the importer now uses, so a backfilled row and a freshly imported one are
byte-identical. A record that cannot supply the fields is a **hard error**, not three empty columns: an empty
column would be the silent loss the whole chunk exists to prevent.
`test_a_0001_database_holding_a_book_migrates_forward_with_nothing_lost` imports the corpus ExampleInc book, downgrades to
0001 (the only way a 0001 database holding a book can exist now), upgrades again, and asserts every one of its
entries is back byte for byte and that the **exported artifacts hash identically** to a fresh import at 0002.

### Immutability

`entries_update` is recreated, not edited in place, and differs from 0001's by exactly three conjuncts —
`NEW.code IS OLD.code AND NEW.comment IS OLD.comment AND NEW.tags IS OLD.tags` — added to the D4 exemption that
lets a *locked draft* be voided. Without them, a locked draft could be voided and annotated in one statement.
A posted entry needed no new rule: the trigger's first line already refuses any update whose `OLD.status` is not
`draft`. `test_the_0002_trigger_is_0001s_with_only_the_new_columns_added` asserts the difference is exactly that,
and the mutation matrix carries six new cases (three posted columns, three locked-draft-void variants).

A side effect worth knowing: SQLite refuses `ALTER TABLE entries DROP COLUMN comment` while `entries_update`
names the column, so the trigger makes the new columns undroppable by accident.

### The oracle: `unmodelled_fields` deleted, `round_trip` in its place

The C2b gate asserted the gap — that the export reproduced **none** of these fields. With the gap closed that
assertion is false, and an assertion that a field is lost is exactly the thing that would have to be weakened to
let a fix land, so it was **deleted** rather than relaxed. What replaced it is strictly stronger:

* `canonical` (the multiset gate) now carries each transaction's `code`, `comment` and `tags`, so the three
  representations are compared **value by value**: a changed comment or an edited tag value is a different
  element, in whichever representation it happened.
* `round_trip` is the counting half: the source's counts, the `entries` **columns**' counts and the **reparsed
  export**'s counts must all be equal; the source's and the reparsed export's posting-status histograms must be
  equal (the one field still unmodelled — see below); `canonical_txn` must still carry the full record, one per
  source transaction; and the schema must still have every column this depends on, both ways round.

`canonical_sha256` was regenerated once, in this chunk, because the **record widened** — not because the source
changed. `mapping_sha256`, over `journal_mappings.canonical_txn`, is byte-for-byte what C2b measured, which is
what shows C1's record is untouched. `baselines.json` records that.

### What is still not modelled

`postings.status` (hledger `pstatus`). Every posting in both corpus books is `Unmarked`, so the round trip is
lossless today, and `round_trip` proves that by comparing histograms rather than assuming it — a journal with a
`!` or `*` posting would fail the gate, which is correct: it would be a real loss. Closing it is another
migration. The importer does not yet refuse such a posting; the oracle is what catches it.

### Confirmed counts

The number of transactions carrying a comment, carrying tags and carrying a code (none) is the same in the
source, in the `entries` columns after import, and in the reparsed export, for both entities. All three numbers
land exactly as the plan expected.

## Chunk E2 — close eligibility: a month closes only when it is complete

The requirement: the agent may close a month, but a month should close only when everything it needs has been
provided. `close` keeps closing on the agent's **own authority** — no human signature, no
approval button, nothing here reads an actor (the 2026-09-11 "Authorisation" section above stands unchanged) —
but it now **refuses** a month that is not complete, and the refusal is actionable rather than a boolean.

### What changed, and what deliberately did not

`src/haddon/repository/close_checks.py` is new and owns everything about eligibility. `closing.close` gained
**one** line of precondition (`require_eligible`) plus one audit event, and an `overrides` argument. Every
mechanic that was already there is untouched and still refuses **first**, with its own code: the lock version
check, `close must advance`, the snapshot-through match, the authorising-decision kind, reopen-can-never-clear-
the-lock (U7), the `close_record_required` trigger chain. `tests/test_close_eligibility.py::
test_eligibility_is_checked_after_every_mechanic_that_was_already_there` pins that order.

**`reopen` is not gated on eligibility, on purpose.** Reopening is how an incomplete month gets fixed; the month
that most needs reopening is exactly the one that cannot pass the gates, so gating it would trap the books shut.

`db/` is byte-identical: **no migration 0003 was needed**. The override mechanism is built out of rows the schema
already has (`proposals` + `decisions` + `events`), which is also why it is auditable for free.

### The three gates

| key | requirement | wired against |
|---|---|---|
| `statement_coverage` | every statement covering the month is loaded, and its printed closing balance agrees with our ledger | `statement_scopes`, `statement_assertions`, `reports.balances.account_balance` — **fully wired** |
| `nothing_unexplained` | nothing in the month is still unexplained or unclassified | draft `entries` (wired); unmatched/undispositioned `source_rows` (wired, gated on the substrate rule below) |
| `nothing_partial` | nothing in the month is partially matched or partially allocated | partial sealed `matches` per row and per posting, and unsealed `match_sets` (wired, same substrate rule) |

The window is the span the close **newly** closes — the day after the current lock through the close date, or
from the first day of the earliest book when nothing is closed yet — split into calendar months, each clipped to
the window. Inactive-looking months are demanded exactly like active ones, and a mid-month close asks for a
part-month. The expectation set for gate 1 is **every cash account in the chart × its declared commodities**,
archived included: the plan wants the *configured* expected accounts (`source_configs`), nothing writes that
table yet, and the chart is the wider set — which is the safe direction, it demands more evidence, never less.

### Fail closed — the rule the chunk is really about

A gate answers `pass` **only** when it has positively established completeness. Everything else blocks:

1. a real finding (`fail`);
2. a component it cannot evaluate (`unevaluable`);
3. a gate that **raised** — including a `HaddonError`; a gate that could not read what it needs has not
   established anything;
4. a gate that returned a status that is not one of the three;
5. a key in `REQUIRED_GATE_KEYS` with **no registered implementation at all**.

`unevaluable` deliberately outranks `fail` when a gate's components are combined: "I could not tell" is the
weaker claim, and the caller must be told that rather than be handed a list that reads like the whole problem.
An unevaluable gate's `reason` begins *"could not be evaluated"* and its items are `kind="gate_unevaluable"` —
it never dresses an empty query up as a specific finding. All five paths are pinned by
`test_an_unevaluable_gate_blocks_the_close_and_never_silently_passes` and
`test_an_unevaluable_gate_says_so_and_does_not_invent_a_specific_finding`.

**The substrate rule** is how gates 2 and 3 avoid the subtler failure — passing vacuously. Source rows, matches
and dispositions only exist under a loaded statement scope, so a cell (account, commodity, month) whose scope
coverage is incomplete is reported `unevaluable` for those gates: *"I found nothing"* is not *"there is
nothing"*. This is a property of the data, not a switch. The moment the Phase 1 workflow chunk stages scopes and
rows, those cells begin evaluating for real with **no edit to `close` and none to the gates** — which is exactly
what `test_statement_coverage_passes_only_when_loaded_asserted_and_agreeing` and
`test_the_source_side_gates_pass_once_everything_is_explained_and_sealed` demonstrate, against chunk A's seeded
workflow rows: supply what a gate asks for and it goes green, break it again and it goes red.

An entity with **no cash account at all** is `unevaluable` on all three, not a pass. Nothing to check is not the
same as everything checked.

### The refusal shape

`close_not_eligible` (new code, 409) carries one detail per blocking gate: `gate`, `title`, `requirement`,
`status`, `reason`, `blocking`, `waiver`, and `outstanding` — a list of `{kind, message, detail}` where `kind` is
a stable token an agent branches on, `message` is the sentence a human reads, and `detail` carries account id and
path, month, commodity, the specific row/posting/entry/assertion ids and **every amount as an integer micro
string** (`asserted_micros`, `ledger_micros`, `difference_micros`, `unmatched_micros`, …). Past
`MAX_OUTSTANDING` (100) per gate the rest are counted in `outstanding_truncated`, not dropped silently.

### The override: recorded, scoped, visible — never a flag

Because every gate fails closed until Phase 1 lands, `close` would otherwise be impossible. The way through is
an **explicit recorded override**, and its shape is the point:

* `close_checks.record_gate_override(ctx, gate=…, through=…, reason=…)` writes a `close_gate_override`
  **proposal** whose subject is `{gate, through}` and whose payload carries the gate's status, reason and the
  **outstanding items as they stood**; an **`override` decision** carrying the acting principal and the reason;
  and a `close.override_recorded` **event**. `override_blocking_gates(...)` is the convenience that does this
  once per currently-blocking gate.
* It is **scoped to one gate and one close date**. A later close re-evaluates from scratch and needs its own
  overrides. An override for another date, an `approve` decision, a decision on a non-override proposal, or an
  override whose proposal has since been revised are each a refusal, not a shrug.
* It **never rewrites a gate's status**. `CloseChecks.eligible` (the books are complete) stays false; only
  `may_close` (a close is permitted right now) becomes true, and the real finding stays visible beside the waiver.
* Every `close` writes a `close.eligibility` event on the close record carrying what every gate found and which
  gates were waived by which decision.

There is no environment variable and no boolean that turns the gates off, and there is no code path that closes
without either passing a gate or leaving a named, reasoned, dated record of why it did not. A month closed on
exception is legible years later; a flag would leave nothing to read.

Existing tests that close a month now record overrides (`repo_helpers.lock_books`, four tests in
`test_repo_closing.py`, three closes in `test_authorisation_model.py`). No assertion was weakened — each of them
gained the override step and kept every check it had.

### Read-only inspection

`close_checks(conn, entity_id, through=…)` takes a `Connection`, not a `WriteCtx`, and writes nothing;
`GET /v1/entities/{e}/close-checks?through=…&override=…` is its face on the D1 surface (listed in
`/capabilities`). Both are proved read-only two ways: no row count moves, and the whole evaluation completes on a
connection with `PRAGMA query_only=ON` that demonstrably refuses a write.

### What of the plan's fuller list was left for later

The plan's "Close checks enumerate…" paragraph wants more than the three required here. Implemented: current statement
coverage, the confirmed printed closing balance, months including inactive-looking ones, zero unexplained ledger
and source residuals, zero partial matches/allocations. **Deliberately left for a later chunk**, each of which is
a new `Gate` subclass plus a `register_gate` call and nothing else:

* **configured expected cash accounts and months** from `source_configs` (`active_from`/`active_to`) rather than
  the chart — the chart is today's conservative stand-in;
* **valid opening proof** — `opening_assertions` / `opening_verifications` exist and are unread here; an opening
  that does not chain from the prior closing is the case this gate is for;
* **a verified snapshot export** — `exports` + the pinned verifier, tying the close to bytes that passed
  `hledger check --strict`;
* **zero unclassified or unresolved period proposals** (excluding the close proposal itself) — `proposals.subject`
  is opaque JSON today, so there is no honest way to scope "proposals of this month" yet;
* **explicitly explained timing items** remaining listed with their evidence;
* **the dependency-hash recheck under the write lock** — `close` already runs inside one `write_transaction` with
  the lock's optimistic version check, but it does not yet compare a proposal's dependency hash.

The plan's *"a zero balance difference alone never means complete"* is the sentence this whole design is built
around: gate 1's balance agreement is one of **three** components, and agreement on its own never produces a pass
while coverage is missing or a source row is unexplained.

## Chunk E3 — two test tiers, split by measurement

A full suite plus a full oracle run is about fifteen minutes, and a chunk runs several. This chunk makes the
*iteration* loop fast without touching the gate: `make test-fast` is what you run between commits,
`make test-gate` is the complete suite plus `bin/haddon-oracle all` and is the only thing that authorises a
commit. Nothing about what a test asserts changed; 958 tests became 960 (the two added here) and not one was
deleted, skipped or weakened.

### Where the time actually was, and why `--durations` is misleading here

`--durations=0` over the whole suite says test X took 22 s. It is nearly always lying about *which* test: a
session- or module-scoped fixture is charged to whichever test happened to request it first, which is an
artefact of collection order. Instrumenting `pytest_fixture_setup` instead shows the real shape — of the
**460 s** suite, **160 s (35%)** is thirteen shared fixture builds:

| fixture | build | tests requiring it |
|---|---:|---:|
| `module:carry_inc` (test_oracle) | 33.8 s | 5 |
| `module:oracle_inc` (test_oracle) | 33.3 s | 12 |
| `module:round_tripped` (test_entry_note_fields) | 27.9 s | 4 |
| `module:inc_at_0001` (test_entry_note_fields) | 22.2 s | 2 |
| `session:imported_inc` (conftest) | 22.2 s | 114 |
| `module:oracle_bv`, `carry_bv`, `two_year_bv`, … | 0.4–8.0 s | 1–16 |
| `session:templates`, `migrated_template`, `provisioned_template` | 0.19 / 0.09 / 0.06 s | 359 / 293 / 237 |

The last row is the point: **chunk B's per-session template scheme is already right and was left alone.** All
three templates together cost 0.34 s to build and serve 359, 293 and 237 tests. The expense is entirely the
corpus work — importing the corpus books through the pinned hledger binary, and the oracle running the
whole pipeline three ways.

This is also why a naive duration split *does not work*. Deselecting the 114 tests measured at 0.3 s or more
left a "fast" tier of 844 tests that still took **198 s**, because the shared fixtures those tests were charged
with were still required by tests that remained. The cost lives in the fixture graph, not in the test.

### The threshold: 0.5 s, and why it is not a gap

A test is `slow` when either

1. its **own** measured cost (setup + call + teardown, with shared fixture builds subtracted) is ≥ 0.5 s, or
2. it requires a shared fixture whose **build** is ≥ 0.5 s — you cannot run such a test without paying that
   build in full, so the fixture's cost is the test's cost.

There is **no gap in the distribution** to put a threshold in, and it would have been dishonest to claim one:
across ranks 40–300 of the sorted per-test durations the largest ratio between neighbours is **1.20×**, and on
the shared-fixture-attributed metric it is **1.10×**. The curve is smooth. So the threshold is a budget
decision, made on two measured facts:

* **Mechanism.** Apart from one test, nothing that stays in-process costs more than **0.408 s**
  (`test_src_is_float_free_on_the_amount_path`), and everything at 0.5 s or more either shells out to the
  pinned hledger binary or carries a corpus book. The exception is `test_d2_seal_cost_does_not_grow_with_sealed_history`
  (0.67 s), the suite's one deliberate performance test — and it was already marked `slow` before this chunk.
  So 0.5 s is close to being a statement about *mechanism*: it is roughly where a test stops being in-process.
* **Budget.** Predicted fast-tier wall by threshold: 0.2 s → 17.4 s, 0.3 s → 22.9 s, **0.5 s → 27.6 s**,
  0.75 s → 30.7 s, 1.0 s → 42.3 s. Each step up buys back fewer tests for more seconds (0.3→0.5 s costs 4.7 s
  for 12 tests; 0.75→1.0 s costs 11.6 s for 13). 0.5 s keeps **75%** of the suite in a tier that still runs in
  under half a minute.

Every test function that qualified qualified in **all** of its parameters, so the marks are plain
`@pytest.mark.slow` decorators and two module-level `pytestmark`s — no `pytest.param(..., marks=...)` anywhere.
**243 slow, 717 fast.** `test_api_spine.py` and `test_api_read_surface.py` went slow as whole modules: every
one of their 98 tests requires `imported_inc`, so one of them in the fast tier costs 22.2 s.

### The completeness check, and what it is really defending

`tests/test_tier_completeness.py` reads `FAST_SELECT` and `SLOW_SELECT` **out of the Makefile** — the single
place the tiers are defined — and proves by *actually collecting* three times that
`fast ∪ slow == everything` and `fast ∩ slow == ∅`. It is not a maintained list of node ids; it is the real
collection, so it holds for any way of narrowing a run (a marker expression, a `-k`, an `--ignore`, a path),
not only for the marker pair that happens to be there today.

With the pair that *is* there — `not slow` and `slow` — no marker mistake can orphan a test, because those two
expressions partition by construction. The check therefore is not defending against a typo'd decorator; it is
defending the **tier definitions**. The moment someone writes `-m "not slow and not flaky"` or adds an
`--ignore` to make the fast tier faster, tests silently stop being run by either command, and this test says
which ones. Both hazards were probed and both go red, naming the orphans.

A second test pins the thing that must not move: `test-gate`'s recipe must invoke pytest with **no** narrowing
flag and no path, and must run `bin/haddon-oracle all`.

`test_the_two_tiers_between_them_run_every_test_in_the_suite` costs **4.1 s** — above the 0.5 s threshold, and
deliberately left in the fast tier anyway. It is the fast tier's own completeness proof; running it only at the
gate would let the fast tier shrink silently for a whole working session. That is the one stated exception to
the rule, and it is the reason the fast tier is 32 s rather than 28 s.

### `pytest-xdist`: measured, and not wired in

The development machine has 2 cores. `pytest-xdist` is recorded in `pyproject.toml`'s `dev` extra and installed, and then
**not used by either default command**, because measurement did not support it:

| run | serial | `-n 2` | `-n auto` |
|---|---:|---:|---:|
| fast tier (717 tests) | **26.4 s** | 26.97 s | 27.42 s |
| full suite (960 tests) | **461.0 s** | 434.3 s (`--dist load`) | 408.2 s (`--dist loadfile`) |

Two workers are *slower* than one on the fast tier. That tier is 717 cheap in-process tests whose total work
is ~25 s; worker startup, a second interpreter importing fastapi and sqlalchemy, and a second build of the
session templates cost more than the parallelism returns. The negative result is the answer, and the fast tier
stays serial.

On the full suite `-n 2 --dist loadfile` is genuinely faster — **11.5%**, 461 s → 408 s — but the gate was left
serial on purpose. `loadfile` still rebuilds `imported_inc` on both workers, and the oracle tests build private
workspaces under `/tmp` and publish; running them beside anything else changes the gate's execution model for
a ninth of its runtime. The gate's job is to be trustworthy, not quick. `-n 2 --dist loadfile` is there to be
used ad hoc when you want the whole suite and not the oracle.

### Results

All wall-clock, on that 2-core machine, `.venv/bin/pytest`:

| | before this chunk | after |
|---|---:|---:|
| fast tier | 246.8 s — 867 tests (only 4 modules/tests carried `slow`) | **29.9 s — 717 tests** |
| full suite | 461.0 s — 958 tests | 475.3 s — 960 tests |
| full suite + `bin/haddon-oracle all` (**the commit gate**) | ~535 s (derived) | **549.6 s, exit 0** |

**8.3× on the iteration loop; the gate is unchanged, which is the whole point.** The "before" fast tier was
measured by reproducing the four `slow` marks that existed before this chunk exactly (`--ignore` the three fully-marked modules and
`--deselect` the one marked test) — 867 tests rather than 865 because this chunk's two new tests are in it.
The before-gate figure is derived, not measured as one command: 461.0 s plus the oracle measured standalone
at 74.0 s. The full suite is 14 s slower than baseline because this chunk added two tests to it (4.1 s) and
run-to-run variance on that machine is a few seconds either way.

## Chunk F1 — the generic statement input

The architecture decision this chunk implements: keep the ledger general enough for any company to use. An agent
can always convert what a bank produces into the right import format, so the ledger needs no importers and nothing
specific to a bank or an account.

So **Haddon contains no bank-specific and no company-specific code, and no reader.** One generic statement
submission — an account, a period, an opening and a closing balance, a list of normalised lines, and the original
document — strictly validated. Whatever the bank actually produced (CSV, XLSX, PDF, a pasted table) is read
*outside* the ledger by an agent; what arrives is already normalised.

**Generality here meant fewer concepts, not more.** There is no plugin system, no format registry, no
configurable pipeline and no per-institution anything. The whole input surface is one dataclass
(`statements.Line`), one document reference (`statements.Document`) and one function
(`statements.submit_statement`).

### The four rules, and where each one lives

`src/haddon/repository/statements.py`. A submission that breaks any of them is **refused whole** — there is no
half-loaded statement, and every refusal test in `tests/test_statement_input.py` compares a full table-count
snapshot taken before it.

1. **The lines sum to closing minus opening** → `422 unbalanced`, detail
   `reason: statement_does_not_balance` with `lines_micros`, `expected_micros` and `difference_micros`. This is
   the check that catches a dropped, duplicated or misparsed line *without knowing the format*, which is exactly
   what a ledger with no reader needs.
2. **Every line's date falls inside the declared period** → `422 validation`,
   `reason: line_outside_period`. The `rows_insert` trigger already enforced this (`scope_invalid`); the Python
   check is the D6 pre-validation in front of it so the refusal names the line and the period rather than
   arriving as trigger text. Both are tested.
3. **The currency is declared** — on the statement *and* on every line, and they must agree. A line with no
   currency is **refused, not defaulted**: inheriting the statement's commodity would make a mis-parsed
   multi-currency export look clean. The declared currency also decides the precision, so a sub-unit JPY amount
   is `422 precision` rather than something rounded.
4. **The same statement cannot be loaded twice** → **new error code `409 document_already_loaded`**. Identity is
   the document's bytes (`source_files.sha256` is UNIQUE), so a renamed re-download is still the same statement,
   and the refusal names the `statement_ids` the document is already loaded as.

One more refusal falls out of the schema and is tested with it: a second live statement covering any day the
first already covers (`409 period_overlap`). The other one recorded here — a period crossing a book year, `U3`'s
CHECK, refused as `book_year_span` — **was deleted with the annual book in Phase 2 (issue 0002)**. Migration 0008
dropped `book_year` from every dated fact, a statement period is an ordinary inclusive range now, and nothing in
the schema or the code raises `book_year_span`.

### The opening balance: chained, and honest when it cannot be

`statement_assertions` has `closing_micros` and **no `opening_micros` column**, which is right: statements
chain, and one period's opening *is* the previous period's closing. So:

* if a **live closing assertion ends on the day before** `period_start` for the same account and commodity, the
  declared opening must equal it or the statement is refused
  (`reason: opening_does_not_match_prior_statement`, carrying `prior_assertion_id` and both numbers). That is
  the chain, and it is what catches a statement loaded out of order or against the wrong account;
* **no assertion at all** for that account and commodity — the first statement —
  `opening_verified: false`, `opening_basis: first_statement_for_this_account_and_commodity`;
* **assertions exist but none is adjacent** — a gap — `opening_verified: false`,
  `opening_basis: no_statement_ends_the_day_before_this_period`, with the nearest earlier `end_dt` recorded so
  the gap is visible rather than implied.

**An unverifiable opening is never defaulted to zero.** The declared number survives exactly as submitted, and
`opening_verified: false` travels with it through the response and every read.
`test_the_first_statement_records_its_declared_opening_unverified_and_never_as_zero` is the test that pins it.

**Where the declared opening is stored: the submission proposal's payload, and no migration.** Every submission
is a `statement_load` proposal accepted in the same call (the ordinary authorisation path — see "Authorisation"
above; nothing here reads an actor). The proposal is immutable, content-hashed and permanent, and it is already
the record of *what was claimed and why*, so the opening, its verification status and its basis live in
`proposals.payload` rather than in a new column. Reading it back **refuses** (`500 internal_error`) if that
record is missing, for the same reason the provenance read refuses a broken link: a missing opening must not
read as `0`.

**No migration 0003.** `db/` is byte-identical again. Chunk A's 0001 models all of this; what was missing was a
writer.

### `opening_assertions` was deliberately left unwritten

It would have fitted only the January case: its CHECK forces `as_of = YYYY-01-01`, so it models a *book-year*
opening, not a statement-period one. Writing it for January statements and the proposal payload for every other
month would be two representations of one number. Nothing in `src/haddon` reads `opening_assertions` today, so
leaving it alone costs nothing and keeps one concept instead of two.

### `source_configs.reader` stays unwritten — but the row has to exist

The plan asked for `source_configs` to be left unwritten rather than filled with an invented reader name. It
cannot be left unwritten entirely: `statement_scopes.config_id` is **NOT NULL**, and — the part that matters —
the `period_overlap` trigger joins through it (`source_configs c ON c.id = s.config_id ... AND c.source =
n.source`). With no config row there is **no overlap protection at all**, and nothing in this chunk weakens a
trigger. Making `config_id` nullable would have been exactly that weakening.

So one row is created on demand per (book year, account, commodity), with:

* `source = "statement"` — a single generic value for every series. Because the trigger compares this column and
  it is always equal, two statements for one account and commodity can never overlap. A per-institution `source`
  would have *reduced* the protection;
* `reader = ""`, `reader_version = ""` — **empty, not invented**. There is no reader to name, and a fictional
  name in the audit trail is worse than a blank one;
* `input_schema = "{}"`, `directory = ""` — the input shape is the service's published schema, and submissions
  arrive over REST, not from a watched directory.

`test_source_configs_reader_is_left_unwritten` pins those exact values, and
`test_the_database_refuses_an_overlap_even_without_the_pre_check` removes the Python pre-check and proves the
trigger still refuses — so the config row cannot quietly stop being shared.

### Provenance is scope, and it refuses rather than guesses

A machine did fuzzy work to produce these lines, so the audit trail has to reach the actual file.

* the document is stored by reference: `source_files` — `sha256`, `filename`, `media_type`, `size_bytes`,
  `blob_path`. **The ledger records it and does not fetch it.** Reading the file would put format handling back
  in the ledger; the hash is the agent's claim about the bytes it read and is rule 4's identity;
* **that reference is carried through**: every `source_rows` row carries the same `source_file_id`, its `row_no`
  in the document and its `raw` text — the original line when the agent can supply it, else the canonical JSON
  of the normalised line, never nothing. Two `evidence_links` rows attach the document to the submission
  proposal and to the closing-balance assertion, both with `page` and `locator`;
* `GET /provenance?source_row_id=…|posting_id=…|entry_id=…` answers **"where did this come from?" from either
  end**. From a line: the document. From a **posting or an entry**: the lines behind it, traversed through
  `matches` (the only link between a posting and what explains it) plus any `evidence_links` on the entry —
  which is how the reference reaches the postings derived from a statement. Nothing in this chunk writes
  `matches`; the classification/matching chunk does, and this read starts answering for real the moment it
  does. Tested today against chunk A's seeded fixture, which is the only fixture in the build holding `matches`
  rows.

**The read refuses a broken chain.** `document_of` raises `500 internal_error` rather than returning a null
document, and `_line_provenance` re-checks that the line and its statement name the *same* document even though
the `rows_insert` trigger already enforces it — because the failure mode that matters is not an empty answer, it
is a **confident wrong file**. `test_a_broken_provenance_link_refuses_instead_of_naming_the_wrong_document`
builds exactly that state (on the test's own copy, by dropping one immutability trigger) and requires a refusal.
A subject the entity does not hold is a plain `404`; an empty `lines` list means the subject exists and nothing
links it to a document yet, which is said rather than dressed up.

### Chunk E2's gate 1 is now a real answer

Loading statements is what turns `statement_coverage` from "nothing has ever been loaded" into a genuine
verdict, and all three outcomes are tested end to end:

* `test_loading_statements_turns_gate_1_from_incomplete_into_a_real_pass` — gate 1 goes `fail` → **`pass`**, and
  gates 2 and 3 stop being `unevaluable` because a scope now exists for every (account, commodity, month) cell;
* `test_a_statement_whose_closing_disagrees_with_the_ledger_is_a_real_fail_not_unevaluable` — a loaded statement
  whose printed closing does not match the books is a **`fail`** naming `closing_balance_disagrees` with the
  difference, not an "I could not tell";
* `test_a_month_covered_by_statements_closes_with_no_override_at_all` — March closes with `overrides=[]` and no
  new `close_gate_override` proposal. Until this chunk, every fixture close had to record an override per gate
  (`repo_helpers.OVERRIDE_REASON`). **Completeness, not a waiver, is now what lets a month close.**

`close_checks.py` was not touched. Nothing about the gates changed; the substrate arrived.

### REST: four routes, one of them a write

`src/haddon/api/routes/statements.py`. `POST /statements` (the input), `GET /statements`,
`GET /statements/{id}`, `GET /statements/{id}/lines`, `GET /provenance`.

Lines are paginated rather than embedded — a year of daily transactions is an ordinary statement and an
unbounded response body is not — and they page by **`row_no`**, i.e. document order, so a page can be compared
against the bank's own file. Amounts are integer micro strings in both directions; `reject_wire_numbers` runs
before the schema, so a JSON number in an amount is the amount contract's 422 and not a pydantic type error.
Every statement response carries a `close_checks` link for its `period_end`, because that is where a caller goes
to watch gate 1 change.

Deliberately **not** built: no export job, no operations endpoints, no statement supersede/correct route
(`statement_scopes.supersedes_id` and `statement_assertions.supersedes_id` exist and the triggers support them,
but revising a loaded statement is its own decision and nothing needs it yet), no upload endpoint (the document
is submitted by reference), and no classification or matching — that is the next chunk, and gates 2 and 3 stay
substrate-only until it lands.

### The plan document is further out of date, on purpose

The original plan is the historical spec and is not edited. It describes
**per-bank readers** — `source_configs.reader`/`reader_version`, a reader registry, per-institution column maps
and a watched `directory` per source. **This chunk rejects that design.** The agent converts whatever the bank
produced into the one generic shape; the ledger holds no reader, names no institution, and parses nothing. The
two reader columns survive in the schema (0001 is sha256-pinned) and are written empty.

Two structural guards keep the decision from eroding into a convenience helper:
`test_the_statement_input_parses_nothing_and_reads_no_file` (neither statement module may import a parsing
library or open a file) and `test_the_service_depends_on_no_parsing_library` (no such library may become a
dependency). A third, `test_the_statement_schema_and_api_name_no_institution`, keeps institution names out of
the input surface and out of the migrated schema; the bank in the tests
(*Northwind Commercial Bank*) is fictional and lives only in `tests/test_statement_input.py`.

### The mutation probes, and the one that came back clean

Every load-bearing claim was broken in `src/haddon/repository/statements.py`, the suite re-run, the break
restored. Each row names the test that caught it.

| break | red |
|---|---|
| delete the lines-sum-to-movement check (rule 1) | `test_rule_1_the_lines_must_sum_to_closing_minus_opening`, `…catches_a_dropped_line_and_a_duplicated_line`, `…holds_for_an_empty_statement_too`, `test_the_endpoint_refuses_each_rule_with_its_own_status_and_code` |
| accept a line dated outside the period (rule 2) | `test_rule_2_a_line_dated_outside_the_declared_period_is_refused` (both parameters) |
| let a line with no currency inherit the statement's (rule 3) | `test_rule_3_a_line_with_no_currency_is_refused_and_never_inherits_the_statements` |
| accept a line in another currency (rule 3) | `test_rule_3_a_line_in_another_currency_than_the_statement_is_refused` |
| stop refusing an already-loaded document (rule 4) | `test_rule_4_the_same_document_cannot_be_loaded_twice`, `test_the_endpoint_refuses_each_rule_with_its_own_status_and_code` |
| stop comparing the declared opening with the prior closing | `test_an_opening_that_disagrees_with_the_prior_closing_is_refused` |
| default an unverifiable first opening to zero | `test_the_first_statement_records_its_declared_opening_unverified_and_never_as_zero` |
| leave the declared opening out of the submission payload | `test_the_first_statement_records_…`, `test_a_gap_before_the_period_…`, and every read-back test |
| drop the line/statement document-disagreement check | `test_a_broken_provenance_link_refuses_instead_of_naming_the_wrong_document` |
| make `document_of` return an empty document instead of refusing | `test_a_document_this_entity_does_not_hold_refuses_rather_than_returning_no_document` |
| load the statement scope one day short of the declared period | `test_loading_statements_turns_gate_1_from_incomplete_into_a_real_pass`, `test_a_month_covered_by_statements_closes_with_no_override_at_all`, `test_a_statement_that_agrees_with_a_posted_ledger_passes_gate_1`, `test_loading_a_statement_shows_up_in_close_checks_over_http` |
| assert the closing balance one day early | the same four |
| write one `source_configs` row per statement instead of one per series | `test_the_database_refuses_an_overlap_even_without_the_pre_check`, `test_source_configs_reader_is_left_unwritten`, `test_two_statements_…` |

**The probe that came back clean, and what it turned out to mean.** Changing `_line_provenance` to resolve the
document from the *statement's* `source_file_id` instead of the *line's* broke nothing. That is not a weak test:
the disagreement check three lines above it has already established that the two ids are equal, so the rewrite
is **semantically identical** on any state the read can reach. Running the two breaks *together* — drop the
check **and** read the statement's id — does go red
(`test_a_broken_provenance_link_refuses_instead_of_naming_the_wrong_document`), so the pair is covered jointly,
which is the only way it can be covered.

`test_where_did_this_line_come_from_reaches_the_actual_document` was strengthened anyway, and for a better
reason: it now checks the reported document against the line's own `source_file_id` **joined in SQL**, rather
than against the values the test submitted. The claim "the document you get back is the one this line points at"
is now checked against a second source of truth.

**The probe harness itself produced the other finding.** The first run was killed by a 120-second timeout
between mutating the file and restoring it, so the *next* run read an already-broken file as its baseline and
every probe reported six extra unrelated red tests. Two things caught it: the last probe's anchor string was no
longer present in the source, and the suite failed in file order. The harness now restores in a `finally` and
asserts its baseline against a pristine copy on disk before it starts. **Worth remembering: a mutation probe
that reports broad, unrelated damage is more likely a dirty tree than a well-covered claim.**

## Chunk F3 — re-pinned to the corrected source, and the third entity in the oracle

### The fixture revision moved

The read-only pinned corpus checkout every oracle figure is measured from was moved from the revision C1, C2a,
C2b, E1 and F1 measured to a later one that corrected two defects in closed years. `baselines.json` records both
revisions, so the history reads. This section is about what a changed fixture does to the pins, and how to
re-pin without hiding anything.

Neither correction was economic:

1. **ExampleBV's 2026 opening did not match its 2025 closing.** `liabilities:corporate-tax-payable` and
   `equity:retained-earnings` each moved by the same few cents, in opposite directions. The two errors offset, so
   the entry balanced and every per-year check passed. Those are **exactly** the two figures C2b's
   `verify_carry_forward` had computed as expected and pinned as known-open.
2. **ExampleSL's 2025 book failed `hledger check --strict`.** The corrected revision declares its commodity, and
   the book now passes `--strict` with the pinned binary, exit 0.

### Every figure that moved, and the source change it is attributable to

The rule taken from the spec — *"never retain the phantom account to preserve a count"* — was applied in the
strict direction: nothing was re-pinned that could not be tied to a named source change.

| pin | old | new | attributed to |
|---|---|---|---|
| **ExampleInc**, every book pin | — | **unchanged** | its 2025 ledger was not touched between the two revisions |
| **ExampleInc** carry (`from_entries`, `to_entries`, `to_opening_legs`, `folded`) | — | **unchanged** | same |
| **ExampleBV**, every book pin | — | **unchanged** | its **2025** ledger was not touched; only its **2026** opening was |
| **ExampleBV** carry `folded`, `from_entries`, `to_entries`, `to_opening_legs` | — | **unchanged** | the 2026 opening's two lines net to zero, so no total moved |
| **ExampleBV** carry `open_items` | two known-open differences | **gone** (field removed) | correction 1 — see below |
| **ExampleSL** book + carry | *did not exist* | pinned for the first time | correction 2 made the entity importable for the first time |

ExampleInc's figures were the explicit control: its entry, posting, pair, comment and tag counts, and all four
content digests, are byte-for-byte what C2b measured. ExampleBV's 2025 book is the same. **No digest of either
entity changed**, which is the strongest available evidence that the corrections did what they say and nothing
else: a single altered byte anywhere in either 2025 ledger would have moved `canonical_sha256`.

### One source change that is *not* one of the two corrections — reported, not quietly re-pinned

The revision range held more than the two corrections it was described as. ExampleSL's chart also gained
declared accounts that have no postings in the year, and its 2025 book gained a second commodity directive.
Neither is accounted for by either named correction, so neither was folded silently into the re-pin.

This is visible in the pins and was not smoothed over. `accounts` in a baseline is the **declared** chart (C1
builds the chart from `hledger accounts --declared` and never from observed posting names), while `pairs` is
what is actually posted to. A chart can declare more accounts than it uses; a future source change that dropped
one of the unused declarations would move `accounts` and nothing else, and the baseline gate would say so.

The duplicate directive is `1,000.00` in the main journal and `1000.00` in the included `accounts.journal`.
Both parse to precision 2, so `read_commodity_directives` accepts them and locates the first; a disagreement
would raise `HledgerShapeError` ("declared twice with different precisions"), which is asserted in
`test_hledger_binary.py`.

### The carry-forward gate: from "exactly these two known-open differences" to zero

C2b's `CarryBaseline.open_items` pinned *which* differences were already known so the gate failed the moment one
appeared, disappeared or moved by a micro. It was never a tolerance — `verify_carry_forward` still returned
`status='failed'` and reported both — but it was a mechanism for passing with a known-broken boundary.

Correction 1 removed the only instance. **The field and the whole code path are gone**, not set to `[]`:
`carry_forward` now requires `v.ok` — zero unexplained differences — for all three entities, and there is no
longer a place to record a tolerated one. A difference that genuinely *is* explained still has a home, and it is
the right one: a `Reclassification` passed to `verify_carry_forward`, which must cite its document.

The test that asserted ExampleBV's 2026 opening was off and that this was reported rather than tolerated was
**deleted**. It asserted a defect that no longer exists; keeping it would be asserting something false. What
replaces it:

* `test_bv_s_2026_opening_is_now_exactly_the_mapped_carry_forward` — the corrected reality, keeping every
  assertion the old test made that is still true (the retained line is the 2025 closing retained plus the 2025
  net result, to the micro; `folded` is unchanged).
* `test_a_reintroduced_carry_forward_discontinuity_is_still_detected` — **the capability, kept.** It copies
  ExampleBV's 2025 *and* 2026 ledgers to `tmp_path` and puts the exact defect back (the two figures the
  correction changed), then asserts the gate fails and enumerates both differences. It is the hard case on
  purpose: the two errors offset, so the entry still balances, every per-commodity net is still zero, and
  `hledger check --strict` still passes — only the carry-forward comparison sees it. A control run on the same
  copy *without* the reintroduced defect passes, so the failure is the defect and not the copying.

### The third entity, as data and not as a case in the code

ExampleSL imports through the existing `bin/haddon-import` path with no new branch anywhere: `ES` / `EUR` /
`S.L.` come from `ENTITIES`, the chart and commodity from its own journals, and the retained account
(`equity:retained-losses`, where the other two use `equity:retained-earnings`) from
`provisioning.verify_retained_mapping`, which was already the one copy of that rule. Its figures — transactions
and postings, pairs and monthly pairs, declared and used accounts, commodities and source files, note-field
counts, the posting-status histogram, memo normalisations and the 2026 opening's legs and folded result — are
measured with the pinned binary and pinned in the baselines file like the other two entities'. Its 2026 opening
verifies against the carry-forward with zero differences.

It runs the same eleven book gates and four carry gates as the other two, and `bin/haddon-oracle all` now
defaults to all three entities, so the commit gate covers it.

### The plan's account/commodity pair count — reconciled, one way and not the other

C2b could only report the two-book count and recorded the shortfall as a finding, because the third entity had
no book. With it importable the plan's figure can finally be checked, and **it reconciles as a sum and not as a
count of distinct pairs**:

* **summed over the three books**, the plan's figure is reached exactly;
* **distinct pairs across the three books** come to a few fewer.

ExampleBV and ExampleSL are both EUR entities and share a handful of identically named accounts, so the sum
counts those pairs twice. ExampleInc is USD and overlaps neither.

**Nothing was adjusted to reach the plan's figure.** Both numbers are derived inside one test from the pairs the
three runs enumerate, and the overlapping pairs are named rather than asserted as a count, so the reconciliation
cannot drift into an arithmetic coincidence.

### Outstanding, deliberately untouched: a book that fails `hledger check ordereddates`

One of ExampleSL's 2025 journals was out of date order at this revision (an early-January transaction followed a
December one). `--strict` does **not** include `ordereddates`, so it does not block the import and it is not in
any gate — adding it would be adding a gate the source cannot pass. The source was not modified. A test recorded
the state in the suite rather than only in this document, so a later fix to the journal would be a deliberate act
that turns a test red instead of a silent change; chunk G1 later moved the finding out of the suite and into the
oracle's report. Reordering transactions in a closed year's journal is a decision for whoever keeps those books,
not for Haddon.

### `tests/test_import_spain_blocked.py` → `tests/test_import_spain.py`

The module existed because ExampleSL was *the* blocked entity. It is not any more, and **no entity in the
checkout is blocked at the new revision** — so the blocked path (raise by default, `on_blocked='skip'`, CLI exit
3 and 4, the `import.blocked` event, the "no book, so no export" rule in `test_export_real_books.py` (now `test_export_corpus_books.py`), and the
real-stderr assertions in `test_hledger_binary.py`) is now proven against a **stand-in checkout** under
`tmp_path`: ExampleSL's journals with their two `commodity EUR` directives removed, which reproduces the
historical error verbatim — same file, same line, same hledger message.
`test_the_stand_in_reproduces_the_historical_defect_and_differs_only_in_that` asserts the copy is byte-identical
to the corpus journals apart from those two lines. The corpus checkout is never modified; that is the whole reason
the stand-in exists.

### Findings

1. **The fast tier was red, not just the slow tier.** The re-pin was expected to turn seven tests red, all slow.
   There were **nine**, and `test_import_entity_raises_by_default_with_the_real_hledger_error` (in the fast tier)
   and `test_nonzero_exit_carries_the_real_stderr_file_and_line` (`test_hledger_binary.py`, also not expected)
   were among them. `make test-fast` did not pass on the refreshed fixture.
2. **A test that started passing for the wrong reason.**
   `test_skip_is_not_a_silent_success_second_run_is_refused` was **not** among the expected failures — it still
   passed. It asserts that a second `on_blocked='skip'` run raises `immutable`, and the intent is that the
   *blocked marker* a skip leaves behind is what refuses it. With ExampleSL importable, the first call now
   **succeeds completely** and the second is refused by an entirely different rule — "this database already
   holds an entity" — which would refuse a successful second import too and so proves nothing about the skip
   path. A green test that has silently stopped testing its subject is worse than a red one. It now runs against
   the stand-in, and asserts the store is still empty and no second event was written.
3. **A source change outside the two named corrections** — unused declared accounts, and a second
   `commodity EUR` directive. Reported above rather than folded into the re-pin.
4. **The plan's pair count is a sum, not a distinct count** — it double-counts the pairs the two EUR entities
   share by account name.

## Chunk F2 — classification, matching, and the human correction path

The middle of the one sentence the build is for: *a statement for a month goes in, the agent classifies it,
entries post, the month reconciles and closes, and a human can look at it and correct something.* Chunk F1
supplied the statement; this chunk turns a line into an entry, ties the two together, and makes chunk E2's
second and third close gates evaluate real data.

`src/haddon/repository/classification.py` is the whole of it: three writes and one read, over tables migration
0001 already had. **`db/` is byte-identical again — no migration 0003.** The Phase 1 tables were designed for
exactly this, and the only thing that was missing was a writer.

| | what it does |
|---|---|
| `classify_line` | decide what a line **is** and book it, on the actor's own authority (`propose_and_accept`), through the existing `entries.post_entry`; by default also allocate it |
| `allocate_line` | an allocation between a line and cash postings that already exist. **Posts nothing** |
| `disposition_line` | explain a line **without** booking one (`exclude` / `redundant_evidence` / `superseded`) |
| `line_state` | what has become of one line: allocations, disposition, and what of it is still unexplained |

### The cash leg is the line, and it is not the caller's to supply

`classify_line` takes the **contra** legs only. The cash leg is constructed from the `source_rows` row itself:
its account, its commodity, its amount, to the micro. The contra side must sum to the negative of the line, so:

* the entry balances **because** it agrees with the statement, not as a separate coincidence;
* the allocation that follows is always exactly the line, so a classified line is never born partial;
* a match can never introduce money the statement did not have, because the posting it points at was built
  from the line.

`test_the_contra_legs_must_sum_to_the_line` is the refusal (`422 unbalanced`,
`reason: classification_does_not_match_the_line`), and a split across several contra accounts is ordinary — the
rule is the sum, not the count.

`dt` defaults to the line's own date and `origin` to **`"source"`** (an existing value of `entries.ORIGINS`).
No new origin was invented: "which entries came from a statement?" is answered by `matches` and `evidence_links`,
which is a join rather than a guess. Every classified entry also gets an `evidence_links` row to the statement's
document, so `GET /provenance?entry_id=…` answers even before anything walks the matches.

### An allocation is not an economic event, and must never be able to become one

This is the claim the whole reconciliation rests on. If matching could move money, the books would agree with
the statement *because matching made them*, which proves nothing.

`allocate_line` writes `match_sets` and `matches` and nothing else. `matches.amount_micros` is an apportionment
of two numbers that already exist — how much of a line one posting explains — and the database refuses it when
it disagrees with either (`bad_allocations`: same account, same commodity, same entity, same sign, cash, posted)
or when the two together exceed what there is (`overallocated`). Two tests defend it, deliberately from
different directions:

* `test_allocating_creates_no_economic_entry_and_moves_no_balance` — **behavioural**: every account/commodity
  balance in the book is captured before allocating, then compared after allocating, after re-allocating onto
  another set, and after the void that re-allocation performs. Identical, all three times, and the `entries`
  and `postings` counts do not move;
* `test_the_allocation_path_can_never_become_a_posting_path` — **structural**, so the property survives a future
  edit: none of `allocate_line`, `_write_match_set`, `_void_set`, `_check_allocations` may call `post_entry` /
  `create_draft` / `seal`, or insert into `entries` or `postings`. That is the test that fails the day somebody
  adds a "while we are here, book the difference" convenience.

### Partial is a state, not an error — and it is visible three ways

A line half allocated, a **posting** half allocated, and an **unsealed** set are three different incomplete
things, and gate 3 already looked for all three. All are now reachable and all are tested:

* `source_row_partially_matched` — `line_state` reports `partially_allocated` with `unallocated_micros`, and
  gate 2 **passes** on the same line, because it *is* explained, just not completely. The two gates say
  different things and the test asserts both;
* `posting_partially_allocated` — one posting funding more than the line. Invisible from the row side, which is
  why gate 3 checks the posting side separately;
* `match_set_not_sealed` — a draft set allocates **nothing**: `line_state` reports `pending_micros` separately
  from `allocated_micros`, the line still counts as unexplained for gate 2, and gate 3 blocks on the open set.
  That is the same rule the gates apply, applied in one more place rather than in a second way.

Partial is also a step and not a dead end: two allocations that together cover a line leave it complete.

### One line is explained exactly one way

A line with a live allocation cannot be dispositioned, and a dispositioned line can be neither allocated nor
classified. Each refuses with `409 immutable` and a `remedy` (`replace_or_void_the_allocation` /
`supersede_the_disposition`).

Without this rule a row could be both `exclude`d and half matched, and the gates would read it as explained —
gate 2 stops at "disposed **or** matched", and gate 3's row check skips disposed rows. Rather than change
`close_checks.py` to disambiguate a state that should not exist, the writer refuses to create it. A revision of
a disposition goes through `supersedes_id`, which keeps both readable, exactly as a revised proposal does.

### Re-allocating, and why `replaces_set_id` is an argument rather than a second endpoint

Moving a line onto a corrected entry needs the old sealed set released first (`sealed -> void` is a transition
the schema already models; it releases a *claim*, never an entry). Rather than add a `release`/`void` verb,
`allocate_line(..., replaces_set_id=…)` voids the named set and seals the new one in the same transaction. One
concept instead of two, one call for the caller, and no window in which the line is unallocated.

`match_sets` has no column for "what this replaced", and 0001 is pinned, so the replaced id lives in the
allocation proposal's payload — the same decision F1 took for the declared opening balance, for the same reason:
the proposal is already the immutable record of what was done and why.

### Gates 2 and 3 are now real, and `close_checks.py` was not touched

Chunk E2 wired both gates against tables nothing wrote; the substrate arrived here. **The acceptance test is
`test_a_month_whose_every_line_is_explained_closes_with_no_override_at_all`**: a March whose statement is loaded,
whose first line is classified-and-allocated and whose second is `redundant_evidence` for a payment already
booked by hand, closes with `overrides=[]` and writes no `close_gate_override` proposal.

`test_each_condition_broken_on_its_own_refuses_the_close_from_the_right_gate` breaks each of the three
conditions on its own and requires the *specific named* item from the *right* gate — `statement_not_loaded`
(gate 1), `source_row_unexplained` (gate 2), `source_row_partially_matched` (gate 3) — plus a
`409 close_not_eligible` naming that gate and a lock that did not move.
`test_completing_the_one_broken_thing_is_what_makes_the_close_go_through` is its positive control.

**Why the second line is `redundant_evidence` and not `exclude` in the acceptance fixture**, which is worth
recording because it is a real property of the design rather than a test convenience: a line that books nothing
at all on a *cash* account necessarily leaves the bank's closing balance and the ledger disagreeing, so gate 1
refuses. Gate 1 and gate 2 are not independent, and `exclude` on a cash line is a claim that the ledger will
also have to account for somewhere. That is the correct behaviour — the plan's *"a zero balance difference alone
never means complete"* has a converse, and both hold here.

### The human correction path, walked rather than rebuilt

`corrections.reverse_entry` and proposal supersession already existed. `test_a_human_corrects_what_the_agent_booked_and_the_line_follows_the_correction` walks the whole thing in one test, because the point is that the pieces join up:

1. the agent classifies a line to the wrong account; the entry is the agent's;
2. a human reverses it with a replacement. **The original stays `posted`, unedited, and stays in the sums** —
   `test_the_correction_leaves_the_original_in_the_sums` checks the trial balance nets to zero on the original
   account rather than the original vanishing, and that all three entries are still posted rows with all six
   postings, which an export and a report will both see. The mirror is checked to be the exact negated leg
   multiset;
3. the mirror and the replacement are attributed to the **human**; the original is still the agent's;
4. the line is re-allocated onto the corrected entry with `replaces_set_id`; the old set is `void`,
   `line_state.entry_ids` names **only** the replacement, and `unallocated_micros` is 0;
5. `GET /provenance?entry_id=<replacement>` still reaches the original document;
6. the month closes with no override.

**`entry_ids` counts live matches only.** A voided set released its claim, so it must not keep the line pointing
at the entry it disowned; `matches` keeps the whole history so a reader can still see what changed and when.

### A correction in a closed month says to reopen

`entries._check_open` gained one detail key and one clause of message: `remedy: "reopen"`, and *"reopen the
period through an earlier date before changing it"*. That is the only change to `entries.py` in this chunk, it
is additive (no existing assertion moved), and it is honest everywhere it fires — reopening is deliberately
**ungated on eligibility** (chunk E2), so it is always an available next step rather than advice that might
itself be refused. `test_a_correction_in_a_closed_month_is_refused_and_says_to_reopen` asserts the refusal,
asserts nothing was written, then reopens and runs the same correction through.

The three line-level writes refuse a closed month the same way, with the same `remedy`. In `classify_line` the
period check runs **before** the row's own state checks (a closed month cannot be re-explained either way), and
only when `allocate=True`: `matches_insert` demands the *row's* date be open as well as the entry's, whereas
booking an entry in a later open month about a line in a closed one touches the closed month not at all.

### REST: four routes, three of them writes

`src/haddon/api/routes/lines.py`. `GET /lines/{id}`, and `POST /lines/{id}/{classify,allocate,disposition}`.
Nothing else — no export job, no operations endpoints, no rules endpoints, and no `release` verb (see above).

A line is addressed by its own id rather than nested under its statement, because it is reached from both ends
and one path for one thing is easier to follow than two; `GET /statements/{id}/lines` items gained a `line` link
so the two surfaces join up. **Every response is the line's whole state afterwards**, plus a `result` object
naming what the write did: the question a caller has after writing is "what is this line now?", and answering it
in the same round trip is what lets an agent work through a statement without re-reading it.

`code` / `comment` / `tags` (the E1 note fields) are on the repository function and deliberately **not** on the
wire: nothing in this slice needs them, and an unused field on a published schema is a promise.

### The mutation probes

Every load-bearing claim was broken in `src/haddon`, the affected suites re-run, and the break restored in a
`finally`. The harness asserts its baseline against a pristine copy before it starts (F1's lesson: a probe that
reports broad, unrelated damage is more likely a dirty tree than a well-covered claim), fails loudly if an
anchor is missing or not unique, and re-runs the whole selection at the end to prove the tree is green again.

**Twenty-one probes, twenty-one caught.** Each row names one test that went red; the gate probes went red in
chunk E2's own suite as well, which is the cross-check that matters — those gates were supposed to already be
correct and only short of data.

| break | red |
|---|---|
| drop the row over-allocation refusal (a line allocated twice) | `test_a_line_cannot_be_allocated_twice` |
| drop the posting over-allocation refusal | `test_an_allocation_that_over_allocates_a_posting_is_refused` |
| let a match point at another account / currency | `…does_not_describe_the_same_money…[wrong_account-subject_mismatch]` |
| let a match run the other way from the line | `…does_not_describe_the_same_money…[wrong_sign-sign_mismatch]` |
| let a match claim an entry that is not posted | `…does_not_describe_the_same_money…[draft_entry-entry_not_posted]` |
| let a match point at a non-cash posting | `test_a_line_on_a_non_cash_account_cannot_be_allocated_at_all` |
| let the contra legs not sum to the line (invent economic movement) | `test_the_contra_legs_must_sum_to_the_line` |
| let a line be classified twice | `test_a_line_cannot_be_classified_twice` |
| let a line be allocated **and** dispositioned | `test_a_line_is_explained_exactly_one_way` |
| let a second disposition be added instead of superseding | `test_a_disposition_is_revised_by_superseding_it_and_never_by_a_second_one` |
| count draft sets as allocated in `line_state` | `test_an_unsealed_match_set_is_a_match_in_progress_and_blocks_the_close` |
| keep a voided set's entry in `entry_ids` | `test_a_human_corrects_the_agent…_and_the_line_follows_the_correction` |
| let the line-level writes ignore the period lock | `test_allocating_and_dispositioning_are_refused_in_a_closed_month_too` |
| drop `remedy: "reopen"` from the period-lock refusal | `test_a_correction_in_a_closed_month_is_refused_and_says_to_reopen` |
| **gate 2** passes on an unexplained staged row | `test_close_eligibility::test_each_gate_names_the_right_outstanding_items`, `test_classifying_without_allocating_leaves_the_line_unexplained`, `test_completing_the_one_broken_thing_is_what_makes_the_close_go_through`, + 1 |
| **gate 3** passes on a partly matched row | `test_close_eligibility::test_a_partially_matched_row_and_a_partially_allocated_posting_are_both_named`, `test_a_partially_matched_line_is_a_real_state_and_blocks_the_close`, `…refuses_the_close_from_the_right_gate[line_partial…]` |
| **gate 3** passes on a partly allocated posting | the same E2 test, plus `test_a_partially_allocated_posting_is_visible_from_the_posting_side` |
| **gate 3** passes with a draft set still open | `test_close_eligibility::test_each_gate_names_the_right_outstanding_items`, `test_an_unsealed_match_set_is_a_match_in_progress_and_blocks_the_close` |
| `replaces_set_id` stops voiding the old set (the line is double-counted) | `test_a_human_corrects…`, `test_allocating_creates_no_economic_entry_and_moves_no_balance`, `test_the_allocate_endpoint_posts_nothing_and_can_replace_an_allocation` |
| **the correction stops posting the exact negative** (the original would be double-counted rather than netted) | `test_the_correction_leaves_the_original_in_the_sums`, and chunk B's `test_repo_corrections.py` |
| **a draft may be created into a closed period** (a correction to a closed month stops being refused) | `test_a_correction_in_a_closed_month_is_refused_and_says_to_reopen`, `test_allocating_and_dispositioning_are_refused_in_a_closed_month_too`, and `test_repo_entries.py` |

**A harness incident worth recording, because it is F1's lesson from the other side.** The first probe run was
started, and a source file was then edited *while it was running*. The harness reads each file fresh per probe
and restores it in a `finally`, so the edit was silently reverted by the next probe's restore — and killing the
run to stop the damage left one mutation (`if False:` in `classify_line`) on disk. It was found, reversed, the
edit re-applied, and the whole run restarted from a clean tree. **The rule this implies is simple and was not
written down before: while a mutation harness is running, the tree it mutates is not yours to touch.** The
second run took a source snapshot to `/tmp` first, so a repeat would have been repairable by comparison rather
than by reading.

### Findings

1. **The gate caught two red tests the fast tier structurally could not.** `make test-fast` was green with the
   whole chunk in place; `make test-gate` was not. Two slow-tier tests enumerate the **published surface** as an
   exact set — `test_api_spine::test_openapi_publishes_every_endpoint_the_chunk_serves` (every path, and every
   mutating verb) and `test_api_read_surface::test_capabilities_publishes_the_real_chart_and_currency_bindings`
   (`capabilities.writes`) — and four new routes made both wrong. Both were **extended, not relaxed**: the path
   set gained the four line routes, the verb set gained the three `POST`s (so a mutating verb added later
   without a decision to add it still fails there), and `writes` gained its three entries. Both tests run only
   against `api_inc`, which needs a real hledger import, so they are slow-tier by chunk E3's rule and no amount
   of fast-tier iteration would have shown this. **This is the case the two-tier split is designed around, and
   it is the second chunk running where the gate earned its ten minutes.**
2. **The period check in `classify_line` was in the wrong place, and a test found it rather than a review.**
   It originally ran *after* `_no_live_disposition`, so a line in a closed month that was already dispositioned
   refused with `immutable`/`row_already_dispositioned` instead of `period_locked`. Both are true, but the
   period lock is the more fundamental fact and the only one with a remedy, so it now runs first.
   `test_allocating_and_dispositioning_are_refused_in_a_closed_month_too` is the test that caught it, and it
   caught it because it asserts the *code*, not merely that something was refused.
3. **`entry_ids` was reporting a voided allocation's entry.** Caught while writing the correction walk: after
   re-allocating onto the corrected entry, `line_state.entry_ids` still named the reversed original, because it
   was derived from every match rather than from live ones. Fixed, and probe 12 is the standing guard.

### What was deliberately left out

* **no `release` / `void` endpoint** — releasing an allocation only ever happens *in order to* make another one,
  so it is `replaces_set_id` rather than a verb of its own. A bare void would leave a line unexplained, which
  the close already refuses;
* **no rules, no auto-classification, no suggestion engine.** The agent decides outside the ledger and submits
  the decision; `rule_versions` / `rule_authorities` stay unwritten, as they were;
* **no `settlements`** — invoice-to-payment settlement is a different relation from statement-line-to-posting
  and nothing in this slice needs it;
* **no statement supersede/correct route** (still, from F1), no upload endpoint, no batch classify. One line at
  a time over REST; the repository functions compose inside one `write_transaction` for anything that wants to
  do a whole statement at once;
* **`journal_mappings.source_row_id` stays unwritten.** It is the importer's link from a journal transaction to
  a source row; this chunk goes the other way and has `matches` for it.

## Chunk G1 — the simplification pass: dropping the schema nothing reaches

The aim of this pass was a meticulous, elegant system. Elegant here means **fewer concepts doing more work**, so this chunk is allowed to remove and not to add. Part
(a), below, is the schema drop; it is one commit on its own because a migration and a test restructure failing
together would be unreadable.

### The audit

Done against a **live migrated database**, resolving every reference with `PRAGMA foreign_key_list` and reading
trigger bodies, rather than by grepping identifiers — a table can look dead because nothing names it in Python
and still be load-bearing through a foreign key. Eight candidates, four verdicts each way, and the four "keep"
verdicts are the more important half of the work.

**Dropped** (migration `0003`):

| table | why it is safe |
|---|---|
| `commodity_styles` | no inbound FK, no code path, no trigger but its own `styles_u` / `styles_d` |
| `batch_items` | no inbound FK, no code path, no trigger but its own `items_u` / `items_d` |
| `proposal_batches` | no code path, but `decisions.batch_id` FK'd to it — so it goes with that column (below) |
| `settlements` | no inbound FK, no code path; its `settlements_insert` (D3) and `settlements_quantum_insert` guards go with it |

**Kept** — these looked similar and are not dead:

| table | why |
|---|---|
| `rule_authorities` | `proposals.record_application` queries it today: an application is authorised by a decision **or** by a rule authority, and `applications.rule_authority_id` FKs to it |
| `rule_versions` | `rule_authorities.rule_version_id` FKs to it; it could only go if `rule_authorities` went |
| `opening_verifications` | `repository/events.py` queries it, and `events.opening_verification_id` FKs to it |
| `opening_assertions` | `evidence_links.opening_id` FKs to it (live since F1's provenance), `opening_verifications` FKs to it, and its CHECK forces `as_of = YYYY-01-01`, so it models a **book-year opening** — precisely chunk E2's not-yet-wired "valid opening proof" close gate. Scheduled, not dead |

`tests/test_migration_0003.py` asserts both halves: that the four dropped tables and their ten triggers are
gone and that **no** surviving trigger, view or foreign key still mentions them, and that each of the four kept
tables is still present and still reached by the reference named above.

Two **columns** go too: `source_configs.reader` and `source_configs.reader_version`. They are architecturally
wrong — this ledger never knows what produced a statement (F1) — so `repository/statements.py` could only ever
write them empty, with a comment saying that inventing a reader name would be a lie. A column that can only
hold a lie is not a column. That made this a small live-code change as well as a schema one.

### The `decisions` CHECK

`proposal_batches` could not be dropped without rewriting a CHECK on a core live table. `decisions` had:

```sql
CHECK((proposal_id IS NOT NULL) + (batch_id IS NOT NULL) = 1 OR kind='migration_accept')
```

and now has, with `batch_id` gone:

```sql
CHECK(proposal_id IS NOT NULL OR kind='migration_accept')
```

The **rule** is unchanged — U4, "exactly one subject except documented migration acceptance". What changed is
that there is now exactly one *kind* of subject a decision can have, so the sum over two mutually exclusive
columns collapses to a NOT NULL test. `decisions` keeps its two immutability triggers, its `UNIQUE(entity_id,id)`,
both remaining foreign keys and every row.

A decision whose only subject was a batch could not be migrated forward — there would be nothing left to say
what it authorised. No code path has ever set `batch_id` to anything but NULL, so no such row can exist, but
`0003` **refuses the whole migration** if it finds one rather than silently discarding an authorisation record.
`test_a_decision_authorised_only_by_a_batch_stops_the_migration` builds exactly that database and proves the
refusal leaves it untouched at `0002`.

### Migration mechanics

`0001` and `0002` are sha256-pinned and were not edited. `0003` is pinned the same way and pins the two 0001
files it **reads** (`downgrade` restores what it dropped from 0001's own text, the idiom 0002 established for
`entries_update`), which is 0003 recording what it read, not a repin of 0001.

Two mechanisms carry the whole migration, and both are worth recording because SQLite makes this harder than it
looks:

1. **`rebuild(table, ddl, into=, select=, triggers=)`.** SQLite can drop neither a CHECK nor a FOREIGN KEY
   clause in place, so `decisions` and `source_configs` are replaced wholesale from pinned text, their rows
   carried through a scratch copy. Deliberately **not** `ALTER TABLE ... RENAME`: with foreign keys on, a rename
   rewrites the REFERENCES clause of every other table that names the one being renamed (nine of them point at
   `decisions`), and it leaves the survivor's DDL quoted rather than exactly the pinned text. Going via a scratch
   copy touches no other table and lands the definition character for character.
2. **`foreign_keys_off(bind)`.** Step 1 of SQLite's own procedure for this class of change is
   `PRAGMA foreign_keys=OFF`, and SQLite ignores that pragma inside a transaction — which Alembic has already
   opened. The attempt to avoid the pragma failed instructively and is recorded here so nobody repeats it:
   `DROP TABLE decisions` under `PRAGMA defer_foreign_keys=ON` runs an implicit `DELETE FROM` that counts every
   child row as a deferred violation, and **repopulating the replacement never clears that counter**, so the
   transaction fails at COMMIT however consistent the data actually is (`PRAGMA foreign_key_check` returns clean
   at the same moment). So the helper commits Alembic's transaction, does the work under its own with
   enforcement off, proves with `PRAGMA foreign_key_check` that nothing dangles **before** committing, and hands
   a fresh transaction back for Alembic to write `alembic_version` in. Enforcement is restored in a `finally`
   and re-read afterwards.

Drops run in FK dependency order: `commodity_styles`, `settlements`, `batch_items`, then the `decisions`
rebuild, then `proposal_batches` — which is only unreferenced once `decisions` has lost `batch_id`.

The hard constraint was that `0003` upgrades a **populated** database. `test_0003_upgrades_a_real_book_without_losing_anything`
takes the corpus's imported ExampleInc book, walks it back to `0002` (the only way a `0002`-era database holding a book
can exist now), brings it forward, and compares every surviving table's row count plus the entire content of
`decisions` and `source_configs` — then checks `foreign_key_check`, `integrity_check`, and that enforcement came
back on. `test_downgrade_restores_0001s_own_definitions_and_upgrade_is_reversible` makes the stronger claim in
the other direction: a downgraded database's schema is **byte-identical** to one that was never at `0003`.

### The matrix shrank by exactly what stopped existing

The forbidden-mutation matrix went from **322 cases (297 refused / 25 allowed)** to **305 (281 / 24)**. All
seventeen removed cases guarded something that no longer exists; not one weakened a rule that still applies:

* 8 — `UPDATE created_by` and `DELETE` on each of `settlements`, `proposal_batches`, `batch_items` and
  `commodity_styles`, from the append-only/immutable loop;
* 6 — every `settlements_insert` (D3) case: `lock: settlements touching locked entry [D3]`, `D3: settlement
  whose payment entry is in the locked period`, `D3: settlement whose invoice entry is draft`, `D3: settlement
  whose payment entry is void`, `D3: settlement of an entry against itself`, `D3: settlement referencing unknown
  entry`;
* 1 — `U6: settlements -2^63`, the `settlements_quantum_insert` case;
* 1 — `decisions: proposal AND batch subject`, the only case that exercised the `batch_id` half of the old CHECK;
* 1 allowed — `D3: settlement between two posted open-period entries (positive control)`.

`test_matrix_size_did_not_shrink` now floors at 281/24 and its docstring names the removals, so the matrix can
still only shrink deliberately. Everything the guardrail protects is untouched: the balance, immutability,
period-lock and seal triggers, and the hledger verifier, all still there — the trigger count fell from 98 to 88
and every one of the ten is a trigger on a dropped table.

### Test count: 1080 → 1074, every removal accounted for

| change | Δ |
|---|---|
| mutation-matrix cases whose subject was dropped (the seventeen named above) | −17 |
| `test_no_float_reaches_db.py`: `settlements.amount_micros` left `RAW_INSERTS`, which drives **two** parametrised tests (STRICT storage, and the on-quantum float) | −2 |
| `tests/test_migration_0003.py`, new | +13 |

Nothing else moved: `test_statement_input.py`'s `test_source_configs_reader_is_left_unwritten` became
`test_source_configs_has_no_reader_and_one_row_per_series` — same test, stronger claim (the columns are gone, not
merely empty) — and `test_tables_match_schema.py` is unchanged at 31, because 0003 removed columns from two
declared tables, not tables.

### Mutation probes

Nine probes, sources snapshotted to `/tmp` first (the F2 rule: while a probe harness is running, the tree it
mutates is not yours to touch). Each breaks one claim, names the test that must go red, and restores.

| broken | caught by |
|---|---|
| `decisions` keeps `batch_id` and the old CHECK | `test_decisions_lost_batch_id_and_carries_the_rewritten_check` |
| `settlements` is not dropped | `test_the_four_dead_tables_and_their_triggers_are_gone` |
| `opening_verifications` — a table the audit **kept** — is dropped too | `test_the_audit_kept_these_and_they_are_still_referenced` |
| a batch-only decision is silently discarded instead of refusing | `test_a_decision_authorised_only_by_a_batch_stops_the_migration` |
| foreign key enforcement is not restored after the rebuild | `test_migrated_file_is_wal_and_guard_accepts_it` |
| `source_configs` keeps the two reader columns | `test_source_configs_has_no_reader_columns` |
| the rebuild carries **no rows** | `test_0003_upgrades_a_real_book_without_losing_anything` (after the fix below) |
| the rebuild carries the **wrong values** (`content_hash` and `reason` swapped in the copy) | `test_0003_upgrades_a_real_book_without_losing_anything` |

**One probe passed against broken code, and that was the finding.** With `rebuild`'s `INSERT ... SELECT` reduced
to `WHERE 0`, `test_0003_upgrades_a_real_book_without_losing_anything` stayed green: an imported book writes
neither a decision nor a source config, so **both** tables 0003 rebuilds were empty and the row-carrying path the
test exists to cover was never executed. The test now writes two decisions (one of them a subjectless
`migration_accept`, the row the rewritten CHECK still has to accept) and two source configs with non-empty
`reader` values into the 0002-era database before upgrading. Re-probed: the zero-row copy is caught by
`rebuild`'s own count guard, and a value-scrambling copy is caught by the content comparison.

### What was deliberately not done

* **no new abstraction.** `0003` adds two helpers, both local to it (`rebuild`, `foreign_keys_off`), and removes
  four tables, ten triggers, three columns and one CHECK branch from the schema, plus `Decision.batch_id` from
  `repository/proposals.py`. The chunk ends with strictly fewer concepts than it started with;
* **no downgrade shortcut.** `0003.downgrade` could have raised `NotImplementedError` — the tables are dead, after
  all — but `test_entry_note_fields.py` builds its 0001-era fixture by walking head backwards, so a head that
  cannot go back would have quietly cost E1 its hardest test;
* **`writable_schema` was considered and rejected** for rewriting `decisions`'s DDL in place. It would have avoided
  the transaction dance, and it edits `sqlite_master` by hand on a live database to do it. Dropping tables is
  already the one thing in this chunk that cannot be undone by editing a file.

### A fixture fault the gate found

`make test-gate` failed twice before it passed, both times on the slow tier, and the second one was not a
consequence of the schema change in any obvious way:
`test_import_inc_bv.py::test_every_way_of_getting_a_second_import_wrong_is_still_refused` refused with
`validation` / `not_migrated` instead of `immutable`, meaning its `shutil.copy` of the two-year ExampleBV fixture had
shipped a database with **no tables in it**. That is the WAL fault `test_oracle.py`'s
`test_publishing_copies_the_database_and_not_merely_its_file` already pins: pages committed since the last
checkpoint live in the `-wal` sibling, SQLite folds them back on last-connection close but skips that while
anything is reading, and whether anything is comes down to when the interpreter last collected a reference cycle.
Migration `0003` changed the page accounting enough to change the outcome. The `two_year_bv` fixture now
checkpoints before any test copies it, which is what every other fixture in `conftest.py` already did. The
ledger behaviour under test is unchanged and still asserted — this was a test-harness bug that the corpus books
surfaced, exactly as they have before.

## Chunk G1 parts (b) and (c) — the books are a fixture, the company is invented

### Part (b): what a book contains is not a fact about this software

Roughly thirty figures about the corpus entities' accounts were written into the test suite — entry and posting
counts, account counts, note-field counts, the plan's pair totals, retained-earnings balances to the micro —
spread over seven modules. They
were wrong the moment the corpus journals were corrected (chunk F3 spent most of its time re-pinning them), and
**that is the proof they were in the wrong place**: a test that fails because someone fixed their bookkeeping is
testing the bookkeeping.

The rule applied here: **the oracle reports on books; the suite asserts properties of software.** A finding like
"this entity fails `ordereddates`" or "this opening is a few cents out" is *output*, not an assertion.

**The corpus books stay** — they found the WAL data-loss bug and the read-lock bug, and they are the only fixture
with enough scale for a page boundary, a cursor across a concurrent write or a running balance to mean anything.
What changed is what is asserted *about* them:

| was | is |
|---|---|
| `INC = dict(entries=…, postings=…, pairs=…, accounts=…)` (literal counts) in three modules | `source_shape(txns, entity)`, derived per run from the pinned binary: the store holds exactly what hledger reports for the same journal |
| `assert len(reparsed) == INC["entries"]` | the export reparses to the same transaction count, posting count **and (account, commodity) multiset** as the store it was made from |
| `assert len(union) == <two-book count>` / `== <distinct count>` / `== <plan's sum>` | two entities share a pair only if they share a commodity, so summed-minus-distinct **is** the overlap between the two same-commodity books |
| `assert g.detail["retained"] == {"USD": "<pinned micros>"}` | `retained == retained_closing + folded`, an identity, for all three entities (the one figure that is a book fact, `folded`, comes from the carry manifest) |
| `assert counted(rows) == {"with_comment": …, "with_tags": …, ...}` | `== BASELINES[(entity, year)].note_counts` |
| `assert total == <pinned entry count>` in the API read surface | `assert total > len(page["items"])` — the page boundary is real, which is why this fixture is used |

**One pinned manifest, and it already existed.** `src/haddon/oracle/baselines.json` holds the corpus shape and its
content digests per entity, plus the carry boundary. Nothing was invented to replace the scattered literals; the
tests that still need a pinned figure read `BASELINES` / `CARRY_BASELINES` from it. That keeps the count of
concepts going down: one manifest, compared in one place — the oracle's `baseline` gate, which **re-derives** every
figure from the pinned binary inside the run, so it cannot pass by agreeing with itself. When the books move,
that gate fails and names each figure that moved.

**Two narrative tests were replaced, not deleted.** Both were about particular journal files:

* `test_the_two_corrected_defects_are_corrected_at_the_source_not_worked_around` asserted an include order, a line
  number, an account name and an amount. The **behaviour** behind it — the phantom `…accumulated-depreciationEUR`
  account could only enter the store if Haddon derived a chart from what it saw in postings instead of from what
  the book declared — is now `test_haddon_never_invents_an_account_the_journal_did_not_declare`, which asserts the
  stored chart *equals* the declared chart, on the same book the defect was found in.
* `test_spain_2025_still_fails_ordereddates_and_that_is_left_alone` asserted a file and a line in somebody's
  journal. What replaces it is the precondition the whole real-books half of the suite rests on:
  `test_every_book_in_the_pinned_checkout_passes_strict_so_none_is_blocked` — Haddon blocks on
  `hledger check --strict` and on nothing else, and at the pinned fixture no book fails it. The outstanding
  `ordereddates` issue stays recorded in the F3 section above and in the oracle's report, which is where a finding
  about somebody's books belongs.

**The corruption probes were made book-independent too.** `TestDeliberateCorruption` used to name a journal, a
bank, a transaction and an amount (`"2025-<bank>.journal"`, `"EUR  <amount>"`). `_corruptible_transaction` now *picks* a
target out of the copied journals by structure, and every requirement it imposes is there so the corruption stays
invisible to everything but the oracle: a journal with no `=` balance assertion anywhere in it (an assertion
elsewhere in the file pins a running balance, so `--strict` would catch the amount edit instead), exactly two legs
(so changing both amounts leaves the transaction balanced), and a day between the 2nd and the 27th (so moving the
date one day back stays inside its own month and no monthly total moves). Same for the carry-forward
discontinuity probe: it moves a fixed offset between two unasserted opening legs it finds itself, and asserts the
gate reports exactly those two accounts, off by exactly that offset, in opposite directions.

### Part (c): Fernside Instruments

The fixture most of the suite runs against used to be a bare two-account entity whose name made the suite read
as though the ledger had been built for one particular company. It is now **Fernside Instruments**, a small instrument maker
that does not exist and belongs to nobody: id `fernside-instruments`, incorporated in the US, books in USD with
EUR and JPY balances beside them (JPY at 0 decimals on purpose, so precision is never uniform), two book years,
and a chart that is a whole little company rather than two accounts and a balance — cash in three currencies, a
receivable, a **payable** (the `liability` account type had nothing exercising it before), one expense and one
income account.

It is defined once, in `tests/repo_helpers.py`, and it is deliberately **not** a company generator: a fixture that
can make any company is a second implementation of provisioning rather than a thing to test provisioning against.
`tests/seed.py`'s integrity fixture is the same company under the short opaque id `E`, which the mutation matrix
names in several hundred SQL literals and where a slug would buy nothing.

`test_the_fixture_company_never_appears_in_src` is the guard: the company's name and id must not appear anywhere
in `src/`. It deliberately does **not** forbid `assets:cash`, which `importing.importer` uses as the general
`is_cash` convention rather than as one company's account — the line is identity, not vocabulary.

What is still corpus-specific in `src/` is the **corpus pointer**, not logic: `importer.ENTITIES`,
`oracle.cli.DEFAULT_ENTITIES` and `baselines.json` name the three books the oracle verifies. That is a
registry of which books to check, and moving it out of the package would be a separate decision.

### Test count: 1074 → 1076 (and 1080 → 1076 over the whole chunk)

| change | Δ |
|---|---|
| `test_fixture_counts_are_what_the_pinned_binary_reports` removed: its entire content was "the literals in this module equal what the binary reports", and the manifest comparison it duplicated is the oracle's `baseline` gate, against the same source | −1 |
| `test_inc_s_2026_opening_is_exactly_the_mapped_carry_forward` became `test_the_2026_opening_is_exactly_the_mapped_carry_forward`, parametrised over all three entities instead of only ExampleInc — the arithmetic identity replaced three pinned amounts, so it costs nothing to run everywhere | +2 |
| `test_the_fixture_company_never_appears_in_src`, new | +1 |

Everything else was a rewrite in place. Two tests in `test_import_spain.py` were replaced one-for-one; the two
`test_*_exports_every_posted_entry` tests in `test_export_real_books.py` (now `test_export_corpus_books.py`) became one parametrised over both
entities; several parametrisations changed their *arguments* from literals to `BASELINES` entries without changing
their count.

### Mutation probes on the replacements

A property test that cannot fail is worse than the literal it replaced, so each replacement was broken on purpose.

| broken | caught by |
|---|---|
| the importer drops the last transaction of the book | `test_inc_imports_exactly_what_the_source_holds` |
| the importer invents an account the journal never declared | `test_haddon_never_invents_an_account_the_journal_did_not_declare` |
| the export drops one leg of every transaction | `test_the_export_reparses_to_exactly_what_the_store_holds` |
| `verify_carry_forward` stops reporting unexplained differences | `test_a_reintroduced_carry_forward_discontinuity_is_still_detected` |
| the `baseline` gate passes whatever the source says | `TestDeliberateCorruption` (all four corruptions) |
| the strict gate also runs a check one of the books does not pass | `test_every_book_in_the_pinned_checkout_passes_strict_so_none_is_blocked` |
| the fixture company's id is written into `src/haddon/api/config.py` | `test_the_fixture_company_never_appears_in_src` |

All seven caught. No probe passed against broken code.

## Chunk G2 — draft → accepted → closed: three states, three distinct triggers

Chunk F2's `classify_line` called `entries.post_entry`, which drafts and seals in one step. So every agent
classification was immutable the instant it was made, and every second thought cost a reverse-and-replace pair:
book 40 statement lines, get 6 wrong, and the month carries 52 entries. The objection was that this gave up
what made a plain hledger journal pleasant to work in: a booking is often not right the first time, and fixing it
should not leave a permanent pair of entries behind.

### The three states, and what triggers each

| state | what it means | trigger | correction |
|---|---|---|---|
| `draft` | the agent is still working | `classify_line` | `reclassify_line` (rewritten in place) or `discard_classification` — **no trace in the books** |
| `posted` | accepted as correct | **`accept`** | `corrections.reverse_entry`; the original stays standing |
| closed | the operator locked the period | `closing.close` | `reopen` (reason required), fix, close again — or a correcting entry in an open period |

**Sealing is triggered by acceptance, not by close.** This is the load-bearing choice. Close happens at filing,
so hanging the seal off it would leave nothing posted all year and every report empty until March of the
following one. Acceptance happens continuously through the year, so the books are live and right from January.
`haddon.reports` was **not touched**: `reports/postings.py` has always filtered `entries.status='posted'` and has
never consulted a lock, so "the reports follow acceptance" was already true the moment acceptance existed. That
was confirmed rather than assumed — `test_a_draft_reaches_no_report_no_export_and_no_balance` compares every
balance in the book before and after drafting, and the probe that widens the report filter to `status<>'void'`
turns it red.

### The accept signature, and why

```python
accept(ctx, *, reason: str, statement_id: str | None = None, entry_ids: Sequence[str] | None = None) -> Acceptance
```

Exactly one of `statement_id` or `entry_ids`, and `reason` always.

* **The unit is a working pass, never a line.** Reading line 30 routinely changes how line 12 should read. If
  the agent accepted each line as it went we would be straight back to F2 — everything frozen instantly, every
  revision a reversal pair — and the draft state would buy nothing. A pass must be *freely revisable inside
  itself* and settled at its end.
* **A statement is what a pass is over**, which is why it is the primary form and the REST surface is
  `POST /statements/{id}/accept`. It resolves to every draft entry the `line_classification` applications of
  that statement's lines booked — read out of the **record** (`applications.result`) rather than out of the
  allocation, because `classify_line(allocate=False)` books an entry and writes no match at all, and that entry
  needs accepting just as much.
* **`entry_ids` is the explicit form**, for drafts that are not a statement's (a manual accrual) or for
  accepting part of a pass deliberately. Naming a posted or void entry is refused, not skipped: it is a mistake
  about what has already happened.
* **Atomic, in one `write_transaction`.** One draft that will not seal takes the whole pass down and leaves every
  other draft exactly as it was. The probe that makes `accept` seal each draft in its own `try`/`except` is
  caught by `test_accept_is_atomic_a_pass_that_cannot_seal_one_draft_seals_none`.
* **The agent accepts its own work.** No human acceptance step, no second party, nothing reads the actor — the
  same posture as § "Authorisation" above. A human reviews afterwards, in the UI or by asking the agent, and
  corrects through the paths in the table.

An accepted pass seals the entries first and then the **draft match sets** that hold their lines, but only the
sets whose every entry is now posted. A set that also points at work outside the pass stays draft and is
reported in `Acceptance.deferred_match_set_ids`; it keeps the month from closing until the rest is accepted,
which is the honest answer rather than a silent half-allocation. (Sealing it early is not merely untidy — it is
what `bad_allocations` refuses, so it would abort the whole acceptance.)

### The completeness hole this opens, and the fix

`close_checks._sealed_match_totals` filtered on the **match set's** status (`match_sets.status='sealed'`) and
never joined through to the **entry's** status, so a sealed set pointing at a draft entry's posting would have
counted as an *explanation*. Until this chunk that state could not exist for longer than one transaction,
because `classify_line` posted immediately. Making classification draft first is what puts it in reach, so it is
fixed here: `_explaining_matches` + `EXPLAINS` now join `matches → postings → entries` and require
`match_sets.status='sealed' AND entries.status='posted'`, and both `_sealed_match_totals` and gate 3's
`_partial_postings` read through it.

**Gate 2's two components are now independently right.** `_drafts` fails on any draft entry dated in the period,
so a month of unaccepted drafts was already refused — but through the *other* component, which would have masked
the row one rather than the two being separately correct.
`test_the_row_component_alone_catches_an_unaccepted_month_with_the_draft_component_disabled` monkeypatches
`_drafts` to `PASS` **in the reader, never in the database** and requires the row component to catch the same
month on its own, naming both unexplained lines. `test_an_interrupted_pass_...` does the other half: half the
pass accepted, half not, the close refused, and the refusal naming the line that is not done — a month that
closed while containing nothing would be the worst available failure.

**One honest limitation, stated rather than papered over.** The `sealed set → unposted entry` state is *not*
reachable through the repository even now: `bad_allocations` includes
`(set_status='sealed' AND entry_status<>'posted')` and the `sets_validate` trigger checks it on every seal, so
the database refuses to create the state at all (`test_the_database_refuses_to_seal_a_match_over_an_unposted_entry`
proves it, by attempting the raw `UPDATE` and getting `match_invalid`). A purely behavioural test of the reader
fix therefore cannot fail, and a probe that cannot fail is worth nothing. So the fix is pinned **structurally**
instead: `test_the_completeness_read_asks_for_a_posted_entry_and_not_only_a_sealed_set` compiles the two queries
and requires the join and the `posted` conjunct in both. Removing the conjunct turns that test red and leaves the
behavioural ones green, which is exactly the honest picture: the read-side rule is defence in depth over a
trigger that already holds, and it is guarded where it can actually regress.

### Correction at every stage

Three paths, tested end to end, and **all three require the human's stated reason**:

* **draft** — `reclassify_line` rewrites the entry in place through the new `entries.revise_draft`; the entry id
  and version tell the story, no mirror and no second entry exist, and only the corrected answer ever reaches a
  report. `discard_classification` is the other half: the draft is voided *and* the draft match set holding the
  line is released in the same transaction, so the line is genuinely free again and can be dispositioned
  instead. Voiding the entry alone would leave a line that is neither explained nor free.
* **posted, period open** — `corrections.reverse_entry`, unchanged in mechanics: the original stays posted and
  stays in the sums, the mirror is its exact negative, the replacement is the human's.
* **closed** — `reopen`, still ungated on eligibility (E2), still requiring a reason, and it keeps the prior
  close record and its snapshot; then fix and close again. Or post a correcting entry into an open period if the
  filed numbers must stand. **Nothing is ever permanently stuck. Closing only means you have to say why.**

**The human's reason is now a required, recorded argument of `reverse_entry`.** It used to be reachable only
indirectly, through whatever reason the caller happened to put on the authorising decision — which is
boilerplate as often as not. The numbers are self-evident a year later; *why somebody disagreed* is not
recoverable from anywhere else, and it is the easiest thing in the flow to drop. It is validated non-blank and
written to the append-only event log (`entry.reversed`) beside the correction it explains. `reclassify_line` and
`discard_classification` take one through the module's existing `_reason` check, and `reopen` has required one
since E2.

### What deliberately did not change

**Posted entries stay immutable while posted.** This chunk moved *when* an entry becomes posted; it did not make
posted editable. In-place edits to posted entries were considered and **rejected**: if posted can change, posted
means nothing, and the only protection left is a log nobody reads. `revise_draft` refuses anything that is not a
draft with the same `immutable` error every other transition uses, and underneath it `entries_update`'s first
statement (`OLD.status<>'draft'`) refuses it again —
`test_a_posted_entry_cannot_be_edited_in_place_by_any_route` checks both, the repository and the raw `UPDATE`.

No migration. `entries.status` already had `draft`/`posted`/`void`, `match_sets.status` already had
`draft`/`sealed`/`void`, and nothing new needed a column: **0003 is still head**. Every trigger, the whole
forbidden-mutation matrix, the `draft → posted` / `draft → void` transitions, "no transition leaves posted or
void", and the hledger verifier are untouched — `db/` is byte-identical.

`revise_draft` may not move a leg an allocation points at. Matching by posting *sequence* means the cash leg
keeps its id across a revision, so the match that holds the line survives; anything that would change the
account, currency or amount of an allocated posting is refused by name with
`"remedy": "replace_or_void_the_allocation"`. The database would refuse the consequence at the next seal
(`match_overallocated`); this refuses the cause, in the caller's terms.

`_check_allocations` gained one parameter, `sealing`, and loosened by exactly what the database already says: a
match against an unposted entry is refused **when the set is being sealed** and allowed while the set is draft,
which is `bad_allocations`'s own rule. A **void** entry is refused either way — there is no state in which it is
money.

### The REST surface

Five writes on a line and two on a statement:
`POST /lines/{id}/{classify,reclassify,discard,allocate,disposition}` and
`POST /statements` / `POST /statements/{id}/accept`. `classify` now returns a line in state `matching` with
`pending_micros` equal to the whole line and `allocated_micros` zero, which is the truthful reading of a draft:
a match in progress that settles nothing. `/capabilities`, the OpenAPI document and
`tests/test_api_spine.py`'s "every mutating verb" set all name the three new routes.

### Test count: 1076 → 1103

`tests/test_draft_accept_close.py` is new (27 tests). `tests/statement_helpers.py` is not a test module: the
statement fixtures were lifted out of `test_line_classification.py` unchanged so the new file uses the *same*
fixture rather than a second, subtly different one. `test_line_classification.py`'s `classify()` helper now
drafts **and accepts** by default (`accept=False` for the tests about the draft state), which is why almost all
of the F2 suite reads the same as before: it is still asserting on a booked line, one step further along.

### Mutation probes

Every load-bearing claim broken on purpose, sources restored from a `/tmp` snapshot between probes (never from
git — the tree was uncommitted work).

| broken | caught by |
|---|---|
| a report counts drafts (`status<>'void'`) | `test_a_draft_reaches_no_report_no_export_and_no_balance` |
| `accept` stops checking that its targets are drafts | `test_accepting_something_that_is_not_a_draft_is_refused` |
| `accept` seals every set it touched, not only the finished ones | `test_an_allocation_is_sealed_only_when_everything_it_points_at_is_posted` |
| `accept` seals each draft in its own `try`/`except` | `test_accept_is_atomic_a_pass_that_cannot_seal_one_draft_seals_none` |
| the completeness read drops the `posted` conjunct (§4's fix) | `test_the_completeness_read_asks_for_a_posted_entry_and_not_only_a_sealed_set` |
| a posted entry becomes editable in place | `test_a_posted_entry_cannot_be_edited_in_place_by_any_route` |
| a correction stops requiring the human's reason | `test_a_correction_without_a_stated_reason_is_refused` |
| a correction takes the reason and never records it | `test_a_posted_classification_is_corrected_by_reversing_it_and_the_reason_is_recorded` |
| `reopen` deletes the prior close record | `test_a_closed_month_is_corrected_by_reopening_and_the_prior_close_survives` |
| `reopen` stops requiring a reason | `tests/test_repo_closing.py::test_reopen_moves_lock_back_and_needs_reason` |

**One probe passed against broken code on the first run, and the finding was in the test, not the code.** The
first version of the spanning-set test built a draft match set that pointed at only *one* unaccepted entry, so
"seal the sets this pass touched" and "seal the sets this pass finished" were the same thing and the mutation
was invisible. The test now builds one line funded by two different draft entries — the only shape in which the
two rules differ — accepts one half, and requires the set to stay draft and be reported as deferred. Re-run
against the same mutation: caught.

---

# Chunk G3 — the reviewer UI (superseded by G4)

> **Superseded.** The Jinja pages, their templates and their two test files were removed in chunk G4, which
> replaced them with a built single-page interface over `/v1`. The decisions below are kept because two of
> them survived the rewrite intact — the two-credentials shape, and the rule that the chrome never names a
> kind of caller — and because the third, "render `close_checks` verbatim", is the mistake G4 exists to
> correct. Read this section for what was learned, not for what is running.

Three server-rendered pages and one write, deliberately plain: Jinja templates and a stylesheet inside
`base.html`. No build step, no npm, no framework, no static directory. `haddon.ui` is served by the **same
FastAPI app** as `/v1`, so there is one process, one registry and one set of engines to reason about.

## The UI is a reader plus one existing write path

`haddon/ui/pages.py` calls the same repository functions the `/v1` routes call and adds no rule and no derived
number. Where a month will not close, the page renders what `close_checks` returns — per gate a status, its own
`reason`, and its own itemised `outstanding` list — rather than a summary somebody wrote. Nothing in
`repository/` was changed for the UI to exist.

The single write is `corrections.reverse_entry`, wrapped by `pages.apply_correction`, which first records the
stated reason as a proposal and an `approve` decision (the authority `reverse_entry` requires) and then passes
**the same string** through as the reversal's reason. It is never defaulted, never templated and never derived
from the entry: chunk G2 made the reason required precisely because it is the one thing that is not recoverable
from the numbers a year later, and a UI that filled it in would have quietly undone that. An empty reason is a
422 that writes nothing.

**Accept and close are not in the UI.** Both already have their own authorisation trail through the API, and a
second way to do them from a browser would produce the same state with a different record behind it.

**The chrome never names a kind of caller.** The first draft of the month page read "N of M need a person" and
"What the agent booked". That is presumptuous and, worse, false: this system deliberately does not distinguish a
human caller from an automated one (§ "Authorisation"), and every one of those lines can be resolved by whoever
gets to it. The headings now describe the line's *state* ("need attention", "What was booked"). A **recorded**
actor is still shown wherever there is one — that is data, and it is the whole point of the trail.
`test_no_page_names_an_actor_type_in_its_own_chrome` walks every page and holds this. The same discipline
applies to the prose in `haddon/ui/`: describe the surface, not who is imagined to be at it.

## Two surfaces, two credentials, and nothing without one

`/v1` keeps its bearer token. The UI pages have **no** application-level credential and are gated by the reverse
proxy's password (`auth_basic`), because a browser cannot present a bearer token and a proxy password is not
something an API client should have to hold. That is a difference between two *credentials*, not between two
kinds of caller. The nginx vhost puts the password on the default `location /` and turns it **off** only under
`/v1/`, so a UI route added later is behind the password by default rather than by somebody remembering — and
`/docs`, `/openapi.json`, `/healthz` and `/readyz` are behind it too.

The acting principal for a correction is `ui:<the user the proxy authenticated>`, taken from an `X-Haddon-User`
header nginx sets from `$remote_user` and blanks on the `/v1` location so an API caller can never supply one. It
is recorded and never parsed, like every other actor string in this system.

`tests/test_ui_routes.py` holds both halves of that claim together — every UI page 200s with no `Authorization`
header, and `/v1` still 401s without one — because a deployment where those drift apart has a hole in it and
nothing else would say so.

## Templates are not tested, view functions are

The mutation matrix cannot usefully reach a template, so nothing that *decides* anything lives in one. Ranking
(`rank_of`), month bounds, statement coverage, the correction's refusals and the amount formatting are all in
`pages.py` / `format.py` and are driven directly by `tests/test_ui_pages.py`.

One decision worth naming: `format.amount` renders whole units at the commodity's precision and **falls back to
the raw micro string** when the value is not an exact multiple of the commodity's quantum. An amount that cannot
be written at the declared precision is a fact about the data; rounding it on the way to a screen would be the
first float in the system.

## The demo statement

(The demo-statement script described below was later removed; the browser harness in `browser/` builds its
own fixture books.)

`bin/haddon-demo-statement` loads one obviously-synthetic statement (`SYNTHETIC DEMO` in the filename, the
locator and every line description) so the month page has a reconcile view before any real bank statement
has been loaded. It lives in `bin/` and **not** in `src/`, for the same reason the fixture company does not:
inventing data is not something the ledger does.

It classifies two lines as **drafts**, excludes one and leaves three unexplained, and accepts nothing — a draft
is in no report and no export, so the ledger and trial-balance pages keep showing the imported books to the
cent (`test_the_demo_statement_never_moves_a_posted_balance`). The cost is that the served demo has no
`allocated` and no `partial` line: the first needs an accepted booking, which would post an invented amount into
the books, and the second needs a synthetic line allocated against an imported posting. Both appear on their own
when a real statement is loaded; `rank_of` is tested against all five states regardless.


---

# Chunk G4 — the reviewer interface, rebuilt, and the month review redesigned

Two things at once, and the second is the reason for the first. The interface became a built single-page app
(Vite + React + TypeScript + Tailwind + shadcn/ui) in `web/`, served by the same FastAPI process; and the month
review stopped showing the close-eligibility machinery.

## The gates stay; the reviewer stops reading them

The complaint about the G3 month page was that it showed a lot of gate output that was too much for someone
reviewing and not actionable. A reviewer needs to know that the books balance and the entries look right.

That was fair, and it was measurable. On a book with no prior lock, one month's page put several screens of gate
output above a handful of statement lines: three gate panels, each itemising every month since the book began,
because `close_checks` evaluates over the whole close window and with no prior lock that window starts at the
first day of the earliest book. Every one of those items was *true*. None of them was a thing a person could do.

Chunk G3 made a deliberate choice here and it was the wrong one. Its reasoning — "render `close_checks`'s own
itemised refusals rather than a summary of them" — is sound about **fidelity** and silent about **audience**. It
is the right rule for an API, which must not paraphrase a refusal, and the wrong one for a page, which must not
hand somebody a list they cannot act on.

So: nothing about the gates changed. `close_checks` is untouched, `close` still refuses on it, `GET
/close-checks` still publishes every gate with its own status, reason and outstanding items. The page simply
does not read it. The three questions it answers instead are the ones a reviewer actually has, and every answer
is actionable:

1. **Does the bank agree with the books?** `GET /reconciliation` (new, below).
2. **Does anything here want a decision?** Lines that need an account, lines part explained, bookings written
   but not posted, days no statement covers.
3. **What can be done about it?** Each of those is a button.

**One claim on that page is checked rather than asserted.** When the difference between bank and books is
exactly the negative of the sum of `unallocated_micros` over every line through the month end, the page says the
outstanding lines account for it. When it is not, it does not say so. The arithmetic is cheap and the sentence
is worth a great deal — it turns "the books are out by 1,234.56" into "this line is why" — but a plausible
explanation that is not checked is worse than no explanation at all.

## Four API additions, all of them things the API should have had

The rule was: where the interface needs something the API does not have, extend the API rather than bend the
interface, and keep every addition general. Each of these is useful to an agent for the same reason it is useful
to a page.

* **`GET /lines`** — statement lines across every statement over a date range, ordered `(dt, row_no, id)`, each
  carrying its state and the entries it was booked to. `statements/{id}/lines` reads one *document* in the order
  it was printed; this reads *time*. Without it, reviewing a month is one request plus one per line — a page
  load that gets slower every month the books are used. The `state` filter runs **after** the page is read,
  because a line's state is derived rather than stored, so a filtered page can be shorter than its limit — or
  empty — while `next_cursor` is still set. The page says so in `filtered_after_page` (issue 0008 section A). An
  unknown state is a `422` naming the five that exist, not an empty page: "there
  are none" and "you spelled it wrong" read identically otherwise.
* **`GET /reconciliation`** — bank against books at any date. `close_checks` already computed a version
  of this, but only where a printed closing balance exists for exactly that date, and only as one outstanding
  item among however many the window produced. Here the statement side is **derived**: the earliest loaded
  statement's declared opening plus every line through the date. Where the bank *did* print a balance for the
  date it is reported beside the derived figure — two independent sources agreeing is worth showing. `null`
  anywhere means "not knowable here", never zero: a reconciliation that printed `0.00` for an account with no
  statement would be telling somebody their books agree with a document nobody has loaded.
* **`POST /lines/{id}/recode`** — move a line's booking, whatever state it is in. A draft is rewritten in place
  (`reclassify_line`, unchanged). A posted one is reversed, replaced and re-allocated with `replaces_set_id`,
  **in one transaction**. That last part is the whole reason it exists as one call: done separately, the line is
  momentarily unexplained between the reversal and the re-allocation, and a month stops closing for a reason
  nobody caused. `classified_entry_ids` now reads `line_recode` proposals as well as `line_classification` ones,
  or a second recode would find the entry that was already mirrored rather than its replacement.
* **`POST /entries/{id}/reverse`** — a plain gap. The API could book, allocate, dispose and accept, but it could
  not disagree with something already posted; only the server-rendered pages could, in process. An agent that
  wanted to correct its own posted entry over REST had no way to.

Writing the test for the last one found a real bug: `routes/entries.py` already had a *route* function called
`get_entry`, so `from haddon.repository.entries import get_entry` was silently shadowed and the new handler
called the route. It is imported as `read_entry` now.

## Two credentials, one API, and why there is a `/ui/v1`

Chunk G3's shape held: `/v1` carries the agent's bearer token, the browser is gated by the reverse proxy's
password. What changed is that the interface is now a client-side bundle, so the browser has to reach the API
itself rather than being handed rendered pages.

A page cannot present a bearer token, so `ApiConfig.proxy_auth` lets a configured header — the user nginx
authenticated — resolve to a `Principal` exactly as a token does: one opaque actor string, one entity scope,
parsed by nothing. `ui:<user>` lands in `created_by` like any other actor, and nothing downstream
can tell the two apart, which is the point.

That needs the proxy to pass the header on the browser's entrance and to **blank** it on the agent's, because
the agent's entrance takes no password and a caller must never be able to name itself there. One `location`
cannot do both, so there are two: `/v1/` (token, header blanked) and `/ui/v1/` (password, header passed, `/ui`
stripped by `proxy_pass`). The service sees an ordinary `/v1` request either way and has one API, one set of
routes and one set of tests.

`proxy_auth` is **off unless configured**. Configuring it is the deployment stating "a reverse proxy sets this
header and nothing reaches this service without passing through it". Where that is not true, leaving it unset is
what keeps a forged header from being a credential; `test_api_review_surface.py` holds both halves.

## Ceremony proportional to what cannot be reconstructed

Editing was meant to ask for no more than it needs, and chunk G2 requires a stated reason on every correction
path. Both are right, and the resolution is to notice that they are about different
writes.

* A **new booking** takes two fields: an account and a description. What is typed is recorded as the entry's
  narration **and** as the proposal's reason, and the form says so in as many words. That is not the software
  inventing a reason — it is the person's own words, used twice, disclosed. There was no earlier answer to
  disagree with, so there is nothing else to ask.
* **Revising a draft** asks for nothing extra. The books never saw the earlier version.
* **Changing something already posted** asks for one more sentence. This is the case G2's rule is actually
  about: the numbers are self-evident a year later and *why somebody disagreed* is not recoverable from
  anywhere else.

**Accepting a pass is now in the interface**, reversing a G3 decision that kept it out on the grounds that it
would be "a second way to do the same thing". It is not a second way — it is the same `classification.accept`,
through the same `/v1` route, recording the same decision. The G3 reasoning assumed a page and an API were
different mechanisms; once the page *is* an API client that assumption is gone. And leaving it out would have
been worse than inconsistent: a reviewer who can book a line but not post it has been handed a draft that sits
there, which is the opposite of what was asked for.

## The build is not in the repository

`make web` builds into `src/haddon/ui/static`, which is gitignored, so `haddon-serve` serves it with no copy
step and there is still exactly one process. A checkout without the build serves `/v1` normally and the page
says which command was not run, rather than 404ing as though the service were broken.

The trade is that a deploy is `git pull && make web && systemctl restart`. The alternative — committing the
bundle — makes every frontend change a diff nobody reviews and a merge conflict nobody can resolve.

## The copy rule survived the rewrite, and moved

`test_ui_routes.py::test_no_page_names_an_actor_type_in_its_own_chrome` went with the Jinja pages.
`tests/test_ui_copy.py` replaces it and holds the same line over `web/src` and `src/haddon/ui`: no interface
copy says "needs a person", "the agent booked", "human review" or any of eleven other phrases that assert a
distinction this system does not make.

It reads **sources** rather than rendered pages, because the interface is a client-side bundle and rendering
every state of every page would mean a browser in the suite to assert something that is really a property of the
text somebody typed. Quoted spans are skipped, so a docstring explaining the rule does not fail it, and two
further tests keep the guard honest: one asserts it is reading at least a dozen real files (a guard that
silently stops reading anything always passes), and one asserts some page still shows a recorded `created_by` —
the rule is about the chrome, not about the data.

## The books could not say anything a bank statement did not say

`POST /v1/entities/{e}/entries` writes a balanced journal entry directly. It closes a gap that had been in the
API since the beginning and was invisible for as long as the only work being done was reading statements.

Every entry in a book had arrived one of two ways. The **hledger importer** loaded a book once
and refused a second pass over the same year (`_require_importable`: "already holds book 2026; a book is imported
once"), so it could not add to a book already open. **`classify_line`** built an entry *around* a statement line
and constructed the cash leg from the line itself, so it could not write an entry that had no line. Between them
there was no way to record an invoice raised, a payroll accrued, a quarter's tax withheld from salaries moved
from a withholding account to the amount owed to the tax authority, or a year's depreciation — none of which has
a bank row on the day it happens.

That is not a missing convenience. **A ledger that can only book cash states a cash flow, not a result.** A company that pays salaries
from the bank but cannot book the accrual ends up with a *debit* balance on `liabilities:payroll-payable` — a
liability the wrong way round, which is what a half-recorded transaction looks like from the outside. Any account
that is normally settled in cash after an accrual ends up in the same state, for the same reason.

### What it is

One route, one repository function (`repository/journal.py`), and no new posting path. `post_journal_entry`
calls `entries.post_entry` — draft, then seal, in one transaction — which is the same function `classify_line`
finishes through. So the balance rule, the two-leg rule, the account/commodity/precision rules, the book-year
rule and the period lock are not restated anywhere in the new module and cannot drift from the ones the importer
and the classifier are held to. The new module's whole job is the four rows around the entry: a proposal, a
decision, an application, and an `entry.posted` event carrying the reason.

**It is posted, not drafted.** A classification stays a draft until the pass over its statement is accepted,
because reading line 30 routinely changes how line 12 should read. A journal entry has no pass to belong to: it
is authored one entry at a time as a finished thought, and the correction path is the one every posted entry has
— `POST /entries/{id}/reverse`.

**It takes a `reason`, and `provisioning.add_account`'s precedent does not apply.** An account is structure and
has nowhere to keep a reason, which is why `POST /accounts` asks for none. An entry is an economic event: the
numbers are self-evident a year later and *why somebody accrued this, on this date* is not recoverable from
anywhere else. The reason is kept verbatim on the proposal, on the decision and on the event, exactly as
`classify_line` and `reverse_entry` keep theirs. `narration` and `reason` stay separate fields — one is what the
entry reads as in every report and export, the other is why it was written.

**Everything is validated before the proposal is written.** `validate_header` / `validate_legs` / `check_open`
run up front and then again inside `post_entry`. That is the rule `corrections.reverse_entry` already states: a
refusal must not leave behind the record of an authorisation for something that never happened, because a caller
handling the 422 inside its own transaction would carry a `journal_entry` proposal with no entry under it.
`entries._check_open` became `entries.check_open` for this — it was always the period gate the whole posting path
shares, and it is now named as one.

**The entry id is settled before the proposal**, so the proposal's subject names the entry it authorises, and so
two genuinely identical entries — the same supplier, the same amount, the same day, which is ordinary — do not
collide on `proposals.content_hash`.

### What it is not

- **Not a batch.** One entry per call. A period's accruals are a dozen calls, each independently refusable.
- **Not a second `/proposals`.** The E-chunk decision that "`/entries` POST is not the proposal endpoint" stands:
  there is one proposal endpoint and it is `/proposals`. This route *writes a proposal* the way every other write
  here does; it does not accept one.
- **Not an importer.** Adding journals to an already-imported book is still refused, and this does not change
  that. What changed is that there is now somewhere else for those entries to go.
- **No UI affordance yet.** The reviewer interface books lines off a statement; a form for a free-standing entry
  is a different page — a leg editor with a running "this does not balance yet" total, an account picker per leg
  (the one `POST /accounts` already feeds), and a date that has to be checkable against the lock before the
  submit rather than after it. That is a page, not a button, and the route is useful without it.

# Documents that are not bank statements

A document can enter the ledger without being a bank statement.

The evidence machinery was already there and already worked: `evidence_links` binds a file, a page, a locator
and a caption to an entry, and a statement load uses it for every row. What was missing was a **door**. The
only way to register a file was `POST /statements`, so an entity's `source_files` could hold only bank
statements. A supplier's receipt, a purchase invoice or a sales invoice that decides how a cost or a sale is
booked had to live outside the books, at best as an invoice number inside narration text.

That is worse than missing attachment support, because a common bookkeeping rule is **treatment follows the
document**: a full invoice means the cost is booked net with input tax claimed, no deductible document means it
is booked gross. A ledger whose accounting rule is about documents could not hold the documents.

## Two writes over two tables that already existed

`POST /documents` writes `source_files`. `POST /entries/{id}/documents` writes `evidence_links`. No new table,
and deliberately so — a second attachment table would mean `GET /provenance` had two places to look and would
eventually look in one of them. Because a filing *is* an evidence link, `GET /provenance?entry_id=…` answers
"what does this entry rest on?" with no read written for this chunk at all.

**Registration is content-addressed, so registering twice is not an error.** `source_files.sha256` is UNIQUE
and identity is the bytes, so the second call returns the first call's row, writes nothing, and answers `200`
with `created: false` instead of `201`. This is the opposite of `submit_statement`'s rule 4 and the difference
is the consequence: loading a statement twice would double a period's lines, so it must be refused; registering
a document twice does nothing at all, so refusing it would only teach callers to check first — and two entries
resting on one invoice is the ordinary case, not the exotic one. The single refusal is a claim that cannot be
true: the same hash submitted with a different `size_bytes`.

**The bytes are not uploaded.** `blob_path` records where the file is kept and the ledger does not fetch it,
which is the contract the statement input has always had and is why a loaded bank statement's `blob_path` is a
path on disk. A document store inside the database is a different project; what must not be lost is the hash,
the provenance and the reason.

**A filing carries a `locator`, a `caption` and a `reason`, all required.** Where in the document, what it is,
and why it belongs on this entry. A file dropped next to an entry with none of the three is not evidence.

## Migration 0004: filing a document against an entry in a closed period is allowed

`evidence_insert` refused an evidence link whose **entry** fell on or before the period lock. Migration 0004
removes that one conjunct and leaves the other two — `statement_assertions` and `opening_assertions` — exactly
as they were. `tests/test_migration_0004.py` reconstructs 0001's trigger from 0004's by putting the conjunct
back, character for character, so "we relaxed exactly one of three" is checked rather than asserted.

**Why the entry case goes.** The period lock protects *accounting* from change. An `evidence_links` row has no
amount, no account and no date of its own; every balance, report and export is identical before and after it.
Refusing it therefore bought nothing and cost a great deal: a document arriving after its month is closed is an
ordinary event — an invoice posted late, a receipt found afterwards, a supplier's copy requested in March for a
January cost — and the only way to file it was to reopen the month, record a close record and a reason, and
re-close, in order to write down a fact that changes no figure. In practice that means it is filed nowhere,
and the documents a year's figures rest on end up kept somewhere other than the books.

**Why the other two stay.** A statement assertion says *the closing balance was X, printed here*, and its
evidence link is where "here" is — the document **is** its authority. Adding a citation to a closed period's
assertion after the fact restates what a closed month's figures rest on, and a restatement belongs in a reopen.
An entry's authority is its own proposal and decision, both already written, which is what makes the entry case
different in kind rather than merely smaller.

The forbidden-mutation matrix records the change the way it recorded D4: the case moved from `refused` to
`allowed` with a note, rather than disappearing, and the two sibling refusals next to it are what makes the
decision legible.

## What this is not

- **Not an upload endpoint.** No multipart, no bytes through the API, no blob store.
- **Not a second provenance read.** `GET /provenance` was already the answer and still is.
- **Not filing against anything but an entry.** `evidence_links` can also point at a proposal, an assertion or
  an opening; those are written by the machinery that owns them. The gap this closes is the entry.

# Booked, awaiting a document

"Booked, awaiting a document" as a state the books can hold.

A line is often booked deliberately provisionally, pending a document that has not arrived. The provisionality
is real and consequential: when a supplier's full invoice turns up, a booking can change from a gross cost to a
smaller net cost plus recoverable tax. Nothing in the system recorded that a line was waiting on anything, so the
list of open items lived outside the books, and if nobody remembered, the line would simply stay wrong.

## Why a side table and not a status column

A posted entry is immutable, by trigger and on purpose, so a mutable `status` on `entries` was never available.
It would also have been the wrong shape. What is wanted is not the entry's current flag but the **history of a
judgement**: provisional from this date, for this stated reason, restated more precisely here, and then settled
by that document. `row_dispositions` already carries exactly that shape for `source_rows` — append-only rows
with a `supersedes_id` chain, where the live record is the one nothing supersedes — and `document_expectations`
follows it rather than inventing a second idiom for the same problem.

Three states, and the one that took a decision is the third:

| state | what it records |
|---|---|
| `awaiting` | the booking stands, and a named document is expected that may change it |
| `met` | the document arrived; `evidence_id` is the `evidence_links` row that is it |
| `withdrawn` | no document is coming, and the reason says why; the booking stands as it is |

**`withdrawn` is a separate ending, not a kind of `met`.** Sometimes no better document exists — a simplified
receipt is all the supplier will ever issue. Folding that into "resolved" would leave the list of what is
provisional right and the reason each thing left it wrong, which is a worse record than none: it would read as
though a document had arrived.

**`awaiting` is required free text.** *What* is expected is the whole content of the state. "This is
provisional" without saying what would settle it is a note to nobody.

**One live `awaiting` record per entry**, enforced by trigger. That is what makes "what is provisional?" a
query over rows nothing supersedes rather than a fold over a history. Calling the endpoint again **restates**
the expectation — a revision that becomes more specific is an ordinary thing — instead of opening a second one
beside the first.

## It clears by doing the real thing

Filing the awaited document against the entry (`POST /entries/{id}/documents`, see "Documents that are not bank statements") writes the `met`
record itself, under the filing's own decision, naming the `evidence_links` row that met it. There is no
"mark as resolved" endpoint and deliberately so: **resolving the state and doing the work are one act**. A tick
box would be a second thing to remember, and forgetting is the entire failure this answers. `POST /documents`
does not resolve anything — registering a file is not filing it against a booking.

The cost of the choice is that any document filed against an awaiting entry settles it, including one that is
not the document that was awaited. That is visible rather than silent — the record names the evidence link, so
what settled it can be read and disagreed with — and the alternative (asking the caller to confirm the document
is the right one) reintroduces exactly the optional step this is trying not to have.

## Two things that were wrong first, and are worth keeping written down

**Chain order, not clock order.** `created_at` is second-resolution. Three records written inside one second
sorted by their random uuids and the history rendered `withdrawn → awaiting → awaiting` — not merely unhelpful
but the opposite of what happened. `history_of_entry` now walks the `supersedes_id` links, so within a chain the
order is in the data. *Between* chains — an entry can await a second document after the first arrived — there
is no link to follow, and those are ordered by the order they were written.

**The record's id is settled before the proposal.** Awaiting the same document again after a withdrawal — same
entry, same words, same reason — hashed to the proposal written the first time and came back as
`409 immutable`, a duplicate of itself. Putting the expectation's id in the proposal's subject makes the second
one genuinely a different proposal. This is the same fix `journal.post_journal_entry` already makes so that two
identical entries (same supplier, same amount, same day — ordinary) do not collide on `proposals.content_hash`.

## No period-lock guard, and no close gate

There is no lock guard on `document_expectations`, for the reason migration 0004 gives for evidence links: it
has no amount, no account and no date of its own, and every balance, report and export is identical before and
after it. This is also the case that needs it most — the month in which somebody realises a booking was
provisional is very often not the month the booking is in.

**It is deliberately not a close gate.** Registering one would make every provisional booking block a close
until it was overridden, which is a change to what closing means and is not what this set out to do. The gates
stay as they are; what changed is that the question is now answerable.

## The month review

One line in the existing attention list, and a collapsible strip beside the statement list — the same shape the
statement provenance strip already uses, for the same reason: a reviewer needs the count without the detail and
the detail without it taking over the page. No gate panels.

Each row carries the one thing a browser can actually do: record that no document is coming, with a reason.
Filing the document is not a browser act — it needs the file — so the row does not pretend to offer it.

The count is of the whole list, with a note saying how much of it is older than the month on screen. That note
is written out in three cases rather than one, because "One booking is waiting · one from an earlier month" is
true and reads as two bookings. It is the same trap the month redesign already hit with the outstanding-line
count, and the same rule applies: a claim on that page has to be derived and checked, not asserted.

# Recoverable tax that was not recovered

Recoverable tax that was not recovered, and why.

A recoverable tax is sometimes deliberately not claimed, for a reason worth keeping: a booking made from a
simplified receipt that does not support a deduction, or a cost booked before any document existed at all. Only
the **effect** was stored — a larger number in an expense or an asset account — and the decision was not. So the
books could not answer *what tax are we not reclaiming, and why*, and an amount capitalised into a fixed asset
could carry unclaimed tax with the reasoning recorded nowhere in the books.

That is real money. Many regimes allow a claim for years after the document's date, so an invisible balance is
a balance nobody reclaims.

## Generality: what is modelled and what is deliberately not

`unrecovered_tax` holds an **amount**, its **commodity**, a required **reason**, and three optional fields. It
does not hold a tax name, a country, a return form or a deadline rule, and `src/` contains no such word.

- **`recoverable_until` is a date the caller supplies.** The ledger applies no regime rule and never will —
  four years in one country, two in another, none in a third is not knowledge a general bookkeeping ledger can
  hold without becoming a tax engine. Recording the date the caller computed is what turns the row from a note
  into an answer to "what can we still reclaim, and by when", at no cost in generality.
- **`tax_code` is the entity's own code**, and reuses the column name `accounts` and `postings` already carry.
  Nothing new has to be agreed on to write "input VAT" or "GST credit", and nothing in the ledger reads it.
- **`posting_id` is optional and says which leg the amount is hiding inside.** This is the specific failure of
  the fixed-asset case above: an amount capitalised into an asset is invisible in a way the same amount in an expense line
  is not. It must be a leg of the entry it claims to be in, by trigger as well as by pre-validation.

**The amount is positive.** This is an amount that was *not* recovered; a negative one would be a claim, and a
claim is a posting, not a note. It carries the same quantum trigger as every other amount column in the schema
— `unrecovered_quantum_insert` beside `postings_quantum_insert` and its siblings — because an amount that is
not a whole number of the commodity's smallest unit is not an amount in that commodity, and this column is no
exception just because nothing posts it.

## Nothing is posted, and that is the point

The arithmetic already happened when the entry was booked gross. What was missing was the other half of the
decision, and the entry could not carry it: a posted entry is immutable. So this is the third table in the same
append-only shape as `row_dispositions` and `document_expectations` — a `supersedes_id` chain where the live
record is the one nothing supersedes. The entry, its version, the trial balance and every report are identical
before and after, and there is a test that asserts exactly that.

## One live record per entry and commodity

By trigger, not by convention. The read's whole value is the **total**, and a total that can count one amount
twice is worse than no total: it would be a number that looks like an answer. A second recording for the same
entry and commodity is a `409` naming the record that already exists, rather than a quiet second row.

For the same reason the total on `GET /unrecovered-tax` is computed over everything in the requested state
rather than over the page. A total that changed as you paged would be a worse lie than no total.

## Two endings, and `forgone` is not a kind of `recovered`

`recovered` names the entry that recovered it — a **different** entry, because a recovery posts its own legs
and is its own economic event; naming the original entry is refused in the repository and by trigger.

`forgone` names nothing and its reason has to do the whole job: the window closed, the claim was not worth
making, the document never became good enough. Without it the list only grows, and a list of recoverable money
that includes amounts nobody can recover any more is the same failure `document_expectations` was written to
end. It would have been odd to reintroduce it two migrations later.

## No period-lock guard

Same reasoning as 0004 and 0005 — no amount posted, no account, no date of its own, no figure moved — and
again this is the case that needs it most. Establishing that a tax was not recovered, and why, usually happens
when the return is prepared, which is months after the month the cost is in. A record that could not be written
after a close would be missing exactly when somebody is finally in a position to write it.

## Where it surfaces

The Reports page, below the balance sheet: the total per commodity, collapsed, expanding to one row per booking
with its reason, its tax code and its last recoverable date. It belongs there and nowhere else — it is a fact
about a **period**, and it is the one fact about a period that the income statement and the balance sheet
structurally cannot show, because the amount is *inside* their figures, folded into an expense or capitalised
into an asset. Collapsed by default because most periods have none, and because a period that has some needs
the reason more than the number: the number is recoverable money and the reason is what decides whether it
still is.

---

# Issue 0004 — the reconciliation extension, replaced

## Five records, and "live" is a view

Loading one statement used to write nine kinds of row across seven tables. Most of them existed to satisfy a
trigger rather than to record anything anybody had said: the `source_configs` row was created on demand purely
so the `period_overlap` guard had something to join through, and the declared **opening balance** lived in a
proposal payload because the schema modelled the closing balance twice and the opening not at all.

What replaces it is what the task actually is: a **document**, the **statement** printed on it, that statement's
**lines**, and an append-only **explanation** per line whose allocating kinds carry **allocations** against
postings. The opening balance is a column. The overlap trigger compares live statements directly.

**"Live" is derived, not stored** — a statement nothing replaces, an explanation nothing supersedes — which is
the answer to the old model's several overlapping notions of "current". There is one question ("what is this
line now?") and it has one answer, computed in SQL by `live_statements` / `live_explanations`.

## The draft/accept pass is gone, and that is a product change

Before this, classifying a line produced a **draft** entry that became real only when the pass over its
statement was `accept`ed. That existed because a pass over a statement is revisable inside itself: reading line
30 changes how line 12 should read, and sealing as you went cost a reversal pair for every second thought.

Issue 0003 had already made ordinary writes direct, and issue 0004's command language has no accept step in it:
*book a line, link it, ignore it, or change your mind.* So `book_line` **posts**, and changing one's mind is
`reexplain_line`, which corrects or reverses through the ordinary correction path. The cost is real — a second
thought about a booked line now leaves a mirror in the books where before it left nothing — and it is accepted
deliberately, because two ways for an entry to reach the books is the complexity this roadmap exists to remove.
`POST /statements/{id}/accept` and `tests/test_draft_accept_close.py` went with it.

## Replacement refuses while a line is still booked

`replace_statement` keeps the predecessor, points the successor at it, and the database allows at most one
successor (`statements.replaces_id` UNIQUE). Replacing **retires** the predecessor's lines: `live_explanations`
requires a live statement, so the allocations underneath stop consuming their postings the moment the input they
rested on is replaced.

That is right for a link and for an ignore, and wrong for a **booking** — an entry is in the books and would be
left with nothing explaining why. So a replacement is refused while any predecessor line has a live `booked`
explanation, and the refusal names them. Undo those first. The alternative considered was reversing them
automatically inside the replacement; it was rejected because a command called "replace this statement" should
not silently post accounting.

## One document is one **live** statement

`documents.sha256` is UNIQUE — the same bytes are one document however they are named. "That one document is
loaded once" was a second key, `UNIQUE(statements.document_id)`, and it turned out to be the wrong invariant:
it said *loaded once, ever*, which made the commonest correction there is impossible.

The case that broke it is the ordinary one. An agent reads a CSV, normalises a 6 March transaction as 5 March,
and loads it. Nothing has been booked yet. The correction is to re-read *the same download*, properly, and
submit it through `POST /statements/{id}/replace` — the command that exists for exactly this. It was refused
`document_already_loaded`, because the document already belonged to a statement. Correcting a parsing mistake
therefore required either different bytes or a hash that lied about the bytes there were, and acceptance
criterion 3 ("a mistaken load can be safely replaced") was not met for the single-account case at all.

So the invariant is now **one live statement per document**, and it is a trigger
(`statements_insert` → `document_already_loaded`) rather than a key, because no key can express it: liveness is
derived from whether another row replaces you. The statement being replaced is excluded from the check, since
at insert time it is still live and is precisely the row being superseded. `repository/statements._refuse_reload`
is the D6 pre-validation in front of it, and names the live statements the document is already loaded as.

What this does **not** change: loading the same bytes as a *second, concurrent* statement is still refused,
including against a different account — so "load it twice and double the period's lines" remains impossible.
A document that is *registered* but never loaded as a statement is unaffected, so filing evidence is not
restricted.

**It does not unblock the single PDF covering two accounts**, and that is deliberate. That case needs two
*simultaneously live* statements over one document, which is the thing this rule still forbids. Relaxing the key
to `(document_id, account_id, commodity)` would allow it in one line — but it would also let the same download
be loaded twice against two accounts by mistake, and nothing else in the schema would catch it, because
`period_overlap` is per subject too. That trade is worth making when somebody actually has the multi-account PDF
in front of them and can say what the correct refusal looks like; until then the narrower rule is the one that
can be checked. The limitation stands, with a smaller boundary than before: one document, one subject, one live
statement, correctable forever.

## A line's source key includes its position

A statement line's identity within its statement is the caller's `source_key`, else the bank's own reference,
else a digest of what the line says **and its line number**. The position is load-bearing: two identical coffees
on one day are two lines, not one line loaded twice, and a key derived from the content alone would refuse the
second. Identity is scoped to the statement, so a replacement may restate every key.

## Reconciliation is switchable, because the exit gate demands it

"Optional" has to mean operable. `haddon/extensions.py` reads `HADDON_RECONCILIATION`; with the extension off
its routes are not mounted, `/capabilities` does not advertise them, and the three reconciliation close gates
return a **pass** whose reason names the switch. That last one is the only place in `close_checks` where "I
found nothing" is allowed to mean "there is nothing" — with nobody expected to load a statement, demanding one
would make an optional extension compulsory through the back door.

The switch is a process setting rather than a per-entity column: turning an extension on or off changes which
commands exist, so it belongs with the other deployment configuration. A stored flag would make the published
surface vary by row, which is the shape issue 0006 is trying to remove.

## Balance and reconciliation are different questions

A statement can be arithmetically perfect and have every line unexplained. `GET /reconciliation` therefore
reports `agrees` (does the bank's figure match the books?) beside `explained` (does the journal account for
every line?), and never folds one into the other. The ledger side of that comparison is
`reports.balances.account_balance`; it is a cumulative per-account balance, not the balance-sheet aggregation.

## Migration 0009 carries the evidence links it can, and refuses the ones it cannot

0009 drops and rebuilds `evidence_links` on `document_id`, because the old table's `assertion_id` and
`opening_id` targets are two of the tables 0009 discards. The rebuild originally copied nothing: a database
holding a posted expense with an invoice filed against it came out the other side with the document intact and
the *link* — its entry, its page, its locator and its caption — gone. The refusal that guards the rest of the
reconciliation schema did not cover `evidence_links`, so this was silent.

Both halves of the fix follow from the same rule the migration already states: **refuse rather than discard, and
carry source data across**. An evidence link filed against an **entry** is source data, it is not part of the
matching model 0009 replaces, and it maps to the new table column for column — so `upgrade` reads those rows out
before the drop and writes them back after, with a count check like the one `documents` already has. A link
pointing at a **proposal, a statement assertion or an opening assertion** has no target in the new schema at all,
so it joins the other un-mappable rows in `refuse_discarded_reconciliation` and stops the migration.

The alternative — extend the refusal to cover every `evidence_links` row — was rejected because it would refuse
a database whose only reconciliation content is an invoice filed against an entry, which is both a common state
and one the new schema can hold perfectly.

## A locked period refuses to release an allocation, not only to create one

`allocations_insert` checks two dates: the statement line's, and the date of the entry the posting belongs to.
That is the right rule for *making* an allocation. Nothing checked either date when an allocation stopped being
live, and there are two ways for that to happen — superseding the explanation that owns it, and replacing the
statement its line belongs to. Both were reachable with the posting's period closed:

> A 5 March line is linked to a 10 February cash posting while both months are open. February is then closed.
> Re-explaining the March line as `ignored`, or replacing its March statement, both succeeded — and February's
> posting lost its live allocation, so a closed month's postings became unexplained with no reopen anywhere in
> the record.

"A locked period fails closed" has to hold in both directions or it does not hold at all (issue 0004, pitfall 3).
The entry-date check that *was* there only fired for a `booked` explanation, which has an `entry_id` to look at;
a `linked` explanation has none, and that is the whole gap. So both paths now check the postings themselves:
`lines._check_allocations_open` before superseding, `statements._refuse_locked_allocations` before replacing,
with `explanations_insert` and `statements_insert` raising `period_locked` / `old_period_locked` underneath as
the guarantee (D6). The remedy in both refusals is `reopen`, because that is the only honest way through.

## "Live" is derived once, and Python reads the same derivation SQL does

Migration 0009 derives liveness in SQL: `live_statements` (nothing replaces it), `live_explanations` (nothing
supersedes it **and** its statement is live), `live_allocations`. The Python readers each rebuilt the definition
inline, and one of the two halves is easy to forget — three of them checked only `supersedes_id`.

The consequence was a close gate reporting a state the database did not hold. Link a −3.00 line fully to a −3.00
posting, replace the statement, link the replacement's line to the same posting: SQL holds **one** live
allocation and is right, while `close_checks` summed both and reported `allocated_micros: -6000000`,
`unallocated_micros: 3000000`, failing `nothing_partial` on a month that was correctly reconciled. A gate that
blocks a close over an allocation the database has already retired is worse than a gate that is merely wrong —
it is unarguable-with from the outside.

`tables.live_statement_clause` and `tables.live_explanation_clause` are now the single Python definition, sitting
beside the table declarations because that is where the SQL views they mirror live, and every reader composes
them: `close_checks`, `lines`, `statements` and `reconcile`. The fix is one definition rather than four patched
call sites, on purpose — the next reader added will get it right by construction.

## Ingestion records what it is told about the bytes, and does not read them

`POST /statements` takes a `sha256`, a size and either a `blob_path` or an `external_ref`. It stores all of them
and **never opens the file**. So a document loaded from `/tmp/statement.csv` with a correct hash keeps that hash
after the file at that path is overwritten, and nothing reports an integrity error; the fixture writer in
`tests/statement_helpers.py` has the same hole by construction.

This is a **known limitation, recorded rather than fixed**, and it is worth being explicit about why:

* it is not a regression — the discarded `source_files` model did exactly the same thing;
* the ledger deliberately does not fetch documents. "There is no reader here, and there never will be" is the
  first line of `repository/statements.py`, and reading local bytes at ingestion would be the ledger fetching a
  document under another name;
* it would only ever cover `blob_path`. An `external_ref` names bytes somebody else holds, and half a guarantee
  advertised as a whole one is worse than a stated limitation;
* the hash is still doing real work. It is the identity key, so the same download cannot be loaded twice, and it
  is what an auditor re-computes by hand against the original.

**What the record therefore claims** is: the agent told us these bytes hashed to this, at this location, at this
time — and that claim is immutable and permanent. It does not claim the ledger verified it, and no document
should say it does.

**The trigger that would force the fix**: the first time Haddon *serves* a stored document — an endpoint that
returns the blob, or a reviewer UI that renders it — the bytes stop being a reference somebody else can check
and become something this service vouches for. At that point ingestion must hash-and-store the bytes in a
content-addressed blob store keyed by the digest, and serving must verify on the way out. Storing a hash of
bytes we are about to hand back unverified would be the actively misleading version.


---

# Issue 0005 — the period close, completed

## A close is a date, a reason and a version

The README promised an ability nobody could perform. `closing.close` existed, and it worked, but it demanded a
`snapshot_id` and a `decision_id` the caller had to mint first — so "an agent can close a period" was true only
of a caller that knew the shape of two internal tables. There was no route at all.

`commands.close_period(ctx, through=…, reason=…, expected_version=…, overrides=…)` is the whole surface now, and
`POST /v1/entities/{e}/close` is its face. In one transaction it evaluates the checks, refuses a blocking one
that no override names, computes and stores its own verification snapshot, advances the scalar lock and writes
one audit action carrying the checks, the overrides and the snapshot's hash.

**`expected_version` is not ceremony and the idempotency key does not replace it.** They answer different
questions, and the issue's acceptance criteria run them together. The key answers *"is this a retry of the
attempt I already made?"* — same key, same body, the stored response replays and nothing is written twice. The
version answers *"am I closing the books I was looking at?"* — two agents closing the same month from two
readings of it is exactly the race the scalar lock exists for, and the second one must fail rather than close
over a period it never saw. A close needs both, and `tests/test_api_close.py` holds the two apart.

## The snapshot is computed, and it verifies something

Pitfall 2 says do not trust a snapshot hash supplied by the caller. The stronger form is available here and is
what was built: there is no parameter to supply one. `closing.verification_snapshot` reads the posted journal as
at the close date, hashes every (account, commodity) balance with the chart and commodity counts beside it, and
**checks that each commodity nets to zero across the whole chart** before it will write the row.

That check passes today because every entry is refused unless it balances — which is exactly why it is worth
making. The day it does not pass, the invariant has been lost somewhere the posting path cannot see, and a close
is the last moment anybody would notice.

This is deliberately **not** the exporter's snapshot. That one renders hledger journals and puts them to the
pinned binary; it is worth having and it is an *artifact extension*, which the journal core may not import
(`architecture.md`). The two share the `snapshots` table and its "one state is one snapshot" rule
(`state_hash` is UNIQUE), which is why both look the row up by hash before writing one: closing through a date,
reopening and closing again over an unchanged book re-derives the same hash and reuses the row.

**A consequence worth stating.** A book that cannot produce a trial balance cannot be closed. The one place this
showed up was `tests/test_no_float_reaches_db`, which deliberately posts a signed-64-bit extreme to prove it
reaches the database as an integer; summing it with the rest of the month overflowed. The entry moved to a date
after the close rather than the check being weakened, because "the books do not add up, so they do not close" is
the right answer.

## The override is a sentence, not an id

Before this, closing over a blocking gate meant calling `record_gate_override` to write a proposal and an
`override` decision, then passing the decision's id back into `close`. It was auditable, and it was
unperformable by the caller the feature exists for.

An override is now `{gate, reason}` — a check's name and why. It is matched against the checks **as the server
has just evaluated them**, which is what makes a blanket bypass unrepresentable rather than merely discouraged:
there is no wildcard to spell, and an override that names a check which is not currently blocking is a `422`
(`override_not_blocking`), not a no-op. A list assembled once and replayed at every close therefore stops
working the moment its check goes green.

**Two refusals, deliberately not merged.** `409 close_not_eligible` names every check that is blocking and what
is outstanding in it; the `422` says an override answered nothing. They are different things to tell a caller
and folding them together would make the second unreadable.

**Where a waiver lives.** On the close record, in `close_records.overrides`: the check's status and outstanding
count as they stood, the acting principal, and the reason. Not a table of its own, because an override has no
life apart from the close it let through — a row that outlived its close would be a standing permission, which
is the thing the fail-closed rule exists to prevent. The database holds the other half of that: a `CHECK` that a
`reopen` waives nothing, since a reopen is not gated on the checks and so can never have waived anything.

`eligible` still never becomes true because of an override. Only `may_close` does, and the real finding stays
visible beside the waiver.

## "Authorized" means recorded, not enforced

The issue asks for an "authorized, reasoned override" and for the UI to hide actions "the authenticated role
cannot perform". There is no role model in Haddon and this did not add one: the 2026-09-11 § "Authorisation"
decision stands — the model records who acted and does not decide who may, and no code branches on actor type.

So "authorized" is honoured as *the acting principal is recorded*, on the waiver and on the close record's
`created_by`. The UI gates on the write-vs-read **credential**, which does exist: the Close page offers a write
only when `POST /close` is in the published `writes` for this caller. That is a fact about the served surface,
not a permission.

## Reopen, named two ways

`reopen_period` takes `through` (a boundary: "open March again") or `close_id` (an action: "undo that close"),
exactly one of them. A close resolves to the boundary it advanced **from**, which is the only reading of "undo
it" that leaves the lock where it stood before.

It is not gated on the checks (pitfall 1) and nothing is undone: the close record and the snapshot it was taken
against stay exactly where they are, and a second record says the lock moved back and why. Undoing a journal's
*first* close is refused with `would_clear_the_lock` rather than silently reopening to some other date — U7 says
the lock may never return to "nothing closed", and reaching that state by a different route would still be
reaching it.

## The policy is a literal, and it lost a failure mode

`close_checks.py` had a `Gate` base class, a `_REGISTRY`, a `register_gate` call and a `REQUIRED_GATE_KEYS`
tuple — a wiring framework for three statically installed checks that were all registered at import in the same
file that defined them. It is now a tuple of `Check` records over plain functions.

The interesting part is what that deleted. `REQUIRED_GATE_KEYS` existed so that a key declared required with no
implementation registered would be a blocking `unevaluable` rather than a silently easier close. With a literal
policy that state is **unrepresentable**: the declaration *is* the implementation. The mistake it guarded has
not gone away, though, so the guard did not either — it moved to the one shape that can still happen, an
extension switched on whose checks were never installed in this process, which blocks and names the extension.
Reverting that line turns `test_an_extension_that_is_on_but_not_installed_blocks_instead_of_passing_vacuously`
red, which is how it was checked.

## The checks are split by what they can be evaluated without

`close_checks.py` was core and read `statements`, `statement_lines`, `explanations` and `allocations` directly;
`tests/test_core_boundaries.py` pinned it as the single allowed core-to-extension table coupling and named this
issue as the one that removes it. The allowlist entry is **deleted, not moved**.

What made the split awkward was `nothing_unexplained`, which answered two different questions at once: a draft
*entry* (core, evaluable with nothing loaded) and an unexplained statement *line* (reconciliation, evaluable
only once a statement covers the cell). They are two checks now — `nothing_in_draft` in the core, and
`lines_explained` with the extension — and the reason that is a simplification rather than a rename is what it
removed: with the extension off, the old single gate had to fake a pass for the half that no longer applied.

**A switched-off extension contributes no checks rather than contributing passes.** "Every statement is loaded"
is not a check that succeeded when nobody was expected to load one; it is a check that does not apply, and
recording it as a pass would make an optional extension compulsory in the audit trail. What stops the shorter
list from being mysterious later is `checks_for_audit`, which writes which extensions were running onto the
close itself — so a close read years from now can tell "all green" from "fewer questions asked".

The extension registers its checks with the core on import (`close_checks.register_checks`), the same direction
`entries.register_allocation_guard` already goes: the dependency points from the extension to the core and never
back. `haddon.api.app` imports `close_coverage` when it mounts the extension's routes, and `tests/conftest.py`
does the same, because installing an extension is a composition-root job.

## One definition of "live", enforced structurally

Phase 3's review found the root cause under several bugs: the two-part definition of live — nothing supersedes
it **and** its statement is live — was reimplemented in Python five times, and three copies dropped the second
half. An earlier fix (issue 0004, "'Live' is derived once, and Python reads the same derivation SQL does") wrote
the derivation down once as `tables.live_statement_clause` / `live_explanation_clause`
and fixed the individual readers; this finished the job (the remaining hand-rolled sites in `close_checks`,
`reconcile`, the statements route and three callers that must *name* the replacement, now
`statements.successor_of`) and added the thing that keeps it done.

`tests/test_one_definition_of_live.py` walks the AST and **resolves table aliases**, which is the whole point:
every one of the drifted readers spelled it `successor.c.replaces_id == t.statements.c.id`, so a check matching
on the literal table name would have waved all of them through. It was verified by planting that exact pattern
back into `reconcile.py` and watching it fail. It also compares the Python clause against the SQL view as sets
of ids on the seeded fixture, because one definition in Python is worth little if it is a different definition
from the database's.

## Closing is in the interface, reversing an earlier decision

The G4 note said: *"Accept and close are not in the UI. Both already have their own authorisation trail through
the API, and a second way to do them from a browser would produce the same state with a different record behind
it."* That reason is gone. There is one `close_period` handler; the browser reaches it through `/ui/v1` like any
other caller, and the record behind a close from the page is the same record as a close from an agent.

It is a page of its own rather than part of the month review, for the reason the month review already gives: a
check reports over the whole span a close **newly** closes, which with no prior lock starts at the first day of
the earliest book. Putting January's findings under a heading that says March was the mistake that kept the
gates off that page, and it would still be a mistake.

`unevaluable` is drawn differently from `fail` there. "I could not tell" is a weaker claim than "here is what is
wrong", and a page that drew them the same would invite a reviewer to override a check that had not found
anything.

## The approval chain is deleted

`proposals`, `decisions`, `applications`, `rule_versions` and `rule_authorities` were the shape authority used
to arrive in. Issue 0003 stopped ordinary writes manufacturing one, issue 0004 stopped reconciliation writing
one, and this issue converted the last four holders — document registration and filing, provisional bookings,
unrecovered tax, and the close-gate override — each to the direct audit action it should always have written.
Migration 0010 then drops the five tables, the `decision_id` column from the four that carried one, the
`/proposals` routes and `repository/proposals.py`.

**The old rows are not converted.** A proposal payload cannot become an audit action: an action records a
command somebody ran, and a proposal is not one. Haddon has no deployed database, so the migration **refuses** a
database that still holds proposals rather than discarding them, and a development database is rebuilt from its
source journals. `close_records` is the exception, because a close is an accounting fact: its rows carry across
with `overrides='[]'`, which is honest — every close that already exists was recorded under the old chain and
the new column can only describe the new one.

Downgrade is refused. Three of those `decision_id` columns were `NOT NULL` at 0001 and there is no decision left
to point them at; inventing one to satisfy a constraint would be inventing history.

## What 0005 left for 0006

Deliberately not done here, because it is the next issue's subject and doing it in passing would be a second
half-built surface: **generated, permission-aware capability discovery**. `api/routes/entity.py` still publishes
a hand-maintained list of paths, updated here to drop `/proposals` and add `/close` and `/reopen`. Account
lifecycle commands and the remaining reconciliation commands are 0006's too.

## The 0005 review, and the two things it found

Both findings were reproduced before anything was changed, and neither was a regression — which is the point of
writing them down here rather than quietly fixing them.

### The close window began after some of what the close was locking

`nothing_in_draft` asks *"is any entry dated in the period still a draft?"*, and it asked it over the window
`_window` computes. With no prior lock that window began at the earliest **posted** entry — so a draft dated
before the first posted entry was never in it. Closing through 2026-03-31 over books whose earliest posted entry
is 2026-03-10 and whose earliest draft is 2026-01-15 recorded `nothing_in_draft: pass — "no entry in the period
is still a draft"`, and then `seal` on that same draft answered `409 period_locked`. Two readings of one period,
and the recorded one was the false one.

The earlier `NothingUnexplained._drafts` behaved identically and the core/extension split carried it through
unchanged, so this is older than 0005. It is 0005's to fix anyway: the issue's exit gate is that an agent can
maintain a normal period using the published commands alone, and being told the books hold no drafts when they
hold one defeats that with a green tick.

**The fix is in the window, not in the check.** The lower bound on a first close was never a policy — a close
locks *every* date on or before `through`, so the honest span is "the beginning of the books through `through`"
and the bound is only there to stop the month iteration running back to year zero. The right bound is therefore
the earliest date the books hold anything a check can have a finding about, which is the earliest entry in
`draft` or `posted`.

`void` is deliberately left out. Nothing a close asks about has a finding to make about a voided entry — it is
not bookkeeping the books hold — and counting it would be permanent: voiding an entry misdated by a decade would
make every first close thereafter owe statement coverage for the empty decade in between. The narrower rule is
also exactly sufficient, because a draft is never void.

**Why widening the shared bound is safe.** `window_start` and the months derived from it are what
`statement_coverage`, `lines_explained` and `nothing_partial` all filter on, so moving it moves every check.
Moving it *earlier* can only ever give a check more to find: more months to demand a statement for, an earlier
floor on every `dt >=`. It cannot turn a `fail` into a `pass`, and
`test_widening_the_first_window_never_narrows_what_the_other_checks_report` pins that direction rather than
trusting the argument.

**An adjacent gap, named and not fixed here.** The same bound hides statement *lines* the same way: load a
January statement, book nothing, and a first close through March never looks at its unexplained lines, because
the window starts at the earliest entry and January holds none. Fixing that properly means the core computing a
bound from tables it must not read (`architecture.md`), or a second registration from the extension — a design
call bigger than this review, and one that belongs with whoever owns the reconciliation checks next. It is
recorded here rather than left to be rediscovered.

### "Link a completed close to its immutable snapshot" was not built

The sixth acceptance criterion, and the "Delivered — all of it" claim above it, were both wrong about it. A
close *returned* `links.snapshot` in its 201 and the UI never rendered it; and `close_records` had **no reader
at all**, so once that response was gone the only route back to the state a past close was taken against was to
walk `/events`. The issue's "Delivered" note has been corrected rather than left standing.

`GET /v1/entities/{e}/closes/{id}` is that reader. It is shaped like `/snapshots/{id}` and `/exports/{id}` next
to it — an immutable row, fetched by id, no listing — because reading the *history* of closes is the reviewer
loop's surface and that is issue 0007's. The entity summary now carries `lock_close_id`, so the path from a
standing lock to its evidence is two hops and needs no event walk: summary → close record → snapshot.

---

# Issue 0008 section A — protocol and operational correctness

Three fixes that do not depend on the redesign, landed ahead of the rest of 0008 because the issue says
correctness fixes that stand alone can. The other section A items — response models on write routes, and
deriving advertised capabilities from the route registry — belong to issue 0006's command surface and are not
touched here.

## A wrong verb is the caller's mistake, and now says so

`errors.install` rendered every Starlette-raised `HTTPException` but 404 as `internal_error` *at the original
status*. So a `DELETE /accounts` came back `405 internal_error`: a body that tells a caller this service has
broken, when what happened is that they used a verb the path does not serve. There is nothing to retry and
nothing to report, which is exactly the answer that wastes an afternoon.

`method_not_allowed` (405) joins the domain error table, and the handler maps by status instead of by a single
special case: 401/403/404/405 to their own codes, any other 4xx to `validation` — the request was wrong — and
only a 5xx to `internal_error`.

**`Allow` is computed here rather than forwarded.** Starlette raises its 405 from the first route whose *path*
matched, so its `Allow` names only that route's methods: on `/accounts`, served by one route for `GET` and
another for `POST`, it says `GET`. An incomplete `Allow` is worse than none — it tells a caller the verb they
wanted is unavailable when it is right there — so `errors.allowed_methods` unions the methods of every route
whose path matches. The same verbs go in the envelope's `details`, because a JSON client reads the body and
should not have to parse headers to find out what it may do instead.

The one route left out is the reviewer bundle's history-API fallback, and **what excludes it is a mark on the
endpoint (`claims_every_path`), not `include_in_schema`.** The two are different questions and the first
attempt conflated them. What disqualifies the fallback is that its pattern matches every path, so it is
evidence about none: counted, it would put a `GET` in the `Allow` of a path that serves only `POST`. Being
unpublished is a different property, and using it as the proxy left `/openapi.json` and the favicons — real
routes this app serves and simply does not put in its own schema — out of their own answer, so a `POST` to
`/openapi.json` came back 405 with **no `Allow` at all**. Marking the fallback instead also keeps the fix from
being "publish the UI route", which would have bought the same exclusion by putting a browser route in the API's
contract.

**An empty union is a 404, not a 405.** If no route defines the path with any verb, the only thing that matched
was the fallback's pattern, and a fallback is not a resource: `POST /v1/entities/{e}/no-such-thing` is not the
caller reaching for the wrong verb, it is a path that does not exist — which is what a `GET` to it already
answers. Returning 405 there was also unserveable, because there were no methods to name and RFC 9110 §15.5.6
makes `Allow` mandatory on a 405. Answering 404 is what makes the rule above hold without exception: **every
405 this service sends carries a complete `Allow`.**

That walk reaches into how FastAPI nests included routers, which is why the test asserts the actual verbs and
not merely that a header came back: if an upgrade moves them, the union would silently go empty — and now that
an empty union means 404, a regression would turn every 405 into a 404 rather than quietly dropping a header,
which `test_every_405_carries_the_complete_allow_it_promises` catches either way.

## Cursors are signed with a configured keyring, not a secret the process invented

`ApiConfig.cursor_secret` defaulted to `secrets.token_hex(32)` — a fresh value per process. Every open
pagination cursor therefore became invalid the moment the service restarted: a deploy, a crash, a systemd
`Restart=always`. The caller sees `422 cursor_invalid` in the middle of a walk it did nothing wrong in, and
there is no way for it to tell that from a cursor it corrupted itself.

`cursor_keys` is now a **keyring**, newest first:

```json
"cursor_keys": [{"id": "2026-09", "secret": "..."}, {"id": "2026-06", "secret": "..."}]
```

The first key signs; every key verifies. So a key is rotated by prepending the new one, and retired by dropping
the old one once the cursors it signed have aged out — no flag day, and no window where in-flight cursors break.
`cursor_secret` remains as the one-key spelling of the same thing (`{"id": "default"}`); giving both is refused
rather than silently resolved.

The cursor payload carries `kid`, the id of the key that signed it, and `VERSION` is 2. Reading the key id out
of the body before the body is trusted only narrows *which* secret the signature is checked against — a cursor
naming a key it was not signed with fails the same comparison as one that was edited — and a cursor naming a key
this service no longer holds is refused, which is what makes retiring a key mean something. `kid` is not `k`:
`k` is already the collection, and `encode` refuses a payload carrying either of the envelope's own names rather
than letting one silently shadow the other.

**A service with no configured key is `not_ready`.** It still works; it stops working at the next restart, and
that is a deployment that cannot do its job across a restart. `/readyz` grew a `cursors` check that says which
key signs and how many verify, so an operator finds out before their callers do.

## The one filter that cannot run before the page, and how the response says so

Issue 0008 asks for derived filters to be applied before pagination "where practical". Across the list routes
they already are: `/statements?live` is a correlated `NOT EXISTS`, `/provisional` and `/unrecovered-tax?state`
have `awaiting_clause` and `state_clause`, and `/entries?status` and `/accounts?archived` are columns. Two are
not — and one of those two, `/proposals?status`, is moot: issue 0005 deleted that endpoint with the approval
chain. That leaves `GET /lines?state`.

It stays after the page, deliberately. A line's status is derived from the whole chain of explanations written
for it, their allocations, the entries behind them and whether the statement it came from has been replaced
(`lines_repo.line_state`). Re-expressing that in SQL would be a **second implementation of what a line is**,
able to disagree with the first — the fault this codebase already has too much of ("Live" is derived once,
above) and a worse one than a short page.

So the response makes the consequence explicit instead. `filtered_after_page` lists the filters applied after
selection — `["state"]`, or `[]` when the query did all the filtering itself. While it is non-empty, a page
holding fewer items than `limit` is **not** the last page: with a sparse filter the first pages come back
*empty* with `next_cursor` still set. `len(items) < limit` is the end-of-results signal every other collection
uses, and on this one it is not that signal, which is why the page says so rather than leaving it to prose.

`test_a_client_that_follows_the_cursor_to_the_end_sees_every_line_in_the_state` walks a twelve-line fixture
whose only unexplained lines are the last two, and pins the whole contract: the first page is empty with a
cursor, the walk terminates, nothing is missed or served twice, and the result equals what a caller would get by
reading everything and filtering it themselves.

**Running after the page does not excuse it from the cursor.** `state` was the one filter left out of
`filters_digest`, so a cursor issued under `state=unexplained` was honoured when replayed under
`state=booked` — or under no filter at all — while changing `from` was correctly refused. A cursor is a
position in *one* listing, and a position measured against rows the caller has stopped asking for belongs to
neither listing; that `state` is applied later than the others is a fact about the query plan, not about what
the caller asked for. It binds like the rest now, sorted first, because the order a query string repeats a
filter in does not change which lines are kept and a cursor should not be refused over that.

**And the types the interface reads declare only fields the service sends.** `Expectation` and
`UnrecoveredRecord` in `web/src/lib/types.ts` still declared a non-optional `decision_id` after migration 0010
dropped the columns — a promise `tsc` endorses happily, because it checks the code against the declaration and
has no opinion about whether the declaration is true. `tests/test_ui_types_are_the_contract.py` compares each
against a payload the API actually sends, which is the only place the two halves of that claim can meet.


---

# Issue 0006 — the agent command surface and the generic bootstrap

## A write is a command, and its name is the one string

Before this, the write surface was described three times and the three could disagree. A route had a path; the
capability list in `api/routes/entity.py` had a **hand-written copy** of that path; the function that did the
work had a name nothing published. The list had already been wrong in both directions during the roadmap — it
advertised `/proposals` after issue 0005 deleted that surface, and routes were added without touching it.

Every mutating route now declares `operation_id` = its command's name, and `api/surface.py` derives the
published reads, writes and command names by walking the routers the process actually mounted. So the string an
agent reads in `/capabilities`, the OpenAPI `operationId` and the function in `haddon.commands` /
`haddon.reconciliation_commands` are **one string**, and the list that could drift does not exist.

This is derivation, not a registry. Issue 0005 deleted a registration framework for three statically-known
checks and replaced it with a literal, for good reasons; the temptation here was to build the same thing under
a new name. What made it unnecessary is that the declaration already existed: `include_router` is the
registration, and reading it is free.

**Reads are derived too**, which is the specific thing issue 0008 section A asked for and held back for this
issue. A read route dropping out of discovery is a capability a caller is told it does not have, and the
symmetric failure was the one nothing was watching.

**The walk has to recurse.** `include_router` does not flatten its routes into `app.routes` on current FastAPI —
it appends an `_IncludedRouter` wrapper — so a one-level scan finds the four documentation routes and nothing
else, and publishes an empty capability list. `tests/test_api_capabilities.py` therefore asserts the derived
set **equals** the OpenAPI document's rather than asserting it is non-empty; a service that can do nothing and
a service that says nothing look identical from the second kind of test.

## "Permission-aware" means what it can mean here

Phase 4 asks for capability discovery that is permission-aware. Haddon records authority and does not enforce
it (see § "Authorisation"): there is no role model, no read-only credential, and no code branches on actor type.
Inventing one to satisfy the word would have been a worse answer than honouring it.

So the surface is scoped to *this entity* — which the credential had to be scoped to in order to reach the route
at all — and `entities_in_scope` names the others it may address. The other half of "what can I actually call"
is the extension switch, which is real: with reconciliation off its routers are never mounted, so none of its
commands are published, and `tests/test_reconciliation_optional.py` pins the exact difference the switch makes.

One consequence worth stating: the surface is fixed when the app is built, not per request. That is the honest
shape — the served routes cannot change under a running process — and it is why those tests build an app per
switch state instead of calling one function twice.

## `create_account` writes an audit action, and that was a hole

`POST /accounts` was the **only write in the service that recorded nothing**. It grew `accounts`,
`account_commodities` and the request row and left the log untouched, so "who extended the chart, and when" was
answerable only from `created_by` on the row itself, and "why" not at all. An earlier change pinned the gap in
`test_api_spine.py` with a comment naming this issue rather than fixing it, which is the right way to leave a
known hole and is why it was still findable.

It is `commands.create_account` now and writes one `account.created` action like every other command.

**`reason` is optional on creation and required on archiving**, which is the one asymmetry here. The old schema
argued at length that an account has nowhere to keep a reason, so asking for words the store would drop was
worse than not asking — a good argument that stopped being true the moment there was an action to keep them on.
It stays *optional* because adding an account is structure rather than an assertion about what happened;
`initialize_entity` already had that shape, recording its own sentence. Retiring an account is a decision about
the books — it is visible in every later report as an account nothing moves on — so `archive_account` requires
one.

`archive_account` had existed in the repository since the beginning with **no route at all**. It takes
`expected_version`, the same concurrency answer `close_period` gives, and it does not reach backwards:
everything already posted to the account stays posted and keeps appearing in every report covering its date.
A test asserts the entry *and* the refusal, because a test that checked only the refusal would pass against an
implementation that deleted the account's history.

## `correct_entry` had no way to call it

The command existed from Phase 1 and the only published route was `POST /entries/{id}/reverse` — the
withdrawal. So the ordinary case, an entry on the wrong account or for the wrong amount, had to be done as a
reversal plus a separate, unlinked entry: two transactions, with a moment in between where the books have
withdrawn the original and nothing stands in its place. A report taken concurrently would show it.

`POST /entries/{id}/correct` is the command's face. Both halves land in one transaction and the reply carries
the mirror *and* the replacement, so the caller does not have to fetch the entry it just caused.

## Decimal in, micros out

Issue 0006's friendly-input rule is a **boundary conversion and nothing else** (its own pitfall 1): a command
takes an account path and an exact decimal string in whole commodity units, the command layer converts with the
commodity's declared precision, and storage, the domain, every report and every response stay integer micros.
More decimal places than the currency has is refused rather than rounded — a reader that emits `"1.500"` for a
two-decimal commodity is misconfigured, and that is worth saying rather than normalising.

`POST /entries` already worked this way. What moved is the reconciliation surface, which still demanded
`account_id` and `amount_micros`: a statement's `opening`/`closing` and every line's `amount`, a booking leg's
`account`/`amount`, and a split part's `amount`. The line's *commodity* is what converts a part, because a part
can only be in the line's own commodity — there is one place that knows, and it is asked once.

`wire.reject_wire_numbers` gained `opening` and `closing` beside `amount`. Without that a JSON number under
`opening` would arrive as a pydantic type error about strings instead of the amount contract's own refusal
naming the rule, and the difference is whether a caller learns the class of bug or the one field.

The published `amount_contract` says both directions now, because a caller that guesses either way is wrong.

## The reply is checked against what the route publishes

`api/app.py` claimed "every route declares a response model that FastAPI validates on the way out". That was
true of the reads. A write returns a `JSONResponse` built inside the write transaction, which FastAPI does not
inspect, so the writes were documented rather than guaranteed — the fourth gap this issue was handed.

It is true now. `idempotency.run` validates the payload against the model **the route itself declares for the
status it answered with**, read off the route (`declared_model`). Passing the model in at each of the nineteen
call sites would have been a second declaration of the same fact, drifting from the `responses=` the OpenAPI
document is built from; there is one declaration and this reads it. `POST /documents` answers 200 or 201 for the
same request depending on whether the bytes were already registered, which is why the status is the key.

Two details that are the point rather than incidental:

- **It runs inside the transaction.** A reply that does not match the contract rolls the write back. Committing
  an effect whose recorded reply is wrong is the state worth making unreachable, not merely reporting.
- **Replays are validated too.** A stored reply is read back out of the request row, so a payload written under
  an older shape of a model surfaces here rather than at the caller.

`tests/test_api_capabilities.py` breaks a reply on purpose and asserts the 500 *and* that the account was not
written; and a separate test asserts every mutating route declares a model for its success status, so a new
write cannot quietly go back to being unchecked.

## The corpus is a manifest, and there is no default path

`src/haddon` named one checkout's directory layout in five places: `CHECKOUT = Path("<a fixed checkout path>")` and
`BOOK_YEAR = 2025` in `importing/importer.py`, `ENTITIES` (three named entities) beside them, `DEFAULT_ENTITIES`
in the oracle's command line, and `FROM_YEAR, TO_YEAR = 2025, 2026` in the continuity oracle. The oracle's pinned
baselines sat *inside the installed package* keyed by the same three names.

All of it is `corpus/example.json` now, read by `haddon.importing.corpus` — a generic loader that knows how
a corpus is laid out and nothing about which one. `HADDON_CORPUS` names the manifest.

**There is deliberately no fallback path in the package.** A default of "this repository's `corpus/`" would put
the checkout back in the product by a longer route, and a default of "whatever manifest is in the working
directory" would let a run pick up a corpus nobody named. Unset, the loader raises and says which variable to
set. The `bin/` wrappers and `tests/conftest.py` set it to this repository's copy, which is where that knowledge
belongs.

The exporter's `PROTECTED_ROOTS` went the same way: the corpus checkout's parent directory was a literal in a
generic module, so a generic exporter named one deployment's directory layout. The corpus declares its own `read_only_roots` and the
vendored binary stays unconditional. The fallback is chosen so a missing or broken manifest makes the guard
*smaller*, never absent — a guard that disappears exactly when configuration is wrong is worse than no guard,
and `test_the_verifier_stays_protected_when_no_corpus_is_configured` is what holds that.

**The guard against it coming back reads the syntax tree.** `tests/test_generic_bootstrap.py` fails on a
checkout path, an entity name or an inherited year *in code*, and leaves docstrings and comments alone. The
distinction is not pedantry: prose explaining why the 2025/2026 continuity case is the shape a double count
hides in is documentation this repository wants, and a guard that could not tell it from `year: int = 2025`
would be answered by deleting the explanation. The year check is narrower still — module-level constants and
argument defaults only — because "any four-digit integer" catches `max_length=2048` and teaches the next person
to work around the guard.

## Two command modules, because the dependency rule is real

The reconciliation commands are `src/haddon/reconciliation_commands.py` rather than more functions in
`commands.py`, because the journal core may not import an extension (`architecture.md`) and they read
`repository/statements.py` and `repository/lines.py`. The import goes the allowed way round: the extension's
module takes the core's `LegSpec`, `resolve_account` and `resolve_legs` rather than repeating them, which is the
one thing this codebase keeps getting wrong when a concept is implemented twice.

`tests/test_core_boundaries.py` gained a top-level area map so the new module is *classified* rather than
defaulting to core — the rule checks it, instead of it being outside the rule.

## Where 0006's own text is out of date, and what was left

- **The issue lists `classify_line`, `replace_classification` and `review_period`.** Those names predate issue
  0004, which replaced the classification model with `book_line` / `link_line` / `ignore_line` /
  `reexplain_line` and the `GET /reconciliation` read. They are the same concepts under better names, and
  publishing 0006's list beside them would have been exactly the defect the plan names: one idea implemented
  twice. 0004's vocabulary is what is published.
- **`/close-checks` did not report the lock version.** `close_period` requires `expected_version`, and the route
  that answers "would this close?" did not carry it — an agent had to go to `/v1/entities` and find its own row
  for the one number performing the close requires. It is on the response now. Nothing about the close changed;
  the answer simply carries what acting on it needs.
- **`reports/composition.py` still counts import provenance** from `journal_mappings`. The coupling allowlist
  named "0006/0004"; it is re-pointed rather than fixed, because splitting the provenance half out changes the
  published `/reports/composition` contract and the page that reads it — a reviewer-surface change. 0006 moved
  the importer's *configuration* out of the package, which is a different question.
- **The review and local-tax writes are named but their handlers are still repository functions.**
  `register_document`, `file_document`, `await_document`, `withdraw_expectation`, `record_unrecovered_tax` and
  `resolve_unrecovered_tax` are discoverable commands with `operation_id`s, and they are excluded from the
  "every command is a function in a command layer" test by name. Building a command layer for surfaces issues
  0007 and 0008 are about to rework would be a second half-built one.

## The path a document sits at is not evidence, and is not published

Issue 0007's fourth gap: "evidence shows a hash and a server-local blob path rather than viewable/downloadable
content".  Both halves of that are wrong in the same sentence and for different reasons.

A **SHA-256** verifies bytes you already have.  Nobody reviewing a month has them.  A **`blob_path`** names a
file on a machine the reader has no account on, so it cannot be opened either — and unlike the hash it is also a
disclosure: a page anybody with the reviewer password can load was telling them where the service keeps files.

So `blob_path` left every response (`Document.as_view()` beside the audit-only `as_json()`, `DocumentView`
beside the inbound `StatementDocument`) and `GET /documents/{id}/content` took its place.  What a reader gets
instead is `storage` — `stored` or `external` — and, when the bytes are here, `links.content`.

**The link is the affordance.**  The interface uses `links.content` as given rather than assembling the URL
from a document id, which is what makes a document held elsewhere render correctly without the component
knowing the rule: the service omits the link, so there is no button and no refusal to explain afterwards.

## Serving a document does not weaken "ingestion does not read the bytes"

The existing decision stands unchanged.  The relationship between it and the new read is one-directional:
registration records a *claim* about bytes, and the download is where the claim is **checked**, at the moment
somebody relies on it.

Which means the recorded `media_type` and `size_bytes` are never trusted over what is actually on disk, and a
file that no longer hashes to its registered `sha256` is refused rather than served with a warning.  Serving it
with a warning would leave the reviewer in exactly the position this read exists to end — holding something
that looks like evidence and is not.  Verification needs the last byte, so the file is read in full and there
is a size ceiling above which it is refused rather than streamed unverified.

## "Authorized evidence" means containment, because there is no role model to mean anything else

0007 asks for an endpoint that serves content "after entity and actor authorization".  Haddon records authority
and does not enforce it (§ "Authorisation"); there is no role to filter by, and inventing one to gate a single
read would be a second authorization story in a system that has deliberately declined to have a first.  So the
authorization is what every entity-scoped route already has: a credential reaches this only for an entity it is
scoped to, and an id from another entity's file is a plain 404.

What is genuinely added is **containment**, which is a different property and the one that matters for a file
read.  `blob_path` is a string a caller wrote into a registration.  Honouring it as a path, unconfined, would
make `POST /documents` a file-read primitive against the whole host: register `/etc/passwd` with its real hash,
then read it back through a ledger.  Bytes come only from under a configured `blob_root`, the path is
**resolved** before the containment check so a symlink out of the root is refused, and an installation that
configures no root serves no content at all.  No refusal names a path, including — especially — the case where
the file is missing and naming it would be the most helpful thing to say.

Inline rendering is an allowlist for the same shape of reason: the media type is caller-supplied too, so
"render it as what it claims to be" is a stored-XSS surface on a same-origin page.  `image/svg+xml` is
deliberately absent from it — an SVG is the one image type that is also a script host.

## An entry's state is derived, and `corrected` is not `reversed`

`GET /entries/{id}` answered the legs and the version.  That is enough to display an entry and not enough to
review one: the questions a reviewer arrives with are "what is this now?", "what superseded it?" and "what did
it supersede?".

`state` is computed from the correction rows that name the entry, never stored — a stored copy would be a
second thing to keep true.  The two failure states stay apart on purpose.  Both leave the original standing in
the books with an exact mirror beside it; only one of them also posts what the entry *should* have said.  A
reviewer told "reversed" about a corrected entry goes looking for a restatement that is already there, and one
told "corrected" about a plain reversal goes looking for one that is never coming.

`corrections` matches the entry against all three columns and reports which end it is.  The backward half is
the one that did not exist: a reader who follows a register to a mirror sees a lone negative entry, and the
honest reading of that, with no way back to what it undid, is that the books are wrong.

## An entry booked off a statement line could not see its own creation

`book_line` records `line.booked` with subject kind `statement_line` and the **line's** id.  `GET
/entries/{id}/events` gathered the entry's id and its corrections, so an entry that arrived through a statement
— which is nearly every entry in a real set of books — had an empty history, and an entry page built on that
read said "nothing has been recorded against this yet" about a booking with a document behind it.

The event read now also gathers the statement lines whose **live** explanation allocates to this entry's
postings.  A line re-explained onto a different account has a history with this entry and no longer has
anything to say about it, which is what `live_explanation_clause` excludes.

The join is written in `api/routes/entries.py` rather than in a core repository module, and that placement is
the decision: `statement_lines`, `explanations` and `allocations` belong to the reconciliation extension, the
core may not depend on it (`test_core_boundaries`), and the API layer is the composition root where the two are
allowed to meet.

The browser harness is what found this.  Neither `tsc` nor the Python suite could: the endpoint answered
correctly for the ids it was given, the page rendered what it was sent, and the defect was entirely in which
ids were asked for.

## An affordance is gated on the command's published name, not on the end of a path

Issue 0006 made one string serve as the OpenAPI `operationId`, the key in `/capabilities.commands` and the
handler in `haddon.commands`.  The interface now gates on that string: `useCommand(entity, "close_period")`.

The previous spelling, `data.writes.some(w => w.endsWith("/close"))`, was a guess that happened to be right.
It also matches a hypothetical `/entries/{id}/close`, and the same shape of guess in
`useReconciliationEnabled` was wrong for a release — an exact `includes("/reconciliation")` was false
everywhere, so the month review told every installation that reconciliation was switched off, and the page
compiled and every endpoint answered while the review was simply not there.

## The `Promise.all` batch 0007 names no longer exists

0007's sixth gap describes a "Post them" button dispatching independent statement requests with `Promise.all`,
so that the wording implies an atomic batch while partial success is possible.  That button and that call were
deleted by issue 0003, which made booking post directly rather than draft: there is no `Promise.all` anywhere
in `web/src`.

Adding a server-side atomic batch command for a caller that does not exist would be building the general
framework this roadmap keeps deleting.  What is kept instead is the property the gap was protecting: **no
affordance in the reviewer interface dispatches more than one write per press.**  A screen that needs to act
on several items reports per-item outcomes and says so in its label.

## Nothing a command can reach is a place commands cannot leave

The 0006 review held Phase 4's exit gate open on one counterexample, and it was right to. The gate says an
agent can maintain a normal period **using the published commands alone, including recovery from validation
errors**. That is a property of the surface *as a graph*, not of any command in it: a surface on which some
single successful call strands the entity fails the gate however well each command behaves alone.

`archive_account` was such a call. It accepted an account that still carried a live balance, a posted entry and
a live statement with an unbooked line. Afterwards:

| what an agent would try next | answer |
|---|---|
| `POST /entries/{id}/reverse` | `422 account_archived` |
| `POST /entries/{id}/correct` | `422 account_archived` |
| `POST /lines/{id}/book` | `422 account_archived` |
| `POST /close` | `409 close_not_eligible` — `lines_explained`, `nothing_partial`, `statement_coverage` |
| `POST /accounts/{id}/archive` again | `409 immutable` — and there was no other account write |

Permanently. One command, one 201, one audit action, and the only remaining exit was to open the database file.

The 409 is the least interesting of these. A close *can* be forced with an `override` naming each blocking
check — but an override is meant to record that somebody looked at an outstanding item and decided to close
anyway, and here it would record a waiver for something the agent was *unable* to satisfy. That is not
recovery; it is writing a false sentence into the close record to get past a gate. The reversal has no
equivalent escape at all: reversing is the only way a posted entry is ever disagreed with, and archiving
removed it for good.

### Why the inverse, and not the other two fixes

**Refusing to archive a load-bearing account** was the obvious one, and it does not establish the invariant. It
narrows the entry to the trap without removing the trap: a zero-balance account with every line explained still
has posted entries, and those entries could still never be reversed or corrected once it was archived. Any
predicate strong enough to close that hole would have to refuse archiving an account that has *ever* been
posted to, which is very nearly every account worth retiring.

**Teaching each refusal to allow what is not new work** is the accountant's reading, and it is more nearly
right than it sounds — a mirror of an existing posted entry introduces no account, commodity or balance the
account did not already have, and a statement line dated before the archive *is* that account's movement rather
than a new use of it. It was rejected on where the rule would have to live: `seal`, `book_line`,
`reexplain_line` and the reconciliation close checks each decide independently, and the fourth one missed is
the next dead end, found the same way this one was — by somebody driving the app a year later.

**Making archiving reversible** is one command and one rule, and the rule is already in this codebase.
Archiving is a *decision about* the books, not a fact *in* them, and every other decision here can be taken
back by another one: `reopen_period` moves a closed period's lock backwards, a correction restates a posted
entry, `replace_statement` withdraws a statement loaded wrongly. Closing a period is a far stronger statement
than retiring an account, and closing is reversible. Archiving being one-way while closing was not is an
inconsistency in the model rather than a principle anybody chose.

So: `POST /accounts/{id}/restore` — `restore_account` — a reason, an `expected_version`, one audit action
(`account.restored`), and nothing about the history moves in either direction. Recovery reads
**restore → finish the work → archive again**, and the audit trail reads honestly: *"archived: the bank account
was closed"*, *"restored: March was not finished when the account was retired"*, *"archived: now really
closed"*. What archiving **means** is untouched — a new leg on an archived account is still refused, which is
the whole content of the command.

No migration was needed, which is itself the argument that the irreversibility was accidental rather than
designed: `accounts_update` has only ever refused an identity change and a missing version bump, and has never
cared which way `archived` moves. The storage model always allowed this. Only the command surface did not.

### The invariant, and how it is held

> **No published command may put an entity in a state that no sequence of published commands can leave.**

`tests/test_command_surface_recoverable.py` states it over the **derived** surface rather than over this one
bug: it walks every published write and requires each to be either additive — it adds a row and changes no flag
another command reads as a gate — or paired with a published inverse. A new command that is neither fails
there, before anybody drives it. The end-to-end repro sits beside it, and reverting `restore_account` fails
both.

## The amount contract advertised a strictness the parser did not have

`/capabilities.amount_contract.refused` said "JSON numbers of any kind, floats, leading zeros, `'-0'` and
anything outside signed 64 bits". Three of those five were true. `"0012.00"`, `"-0.00"`, `"12.34\n"` and
Arabic-Indic `"١٢.٣٤"` all posted **201**, the last of them arriving in the books as 12.34 with no error and no
rounding — a different number than the caller wrote.

One line, three causes, all in `Micros.from_decimal`'s shape check `^-?\d+(\.\d+)?$`:

* `$` in Python matches before a trailing newline as well as at the end, so `"12.34\n"` was accepted while
  `"12.34 "` was refused — the inconsistency that makes it a bug rather than a policy;
* `\d` is every Unicode decimal digit, not `[0-9]`, and `Decimal` accepts those digits too;
* `\d+` permits leading zeros, which the published sentence named specifically.

**The parser moved, not the contract.** The contract is what an exact ledger should promise: one spelling per
amount, so there is nothing to normalise and nothing to guess. The regex is now
`\A-?(0|[1-9][0-9]*)(\.[0-9]+)?\Z` with negative zero refused in every spelling, which is exactly what
`Micros.parse` has always demanded of the outbound form. The arithmetic is untouched — this is the accept/reject
predicate ahead of `Decimal`, and every internal caller passes a `Decimal` or an already-canonical string.
`Micros.parse` had the same `$` hole (`"12000000\n"` parsed) and is fixed with it.

The published sentence is now longer and names each clause, and the regression test reads it: for each clause
it asserts the clause is still published *and* that an amount of that kind is refused over HTTP. Weakening the
sentence instead of the parser fails it too.

## A corpus manifest that was wrong said so in Python's words, not Haddon's

Issue 0006 made `HADDON_CORPUS` the single source with no fallback, on the reasoning that a ledger guessing
whose books it is verifying is worse than one that refuses to run. That reasoning puts the weight on the
refusal being *useful*, and the refusals were not:

| manifest | before |
|---|---|
| malformed JSON | `JSONDecodeError: Expecting property name … (char 17)` |
| an entity that is a string | `TypeError: … argument after ** must be a mapping, not str` |
| `"default_year": "two thousand"` | `ValueError: invalid literal for int() with base 10` |

None named the file. Two named no field. Every refusal in `read_corpus` is now a `validation` error naming the
manifest and the field, entity entries included by index.

Separately, and worse because it was silent: a manifest declaring the same entity `key` twice resolved the key
to whichever block came **last**, because the loop was `entities[entity.key] = entity`. The likely way to write
that manifest is to copy a block while adding a company — so the failure mode was importing one entity's
journal under another's id and legal name, with nothing said, in a tool whose whole job is to be exact about
whose books these are. A duplicate key is now refused by name, quoting both ids.

Finally, `bin/haddon-import` and `bin/haddon-oracle` each carried `${HADDON_CORPUS:-…/corpus/example.json}`,
which put the fallback back by a longer route — through the one command anybody actually types. Both are gone;
the repository names its own manifest once, in the `Makefile` (`export HADDON_CORPUS ?=`), which is not a
wrapper and is where a repository is entitled to say which corpus its own gate runs against. `bin/haddon-export`
keeps its default and now says why: there the corpus is an input to `exporter.protected_roots`, never a choice
of whose books to read, and the fallback can only make that guard wider.

---

# Issue 0008 section B — one arrangement for the statements

The reviewer interface rebuilt the shape of a statement in TypeScript: section order, the sign convention for
credit-natured accounts, the union of the comparative column, subtotals, the synthetic result row inside equity
and the balance check — a second implementation beside `haddon.statements.shape`, which the PDF is drawn from.
Two implementations of one arrangement is one place for an emailed document and the screen it was read from to
disagree, and the disagreement is invisible because each is internally consistent.

## What was built

`GET /v1/entities/{e}/reports/statements` publishes the arrangement as JSON: per commodity, the shaped income
statement and balance sheet, plus the period's name and heading, the comparative's name, the sign footnote and
`other_commodities`.  The result label is **per statement, not per document**: a document holding several
commodities holds several bottom lines, and one of them being a profit says nothing about the others. It takes the same inputs as `/reports/statements.pdf` (`from`, `to`,
`comparative`, `commodity`) and is built by the same two functions, `_statement_inputs` and `_statement_views`,
so the JSON and the PDF cannot be drawn from different numbers. `shape.difference` and the per-column `checks`
are first-class output rather than a subtraction recomputed by each reader.

**Why a new endpoint and not fields on the existing reports.** A statement's arrangement depends on a period
*and its comparative*, and the raw `/reports/income-statement` / `/reports/balance-sheet` reads answer for one
period each. The interface used to fetch two periods and merge them; moving the merge into the service is what
removes the second implementation. The raw reads stay for agents that want the unshaped lines.

## What was deleted

`web/src/lib/statement.ts` kept its presentation types and grew a single `toSection` that renames served fields
and computes nothing; `buildSection`, `presentedSign`, `commoditiesOf` and `ofCommodity` are gone.
`Reports.tsx` draws the served sections, subtotals, result label and balance check directly. The now-unused
`useIncomeStatement`/`useBalanceSheet` hooks and the matching client methods went with them. `render.py`'s
inline result label and footnote moved to `shape.income_result_label` and `shape.sign_footnote`, so the PDF and
the JSON say the same sentence; a dead `_journal_list` was removed in passing.  `period.ts` lost
`columnName`, `shortPeriodName`, `periodHeading`, `comparativePeriod` and `previousPeriod`: they are the
TypeScript half of the wording and comparative-choice the service now decides, and nothing reads them.

## Two ways it could still have said two things

Review found the arrangement shared but two of its *words* still computed twice, which is the same defect in
smaller print:

* **The bottom line's name.** `result_label` was one field on the document, read off the leading commodity, and
  the interface printed it under every commodity's figure — so a EUR loss beside a USD profit was headed
  "Profit for the period". The label now belongs to the income statement it names
  (`views.shaped_income` → `shape.income_result_label`), which is the only scope at which it is true.
* **What the document leaves out.** The PDF built `other_commodities` from the current column's lines while the
  JSON built it from both columns. A commodity held at the comparative date and settled inside the period is
  gone from the sheet drawn at the period's end, so the PDF omitted a commodity the screen named — while
  printing the very column that held it. Both faces now call `_other_commodities`.

## Two tests that had to get sharper

`test_reconciliation_optional` excluded reconciliation routes by `p.endswith("/statements")` or `"/statements/"
in p`, which also caught the core's new `/reports/statements`; the match is now a whole path segment
(`p == x or p.startswith(f"{x}/")`). `test_api_capabilities` pins the read surface by name and gained
`/reports/statements`.

## What this deliberately did not change

Period *navigation* stays in the interface (`web/src/lib/period.ts`): which period a reader asked for and which
one to step to are interaction, and issue 0002 deferred fiscal-period presets on exactly that reasoning. The
*wording* of the period shown — its name and heading, including "to date" for one still running — now comes
from the service, because a heading is part of the arrangement the PDF and the screen must share.

# Three things the statement stopped saying, and one of them is a column

Issue 0008 section B made the PDF and `/reports/statements` one arrangement with two faces. This is the first
change where the two faces deliberately say *different* things, and the first where a column comes off the page.

## The event sequence is not provenance a reader can use

The footer read `Drawn 16 September 2026, 07:40 UTC · records to event 64`. `snapshot_seq` is this service's
position in its own event log: it is exactly the right thing to hand a program, which can quote it back on the
next request and be told whether the books have moved. It is the wrong thing to print on a document that gets
emailed, because its reader has nothing to look it up against and no way to act on the answer.

So it came off the page and stayed on both JSON faces, and `DocumentContext` no longer carries it at all —
a field the renderer does not draw is a field the next person to read the class will try to find on the page.
The *dating* is untouched and is the part that mattered: a statement of an open book is a snapshot, two copies
drawn an hour apart are two documents, and `Drawn …` is what tells them apart.

## The sign convention is explained to the caller, not to the accountant

`sign_footnote` printed one paragraph on both faces: the sign convention, then — when there was one — the
sentence naming the commodities the document does not show. They are not the same kind of sentence.

The commodity sentence is a **fact about this document**: it shows one commodity, amounts in another cannot be
added to these, and a page that did not say so would read as the whole of the books while being short of money.
Both faces must carry it, and it must be built once so an emailed document cannot omit a commodity the screen
named (which is the bug the `_other_commodities` union fixed).

The sign sentence is an **explanation of a convention**, and the person holding a PDF of an income statement
already knows that revenue is written without a minus in front of it. It was printed on every document that
left the service. It stays on `/reports/statements`, whose reader is a program or an interface building its own
page and can be assumed to know nothing.

`sign_footnote` therefore splits into `SIGN_NOTE` and `omitted_commodities_note`, and the PDF draws only the
second — and draws no footnote at all when there is nothing left out, rather than an empty rule of grey text.
`web/src/routes/Reports.tsx` keeps its own copy of the sign wording, which is the screen's copy for the screen's
reader and was never taken from this function.

## A period that never existed is not a column of zeros

A first set of accounts was drawn with a comparative column of nothing: `ACCOUNT 2025 2024`, every figure in the
2024 column `0.00`, on a book whose earliest posting is 1 January 2025. The column is derived from the calendar
(`haddon.statements.period`), so it can always be *named* — a book's first year has a "prior year" in the same
sense that the year before it has a number — and naming it was being taken for having one.

The fix is one line in `_statement_inputs`: a `prior` the books cannot supply becomes `None`, which is already
the single signal the whole shape layer reads for this (`show_comparative`, `ColumnLabels.comparative`, the
balance check's "at both dates", the JSON's `comparative`). Dropping it *there* — before either face is built —
is what keeps the screen, the JSON and the emailed PDF the same statement.

### What "cannot supply" means, and what it must not mean

The tempting test is "every figure in the prior column is zero". It is wrong, and it is wrong in a way that
would have looked right on the one book that prompted this change.

| the books | what the reader should see |
|---|---|
| nothing was ever recorded before this period | one column — there is nothing to compare against |
| a period before this one in which nothing moved | two columns, the second of them zeros |

Those two produce identical figures and are not the same fact. The second is an answer — the books were open,
the quarter was quiet — and it is the answer a reader of a quiet quarter has come for. Collapsing it would
leave them unable to tell a quiet period from a period that does not exist, which is the same failure the
`composition` report exists to prevent ("is this statement thin because the period was quiet, or because only
part of the book is loaded?").

So `_anything_recorded_through` asks whether a **posted entry** exists at or before the comparative's end date,
and never looks at an amount. `status == 'posted'` matches `_period_bounds`: a draft is not yet a record. The
bound is `<= prior.end` rather than "inside the prior period" because the comparative balance sheet is drawn
*at* its date and is cumulative — a position taken on before the comparative quarter still stands in its
column, and `tests/test_statements_pdf.py` holds that boundary too.

### Why the tests read PDF bytes

`show_comparative` existed before this change and worked. A test of the shaping alone would have passed against
a document that still drew the empty column, because nothing in the shaping was broken — the caller was handing
it a prior period. Every assertion in the "one column, or two" block reads text extracted from the rendered PDF:
the column heads, the figures on a row, and the balance check's wording.

## What this deliberately did not change

The base commodity still leads the document even when it holds no line of its own (`_commodities`), which is the
accepted behaviour from section B: a statement that silently led with whichever commodity happened to be busiest
would be a different document each period. The comparative is still a **union** of both columns rather than a
lookup, and a mid-period balance sheet still carries the result row inside equity — a single-column statement
changes which columns are drawn and nothing about how either is built.

# Batch entry input — N entries as one refusable unit

`POST /v1/entities/{entity_id}/batches` posts many entries in one transaction, checked against the caller's own
declaration of what the batch contains, and refused whole when it does not match.

The gap it closes is not expressiveness — every entry a batch can write, `POST /entries` could already write.
It is that **a set had no representation.** An agent handed a converted year loads well over a thousand entries
through a loop of single posts, and afterwards the only thing anybody can say about the result is that each call returned 201.
A dropped entry, a retried one that landed twice, a connection lost two thirds of the way through: each leaves
an append-only, lockable ledger that is internally consistent and simply wrong, with nothing in it to disagree.
The books are then closed over it. That is the failure this exists to make impossible, and a per-call retry
policy does not address it, because the thing that went wrong is a property of the set.

## The invariant is the caller's assertion, not something the ledger derives

A batch carries an **asserted count** and **asserted per-commodity totals** — debits and credits, in micros, as
non-negative magnitudes — and the ledger compares them against what it actually posted before anything commits.

This is the same shape as the statement input, and for the same reason. Chunk F1's rule is that the agent
converts and the ledger validates: Haddon holds no reader, no format and no per-source code, so it never sees
the journal, spreadsheet or export the entries came from and **cannot derive what they should have added up
to**. Anything it computed from the submission alone would be a restatement of the submission — it would agree
with a batch that lost a third of its entries in conversion, because the entries it was given are all it knows
about. The count and the totals have to come from the other side of the conversion, where the source is, which
means they are a claim the caller makes and this ledger refuses.

A statement asserts that its lines sum to closing minus opening. A batch asserts how many entries it carries and
what each commodity comes to. Both are one number the caller took from a place the ledger cannot see, and both
turn "did all of it arrive?" into an answer rather than a hope.

**Both sides of each commodity are asked for even though a balanced batch makes them equal.** Every entry nets
to zero per commodity, so debits and credits are necessarily the same figure once the batch is built — which is
exactly what makes asking for both worth doing: they are computed independently by the caller from its own
source, so a figure transposed on one side of the conversion disagrees with the other rather than cancelling
out. The ledger's own arithmetic has nothing to say about that case, because the entries balance.

Every commodity of both sets is compared, not just the declared ones. A commodity the batch posted and the
assertion never mentioned is a mismatch as loud as a wrong figure — a conversion that quietly dropped a currency
is precisely what an asserted total exists to catch, and checking only what was declared would not see it.

## What a refusal is

One transaction. A broken assertion, an unknown account in the nine-hundredth entry, a date inside a closed
period — any of them refuses the **whole** batch, and the entries before it do not survive. There is no
half-loaded batch, and `tests/test_batch_entries.py` compares every refusal against a full table-count snapshot
taken before it, which is the house pattern chunk F1 established.

The refusals are split by what they are about rather than into one code:

| | |
|---|---|
| `422 validation`, `batch_count_does_not_match` | the caller and the ledger disagree about the size of the submission itself. Checked before anything is posted: it says nothing about the books and everything about the request that arrived |
| `422 unbalanced`, `batch_totals_do_not_match` | what was posted does not add up to what was declared. Names **every** disagreeing commodity at once, with the asserted and the actual figure on each side |
| `422 validation`, `batch_too_large` | over `limits.max_batch_entries` (1,000), refused by the number it broke rather than attempted |
| everything else | whatever the single-entry path would have said, because the single-entry path said it |

The last row is the one that matters most. Every entry is written by `commands.post_entry`; there is still one
posting path, and a batch buys no exemption from the balance rule, the account, commodity and precision rules or
the period lock. A closed month refuses a batch exactly as it refuses one entry, with the same `reopen` remedy.

**And every one of those refusals says which entry it is about.** `post_entry`'s refusals are phrased for
somebody who sent one entry — the path is `legs[0].account`, or `dt`, or `id` — which is exact for one entry and
no use at all for nine hundred. A conversion told "no account 'expenses:nope' in this entity" and nothing else
has to bisect its own submission to find where, which is the work a batch exists to remove. So the refusal keeps
its code, its message and its details, and every detail gains the entry's place in the batch: `batch_index`, the
entry's `dt` and `narration`, its `entry_id` when the caller supplied one, and a `path` rewritten to
`entries[900].legs[0].account`. The message is prefixed the same way. Nothing is dropped, so a caller matching
on `account_unknown` or on `details[0].remedy` still matches; it is only told where to look. Loading the
corpus's ExampleBV and ExampleInc 2025 books through this surface is what showed the gap — see below.

## Why there is a maximum, and why it is published

A batch is applied in a single `BEGIN IMMEDIATE` transaction, so its size is how long every other writer waits.
One thousand is the cap, refused with a 422 naming the number, and published as `limits.max_batch_entries` in
`/capabilities` — the same arrangement as `max_limit` and `max_entry_legs`, where the advertised number and the
enforced number are one number. A caller with more than that splits the work, and each batch is refused or
applied whole on its own; the alternative — an unbounded transaction — is a timeout, and a timeout says nothing
about what was loaded, which is the failure this whole surface exists to remove.

## One operations row

The batch is recorded as one row in `operations` (`kind: "batch"`), moved `queued → running → succeeded` in the
same transaction: its `intent` is the assertion, its `counts` are the entries, postings and commodities it
wrote, and its `result` names the entries. That is the durable record of the *set*, and it is what the per-entry
audit actions cannot be — each entry still keeps its own `entry.posted` action, because each is still a booking
somebody is answerable for, and one `batch.posted` action names the operation.

`requests.operation_id` stays null. `execute_once` records the request row after the work returns and carries
nothing back from it, so linking the two is a change to every write path in the service; this chunk did not make
it, and the operation id is in the reply and in the batch's audit action instead.

A batch is **additive** in the sense `tests/test_command_surface_recoverable.py` means: what it writes is a set
of ordinary posted entries, each reversible and correctable on its own. The batch is a unit of submission, not a
unit the books afterwards hold as one thing, and there is deliberately no "unpost the batch" command — that
would be a second correction mechanism beside the one every posted entry already has.

## What this deliberately does not include

**No `journal_mappings` row.** That table is the hledger importer's full-fidelity canonical record — the
oracle's `mapping_sha256` check reads it, and a row in it claims a specific source revision, journal path and
source index. A generic batch has no canonical source transaction to put there, and writing a fabricated one
would corrupt the one link the oracle uses to prove the imported books still say what the journals say.

**No reader, no parser, no per-source, per-bank or per-company code.** The submission is already-normalised
entries in the vocabulary `POST /entries` publishes. This is chunk F1's rule applied to the second input
surface, and it is why a batch is 200 lines rather than a subsystem: the ledger gained a unit of submission, not
a knowledge of where entries come from.

**`bin/haddon-import` and `haddon.importing` are untouched.** The hledger importer is a different thing — a
full-fidelity load of a journal the oracle verifies against, mappings and all — and it is not deprecated,
replaced or narrowed by a generic batch.

**No caller-supplied actor.** The actor is the credential's, as on every other write. A batch is a larger write,
not a differently-authorised one.

## What loading the corpus books through it showed

Before an agent used this, both of the corpus's 2025 books were loaded twice and the results compared:
once through `bin/haddon-import` (the trusted path) and once by converting the *same* hledger journal into a
batch payload and posting it, which is the work the agent would actually have to do. ExampleBV is a
small book — tens of entries over a few dozen accounts; ExampleInc is a large one, well over the batch cap.

**The two books agree.** Entry count, posting count, chart, commodities; the trial balance per account per
commodity to the micro; and every entry's `dt`, `narration`, `code`, `comment`, `tags` and per-posting `memo`.
The published reports — trial balance, balance sheet, income statement — are identical for both entities, and
re-exporting each book to hledger journals produces transactions that are identical transaction for
transaction, posting for posting, comments and tags included.

Four differences remain, and all four are consequences of what this chunk deliberately does not do:

* **No `journal_mappings`**, so an exported batch-loaded book is one `<year>-haddon.journal` under a directory
  named for the entity id, rather than the source-named journals under `ExampleBV/` the import round-trips
  to. The *content* is the same; the grouping and the per-transaction source status are what the mappings hold.
* **One `operations` row** where the import writes none, and one audit action per entry plus one `batch.posted`
  where the import writes a single `import.completed`. That is the command surface behaving as it does
  everywhere else.
* **`origin` cannot be `opening`.** It is a reserved origin (`entries.RESERVED_ORIGINS`), assigned by the
  service and refused from a caller, so the opening entry of a converted year — which the importer marks
  `opening` from the file it came from — lands as an ordinary entry. Nothing in either book's numbers moves,
  because both openings touch only balance-sheet accounts and `movements()` excludes openings from the income
  statement alone. A conversion whose opening touched a P&L account would move a number, and the export's
  `opening` group would be wrong in any case. Reserving the origin is the right default — an opening the
  importer marks has had its restatement verified, and one a caller marks has not — so this is left as it is
  and written down rather than quietly worked around.
* **The chart is the caller's problem.** A batch posts to accounts and never creates one, so each book needed
  one `POST /accounts` call per declared account before its entries — one of which is always a 409, because
  `initialize_entity` writes the retained-earnings account itself. That is over a hundred calls of boilerplate
  for two books, and it is the one part of a conversion this surface does not make answerable: a
  chart half-created by a loop of single posts has exactly the property the batch exists to remove.

**The cap is not the binding constraint.** ExampleInc's year cannot be one batch, so it went in as two halves, each
taking roughly as long as half the importer's run over the whole year. What the split costs is not
time; it is that the assertion is now per-batch. Each half asserted its own count and totals and each was
checked, but *nothing asserts the year*: a conversion that dropped an entry while slicing gets two batches that
both agree with themselves. And the halves are independent — the second one referencing an account that does not
exist was refused whole and left nothing, exactly as documented, while the first half's entries stayed committed
and there is deliberately no command to take them back. Splitting therefore reintroduces, at the seam between
batches, a smaller version of the failure the batch removes inside one.

# The export over the wire — `POST /exports` and the files it published

The deterministic export (chunk C2a) was complete and reachable from one place: a shell on the machine.
`bin/haddon-export` rendered the book, verified it with the pinned binary and recorded an immutable `exports`
row; `GET /exports/{id}` read that row back and described each file by name, digest and length — and there was
no way to ask for an export and no way to fetch a byte of one. So the books' own journal, which is the only
form of them somebody else's tools can read, was the one thing a credential could not obtain.

This adds the transport and nothing else. **No rendering, verification, ordering or storage rule is
reimplemented or relaxed**: the route calls `exporting.exporter.export_book`, the same function the CLI calls,
so two exports of one state are still byte-identical, a strict-check failure still publishes nothing and
records nothing, and re-exporting an unchanged range still reuses its snapshot rather than minting a second.

## Where the files go is the deployment's statement, not the caller's

`export_root` is configuration, like `blob_root` and `entity_root`, and unset means this installation does not
export over the API (`422 export_root_unset`) rather than the service choosing a directory for somebody. An
out-root in the request body would have made a published command a write-anywhere primitive on a box where the
one thing an export is trusted to do is write files.

Each entity gets its own subdirectory, `export_root/<entity_id>/`, and that is the containment. The directory
*inside* it is named by `journal_mappings.journal_path` — book data, written by an import — so a shared root
would let one book's rows name the directory another book's export is published into. The id is a slug, so it
is one path segment and cannot climb out.

## The reply is written in the export's own transaction

`export_book` owns a write transaction and the route does not, so `execution.execute_once` — where every other
write gets its idempotency — could not be used: running it around the export would mean a second `BEGIN
IMMEDIATE` against a file the exporter is already writing.

The alternative shape, "export, then record the reply in a second transaction", has a window in it: a crash
between the two leaves an export that is durable and a reply that is not, and the retry re-renders, re-verifies
and re-publishes. So `export_book` gained one hook — `also_record(ctx, result)`, called with the finished
result inside the transaction that writes the operation, snapshot, `exports` row and event, before it commits.
The route uses it to write its `requests` row and to pre-allocate the request id the export's events link to,
which is exactly what `execute_once` arranges for everything else. Either both rows commit or neither does.

The replay *check* is a read of the entity's request log before the export starts (the same thing
`POST /v1/entities` does for a creation), and it is made under a **per-entity export lock**
(`Registry.exporting`). Two exports of one book publish into one exclusively-managed directory; the loser
re-hashes the winner's bytes and fails `ArtifactMismatch` having published nothing anybody asked for. Two books
have no reason to wait for each other, so the lock is per entity.

## Serving the bytes: the recorded digest is the claim

`GET /exports/{id}/artifacts/{name}` is the download chunk D1 declined to open ("handing out file bytes is an
authenticated download surface with its own rules"). The rules are `api/evidence.py`'s, applied to a file this
service wrote itself, and they live in `api/exported.py`:

- the **name is a key into the export's own immutable artifact list**, so a name that export did not record is
  a 404 before any path is built out of it;
- the **directory comes from the snapshot's manifest** (`ledger_dir`), validated segment by segment because it
  is derived from book data, and is resolved under this entity's export root — a path that resolves outside it
  is refused, not served;
- the file is **re-hashed against the `sha256` on the `exports` row** before a byte is returned. That digest is
  the claim that these exact bytes passed `hledger check --strict` under the pinned binary; a published journal
  somebody has since edited is refused rather than served with a warning;
- **no refusal names a path on this machine**, including — especially — the case where the file is simply gone.

Nothing new is stored: the row says which files, the manifest says which directory, the configuration says
which root. An export published elsewhere by the CLI is therefore a real row whose bytes this service does not
serve, which is the honest answer rather than a second storage model invented to make it one.

## Things deliberately not done

- **No zip, and no "download the whole export" address.** A zip's bytes have no recorded digest, so offering
  one would mean inventing an integrity claim beside the one the `exports` row already makes. The files are
  two to five per export and each one carries its own.
- **No `run_export` function in `commands.py`.** The published command name is the handler's name, and the
  handler is `export_book`. A one-line wrapper would be a second name for one thing.
- **No `reason` on the submission.** Every write that decides something takes one; an export decides nothing,
  which is the same reason chunk C2a records no proposal. What is recorded is the range, the actor and the
  state that was rendered.
- **No new error code.** A file that cannot be served answers `evidence_unavailable` with its own
  `details[0].reason`, which is the same answer, and the same shape of answer, a document's bytes give.
