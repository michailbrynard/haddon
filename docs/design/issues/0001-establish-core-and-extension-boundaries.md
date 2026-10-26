# 0001 — Establish the journal core and extension boundaries

**Kind:** architecture + dependency reduction  
**Risk:** medium  
**Effort:** M

## Problem

Haddon currently presents journal entry, statement ingestion, reconciliation, document expectations, tax
recovery, approval records, close checks and artifact verification as one indivisible ledger. That makes each
new concern spread through migrations, tables, triggers, repository functions, schemas, routes,
capabilities, tests and documentation.

The result is locally rigorous but hard to understand or change. The product needs an explicit stable core
and a small number of extensions with one-way dependencies.

## Decision

Use these conceptual boundaries. They may initially remain packages in one process and database; a plugin
runtime is not required.

### Journal core

- entity metadata and chart of accounts;
- commodities and exact amount conversion;
- entries and balanced postings;
- correction/reversal relationships;
- one monotonically advancing period lock;
- idempotency/request records; and
- immutable, human-readable audit events.

The core must be usable without a statement, source document, tax workflow, proposal or report artifact.

### Reconciliation extension

- source documents;
- statements and statement lines;
- allocations between statement lines and postings; and
- explicit dispositions for lines that will not be booked.

### Review extension

- evidence access and annotations;
- reviewer-oriented queries and UI;
- optional pending-review tasks; and
- exceptional workflows such as “awaiting a document”.

### Artifact and verification extension

- hledger import/export;
- deterministic snapshots and the oracle; and
- PDF or other rendered reports.

Country- or company-specific tax records, including unrecovered-tax workflow, belong in a local extension.
They must not add required fields or state transitions to an ordinary journal entry.

## Work

1. Record this boundary as a short architecture decision and link it from the main README.
2. Draw a package-level dependency rule: extensions may import the journal core; the core may expose stable
   data/query interfaces but may not import an extension.
3. Inventory each current table, route and repository module against the four areas. Mark transitional tables
   that issues 0003 and 0004 will remove.
4. Move company-specific entity registries, country mappings and corpus paths out of generic journal modules.
   Do not create a dynamic plugin framework; ordinary Python package/config boundaries are enough.
5. Keep current database deployment intact during this change. Splitting databases or removing duplicated
   `entity_id` columns is explicitly deferred.

## Acceptance criteria

- The architecture documentation names the journal-core types and invariants in one page.
- A dependency check or focused test prevents journal-core modules from importing reconciliation, review,
  artifact or local-tax modules.
- Every public route is labelled core or extension in generated capabilities/documentation.
- Provisioning a journal does not require statement-source, tax or artifact configuration.
- No new registry, base class or runtime plugin loader is introduced merely to express these boundaries.

## Non-goals and safeguards

- Do not weaken balance, commodity precision, lock or append-only-history constraints.
- Do not add FX translation or a reporting currency.
- Do not migrate the one-file-per-entity layout in this roadmap.
- “Optional” does not mean unaudited. Extension actions that affect accounting records still emit core audit
  events and use core idempotency.
