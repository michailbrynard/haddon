# 0006 — Design the agent command surface and generic bootstrap

**Kind:** API/CLI product design  
**Risk:** medium  
**Effort:** L  
**Status:** done. The decisions and what this text got wrong are in
[`decisions.md`](../decisions.md) § "Issue 0006".

## Problem

The current API exposes many storage-shaped routes and requires internal IDs and integer micro-unit strings in
places where an agent naturally knows an account path and a decimal amount. Initialization and import are also
tied to a particular corpus checkout, year and three hardcoded entities.

The underlying strictness is valuable, but the interface makes correct common work unnecessarily expensive.
Agents need a small set of discoverable, idempotent commands with precise errors; humans need the same
vocabulary reflected in the UI and documentation.

## Desired surface

Keep rich read resources for inspection. Center writes on named commands backed by the same application
handlers regardless of transport:

### Journal core

- `initialize_entity`
- `create_account` / `archive_account`
- `post_entry`
- `correct_entry` / `reverse_entry`
- `close_period` / `reopen_period`

### Reconciliation extension

- `load_statement` / `replace_statement`
- `book_line` / `link_line` / `ignore_line` / `reexplain_line`
- `review_period` — the `GET /reconciliation` read

> These three bullets were written before issue 0004 replaced the classification model. `classify_line` /
> `replace_classification` are `book_line` / `link_line` / `ignore_line` / `reexplain_line`: the same concepts
> under names that say which of the four things is being said about a line. Publishing the older names beside
> them would be one idea implemented twice, which is the defect this plan exists to remove.

An HTTP API can expose these directly. A future agent-tool/MCP adapter should call the same typed handlers and
must not reproduce business logic.

## Friendly exact inputs

- Accept account paths wherever a path is unique within the entity; keep account IDs as an advanced option.
- Accept exact decimal strings such as `"12.34"` with a commodity, converting by declared commodity precision.
- Use exact decimal strings in the public command surface. Keep integer micros inside the domain and importer;
  expose a second wire representation only if a concrete integration demonstrates that it is needed.
- Never accept a JSON floating-point amount.
- Return canonical account IDs/paths and integer-unit values so the result remains unambiguous.

Errors should identify a stable code, the exact field, the violated invariant and useful candidates where
safe—for example an unknown account path or ambiguous currency—without exposing a stack trace.

## Generic initialization and import

1. Add a minimal initialization command/config containing entity identity, base currency, commodities with
   precision and an optional starter chart. It must not require a calendar-year book.
2. Move checkout paths, default years and hardcoded `ENTITIES` out of `src/haddon/importing`, oracle and
   exporter runtime defaults into an explicit corpus manifest or corpus-specific adapter.
3. Keep hledger journal import as a supported adapter that resolves account paths and preserves source
   provenance. The current corpus remains a verification fixture, not the definition of a valid Haddon
   installation.
4. Provide a small example entity and end-to-end quickstart that initializes, posts, corrects and reports
   without private data.

## Acceptance criteria

- A new generic entity can be initialized from a short documented command or manifest.
- An agent can post a two-leg entry using date, account paths, commodity and exact decimal strings in one
  idempotent request.
- The command response links to the created entry and its audit action; it does not require understanding
  proposals, decisions or applications.
- Correction, statement replacement and close are discoverable typed commands rather than repository-only
  capabilities.
- `src/haddon` contains no baked-in corpus checkout path, fixed import year or closed entity choice list in
  generic modules.
- One contract test drives each command through the transport and directly through its application handler.

## What shipped

- Named commands in `haddon.commands` (core) and `haddon.reconciliation_commands` (extension); every mutating
  route is a thin face over one, and its `operation_id` is the command's name.
- `/capabilities` derives its reads, writes and command names from the routers the process mounted
  (`api/surface.py`); the hand-written list in `routes/entity.py` is deleted.
- `create_account` writes the audit action `POST /accounts` never wrote; `archive_account` and `correct_entry`
  gained the routes they never had.
- Write replies are validated against the model their route declares, inside the transaction and on replay.
- Decimal strings and account paths on every write; integer micros on every response.
- `CHECKOUT`, `BOOK_YEAR`, `ENTITIES`, `DEFAULT_ENTITIES`, `FROM_YEAR/TO_YEAR` and the oracle's baselines are a
  corpus manifest outside the package (`corpus/`, `HADDON_CORPUS`).
- `docs/quickstart.md` runs end to end against a company with no fixture in this repository
  (`tests/test_generic_bootstrap.py`), which is Phase 4's exit gate.

## Pitfalls

1. Friendly input is a boundary conversion, not a move from integer arithmetic to decimal or float storage.
2. Do not create a second “simple API” beside the old one. Extract command handlers, switch the in-repository
   callers and delete superseded write routes in the same development phase.
3. Avoid a single generic `execute(kind, payload)` command; named schemas are easier for agents to discover and
   safer to evolve.
4. Generated capabilities should describe only actions the current actor and enabled extensions can actually
   perform.
