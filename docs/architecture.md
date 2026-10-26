# Architecture

Haddon is a small journal core with optional extensions around it, served over HTTP by one process per
deployment. This page is about the boundaries between those parts, how they are enforced, and where things
live in the code.
For the accounting model itself, see [concepts.md](concepts.md); for how the design got here, see the
[design log](design/decisions.md).

## Three kinds of fact

1. **Accounting facts**: what the books say happened. A balanced entry, its postings, its accounts and
   commodities, and the correction or reversal that supersedes it. Append-only: a posted accounting fact is
   never edited in place.
2. **Audit facts**: who did it, when, why, under which request, and what records resulted. Also append-only,
   and the only record of authority Haddon keeps. There is no approval step; see the design log, section
   "Authorisation: the model records, it does not enforce".
3. **Workflow state**: statement reconciliation, provisional bookings, review annotations. It may refer to
   accounting and audit facts. It is never a precondition for posting an ordinary entry.

## The journal core and its extensions

Extensions may import the journal core. **The journal core may not import an extension.** The core works with no
statements, documents, review state, tax records, exports or rendered reports.

### Journal core (always present)

- the entity, its chart of accounts and its commodities, with exact amount conversion;
- entries and their balanced postings;
- corrections and reversals;
- one period lock that moves forward on close and back on reopen;
- idempotency records; and
- the append-only audit log.

Invariants the core guarantees:

- **Balance per commodity.** A posted entry's legs net to zero for each commodity. Multi-commodity entries are
  allowed; there is no currency translation.
- **Exact arithmetic.** Amounts are integer micro-units. No binary floating-point value reaches the database;
  decimal strings are converted at the boundary using the commodity's declared precision.
- **Append-only posted facts.** Posted entries are corrected or reversed, never edited. Drafts may be revised
  or voided because they are not yet accounting.
- **One lock, fail-closed.** Nothing dated on or before the lock may be changed by an action that affects the
  books. Reopening moves the lock back on purpose and is itself audited.
- **Idempotent writes.** A retried command with the same request identity returns the same result and leaves
  one set of rows.
- **Audited writes.** No change without an actor, a time and a reason on the audit log.

These are enforced in the schema as well as in Python: STRICT tables, foreign keys, CHECK constraints and
triggers. `tests/test_integrity_mutation_matrix.py` attempts each forbidden write directly against SQLite and
expects the database itself to refuse it.

### Reconciliation extension (optional, on by default)

Documents, bank statements and their lines, explanations (booked, linked, ignored) and allocations between
lines and postings. "Live" is derived rather than stored: the live statement is the one nothing replaces, the
live explanation the one nothing supersedes.

`HADDON_RECONCILIATION=off` (`haddon/extensions.py`) switches it off: its routes are not mounted,
`/capabilities` does not list them, and its close checks are not part of the close policy at all. Which
extensions were running is recorded on each close.

Where the core needs to know about the extension, the extension registers a hook. The core's draft-revision
path asks a registered allocation guard whether a posting is in use (`entries.register_allocation_guard`,
registered by `repository/lines.py`); the close policy collects checks from whoever registered them, so the core
contributes `nothing_in_draft` and the extension adds `statement_coverage`, `lines_explained` and
`nothing_partial` (`repository/close_coverage.py`).

### Review extension

Evidence access and filing (`documents.py`, `api/evidence.py`), the "awaiting a document" records
(`expectations.py`), and the reviewer-oriented reads the UI uses.

### Artifact and verification extension

hledger import and export, deterministic snapshots, the oracle, and rendered statements (PDF). The pinned
hledger binary and the sample corpus are verification fixtures, not requirements of a working installation.
No generic module names a corpus checkout, a year or an entity: those live in a corpus manifest
(`corpus/example.json`) named by `HADDON_CORPUS`, and `tests/test_generic_bootstrap.py` fails if one appears in
code.

### Local extension

Records specific to a tax regime belong here; today that is recoverable tax not recovered (`unrecovered.py`).
They may not add required fields or states to an ordinary entry.

## The dependency rule

[`tests/test_core_boundaries.py`](../tests/test_core_boundaries.py) classifies every module under `src/haddon`
and fails if a core module imports a reconciliation, review, artifact or local-tax module. Extensions may import
the core and each other. The transport layer (`api/`, `ui/`) is the composition root and may import anything.

The import rule cannot see SQL that names another layer's tables, so the same test keeps an explicit allowlist
of such couplings, each with its reason. One remains: `reports/composition.py` counts import provenance from
`journal_mappings`, a table the importer owns.

## Request path

A write request passes through, in order:

1. `api/idempotency.py`: stamps a request id and refuses a mutating `/v1` request without an `Idempotency-Key`.
   It is middleware, so a new write route is covered without anyone remembering to add it.
2. `api/security.py`: resolves the bearer token (or the proxy's user header) to an actor and checks the entity
   is in scope.
3. The route (`api/routes/*.py`): parses the body against a strict Pydantic schema, then calls one command.
4. `execution.py`: opens the entity's database, begins a `BEGIN IMMEDIATE` transaction, checks or records the
   idempotency key, runs the command, stores the response, and commits; any error rolls back everything.
5. The command (`commands.py` or `reconciliation_commands.py`): converts friendly inputs (account paths,
   decimal strings) to stored form, calls the repository, and writes one audit action (`repository/audit.py`).
6. `api/errors.py`: turns any `HaddonError`, validation failure or database refusal into the standard error
   envelope. Trigger messages are mapped to stable codes in `repository/errors.py`.

## The command layer

`src/haddon/commands.py` holds the journal core's commands: `initialize_entity`, `create_account`,
`archive_account`, `restore_account`, `post_entry`, `post_batch`, `correct_entry`, `reverse_entry`,
`close_period` and `reopen_period`. `src/haddon/reconciliation_commands.py` holds the extension's:
`load_statement`, `replace_statement`, `book_line`, `link_line`, `ignore_line` and `reexplain_line`. They are two
modules because the core may not import an extension. The export command's handler is
`haddon.exporting.exporter.export_book`, and the document, expectation and tax commands live with their
repositories.

Commands come in inverse pairs where one changes state another reads as a gate (archive and restore, close and
reopen); `tests/test_command_surface_recoverable.py` holds that as an invariant.

**One name per command.** Every write route declares `operation_id` equal to its command's name, so the string
in `/capabilities`, the OpenAPI `operationId` and the function that does the work are the same.
`api/surface.py` derives the published reads, writes and commands from the routes the process actually
mounted; nothing keeps a separate list in step.

`initialize_entity` is the one command not relative to an entity, since a database file has to exist before
there is an entity to address. It is published on the collection, `POST /v1/entities`.

## Continuous time

An entity has one continuous journal. Entries carry a date, not a book year. Reports take an inclusive range with
either bound open; balances are cumulative through a date and movements are the postings inside the range. An
opening balance is an ordinary entry with `origin='opening'`. `tests/test_reports_scoping.py` fails if a report
function accepts a year again.

Two consequences:

- **An opening restatement is not an opening.** Annual source books restate the previous year's closing balance
  sheet at the start of each year. Imported onto a journal that already holds that year, it would book every
  balance-sheet account twice while still balancing. `importing/restatement.py` verifies it against the
  continuous position instead of posting it, and `oracle/continuity.py` checks the same thing against hledger at
  every year boundary.
- **The balance sheet's result runs from the start of the journal.** Nothing posts the result to equity, so the
  row that makes the sheet balance is every income and expense movement through its date.
  `/reports/balance-sheet` publishes it as `result_micros`; the shaped statements call it "Result to date".

`statements/shape.py` decides how the statements are arranged (sections, order, signs, subtotals, the result row
and the balance check). `/reports/statements` publishes that arrangement as JSON, the PDF is drawn from the same
data, and the UI renders what it is given, so the three cannot disagree.

## Inventory

The counts below are generated from the code by `bin/haddon-inventory` (`make inventory`) and checked by
`tests/test_inventory_is_generated.py`, so they cannot go stale.

<!-- inventory:begin -- generated by bin/haddon-inventory; do not edit by hand -->

| Thing | Count | Taken from |
|---|---|---|
| Entity reads (`GET` under `/v1/entities/{entity_id}`) | 33 | `api.surface.published`, every extension on |
| Entity writes, each a named command | 22 | `api.surface.published`, every extension on |
| Service-level `/v1` routes (not relative to an entity) | 2 | `api.surface.service_published`: the entity collection |
| The same surface with the reconciliation extension off | 42 | the same app under `HADDON_RECONCILIATION=off`: 26 reads, 16 writes |
| User tables in the migrated schema | 23 | `sqlite_master` of a migrated database |
| Views | 5 | the same |
| Indexes | 9 | the same |
| Triggers | 65 | the same |
| Alembic revisions | 1 | `db/migrations/versions/` |
| Files under `db/migrations/` | 5 | the same directory, recursively |
| Python modules under `src/haddon` | 105 | `*.py`, recursively |
| Repository modules (excluding `__init__`) | 20 | `repository/*.py` |

<!-- inventory:end -->

### Repository modules

| Module | Area |
|---|---|
| `tables.py`, `ctx.py`, `errors.py`, `transaction.py` | core (storage plumbing) |
| `events.py`, `requests.py`, `audit.py` | core (audit, idempotency) |
| `provisioning.py` | core (entity, chart, commodities) |
| `entries.py`, `corrections.py` | core (entries, corrections) |
| `closing.py` | core (period lock, close records, verification snapshot) |
| `close_checks.py` | core (the close policy and its fail-closed evaluation) |
| `close_coverage.py` | reconciliation (the close checks that need a loaded statement) |
| `statements.py`, `lines.py`, `reconcile.py` | reconciliation |
| `documents.py` | review and reconciliation (evidence); served through `api/evidence.py` |
| `expectations.py` | review ("awaiting a document") |
| `exports.py` | artifact |
| `unrecovered.py` | local tax |

### API route files (`src/haddon/api/routes/`)

| File | Area |
|---|---|
| `service.py`, `entity.py`, `accounts.py`, `entries.py`, `batches.py`, `events.py`, `reports.py`, `close.py` | core |
| `statements.py`, `lines.py`, `reconcile.py` | reconciliation, mounted only when the extension is on |
| `documents.py`, `provisional.py` | review |
| `artifacts.py` | artifact (exports) |
| `unrecovered.py` | local tax |

### Tables

| Table(s) | Area |
|---|---|
| `entities`, `commodities`, `accounts`, `account_commodities` | core (entity and chart) |
| `entries`, `postings`, `corrections` | core |
| `period_locks`, `close_records`, `snapshots` | core (the lock, the close, its verification snapshot) |
| `requests`, `operations`, `events` | core (idempotency, batch operations, audit) |
| `documents`, `statements`, `statement_lines`, `explanations`, `allocations` | reconciliation |
| `evidence_links` | review (evidence) |
| `document_expectations` | review |
| `unrecovered_tax` | local tax |
| `journal_mappings` | artifact (import provenance) |
| `exports` | artifact |

`tests/test_dropped_schema_stays_dropped.py` keeps tables that earlier versions removed from coming back.

## Migrations

`src/haddon/db/migrations/` is the only schema mechanism (Alembic). The SQL files a migration runs are
SHA-256-pinned, so changing the schema means a new migration, not an edit. There is currently one revision,
`0001`, holding the whole schema. `make migrate DB=<file>` upgrades one entity file to head; `bin/haddon-serve`
reports through `/readyz` whether every configured file is at head.

## Test fixtures

- **Fernside Instruments** (`tests/repo_helpers.py`): an invented instrument maker with its own chart and three
  currencies, used by most of the suite. `test_the_fixture_company_never_appears_in_src` keeps its name out of
  `src/`.
- **`tests/fixtures/fernside/2025.journal`**: one ordinary year in USD, EUR and JPY with one correction; the
  source of truth for `tests/test_golden_core.py`, which checks per-commodity balance, report totals, precision,
  mirror-not-edit corrections and fail-closed locking without the corpus or hledger.
- **The sample corpus** (`corpus/example/`): three fictional companies at realistic scale, for the importer,
  exporter, oracle, pagination and concurrency tests. Tests assert properties of it (store equals source, export
  reparses to the same multiset, balances reconcile), not hand-copied figures; the one place figures are pinned
  is the oracle's baselines file.
