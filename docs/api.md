# API reference

The authoritative reference is the OpenAPI document the service publishes:

- `GET /openapi.json`: every route, request and response schema, and the limits the service enforces;
- `GET /docs` and `GET /redoc`: the same document rendered for a browser.

It is generated from the running code, so it always matches the installation you are talking to. This page
summarises the conventions and lists the routes. For how an agent should use them, see
[agent-guide.md](agent-guide.md).

## Conventions

**Base path.** Everything an entity owns is under `/v1/entities/{entity_id}`. Each entity is a separate database
file; an id that belongs to another entity is `404`.

**Authentication.** `Authorization: Bearer <token>`. Each token maps to an actor string and a list of entities
in the service configuration. No token is `401`, an entity outside the token's scope is `403`. The reviewer UI
authenticates through a reverse proxy instead (see [operations.md](operations.md)).

**Writes** are `POST` to a named command. Each requires an `Idempotency-Key` header. The same key with the same
request replays the stored response with `Idempotency-Replayed: true`; the same key with a different request is
`409 idempotency_conflict`. The stored response commits in the same transaction as the write.

**Amounts.** Request amounts are exact decimal strings in whole commodity units (`"-12.00"`); response amounts
are integer strings of micro-units (`"-12000000"`, fields ending in `_micros`). JSON numbers are refused in both
directions, as is more precision than the commodity declares.

**Dates.** `YYYY-MM-DD`. Ranges are inclusive `from` and `to`, either of which may be omitted; point-in-time
reports take `as_of`. Unknown query parameters are ignored.

**Lists** return `{items, next_cursor, snapshot_seq}`. Cursors are opaque and signed; they bind the entity, the
filters, the ordering and a snapshot. Pass `next_cursor` back as `cursor` unchanged. Default page size 50,
maximum 200 (events: 100 and 500). An expired cursor is `409 cursor_expired` with a link to restart.
`/events` is paged by sequence number instead: `since`, `next_since` and `high_water`.

**Responses** to entity requests include `entity_id`, `legal_name`, `base_currency` and `snapshot_seq` (the
audit log's high-water mark when the response was produced), and a `links` object naming related resources.

**Errors** are always `{"error": {"code", "message", "retryable", "details", "request_id"}}`. The codes are
listed in [agent-guide.md](agent-guide.md#reading-refusals). Every response carries an `X-Request-Id` header.

## Service routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness |
| GET | `/readyz` | readiness: migrations, a test write per entity, the hledger binary, cursor keys |
| GET | `/v1/entities` | the entities this credential can reach, with each one's period lock |
| POST | `/v1/entities` | `initialize_entity`: create a new set of books (needs `entity_root` in the configuration) |

## Entity reads

All paths are relative to `/v1/entities/{entity_id}`.

| Path | Returns |
|---|---|
| `` (the entity itself) | entity summary |
| `/capabilities` | the commands, reads, contracts and limits this installation serves |
| `/accounts`, `/accounts/{id}` | the chart; one account |
| `/accounts/{id}/register` | an account's postings with a running balance (`from`, `to`) |
| `/entries`, `/entries/{id}` | entries (filters: `status`, `origin`, `from`, `to`, `account`); one entry with its state and correction chain |
| `/entries/{id}/events` | the audit events for an entry, including the statement line that explains it |
| `/events` | the entity's audit log (`since`, `limit`) |
| `/reports/trial-balance` | balances per account and commodity (`as_of`) |
| `/reports/balance-sheet` | assets, liabilities, equity and the result to date (`as_of`) |
| `/reports/income-statement` | income and expense movements (`from`, `to`) |
| `/reports/statements`, `/reports/statements.pdf` | both statements arranged for presentation, as JSON or PDF |
| `/reports/composition` | entry counts by origin and source journal |
| `/close-checks` | what a close through a date would find (`through`); read-only |
| `/closes/{id}` | one close or reopen record |
| `/snapshots/{id}` | a verification snapshot taken by a close or an export |
| `/statements`, `/statements/{id}`, `/statements/{id}/lines` | loaded bank statements and their lines |
| `/lines`, `/lines/{id}` | statement lines with their explanation and history (`from`, `to`, `state`) |
| `/reconciliation` | per cash account: does the bank agree with the books, and is every line explained |
| `/provenance` | the documents and lines behind a `line_id`, `posting_id` or `entry_id` |
| `/documents`, `/documents/{id}` | registered documents (`sha256` to look one up) and what they are filed against |
| `/documents/{id}/content` | the document's bytes, if `blob_root` is configured and the hash matches |
| `/provisional` | entries awaiting a document |
| `/unrecovered-tax`, `/unrecovered-tax/{id}` | recoverable tax not recovered, with totals per commodity |
| `/exports/{id}`, `/exports/{id}/artifacts/{name}` | an export record; one published journal file |

## Entity writes

| Command | Route | Notes |
|---|---|---|
| `create_account` | `POST /accounts` | path, type and commodities are required; `reason` optional |
| `archive_account` | `POST /accounts/{id}/archive` | reason required; history is kept |
| `restore_account` | `POST /accounts/{id}/restore` | the inverse of archive |
| `post_entry` | `POST /entries` | one balanced entry, posted immediately |
| `post_batch` | `POST /batches` | many entries, all or nothing, checked against the caller's count and totals |
| `correct_entry` | `POST /entries/{id}/correct` | mirror plus replacement, one transaction |
| `reverse_entry` | `POST /entries/{id}/reverse` | mirror only |
| `close_period` | `POST /close` | `through`, `reason`, `expected_version`, optional `overrides` |
| `reopen_period` | `POST /reopen` | `through` or `close_id`, `reason`, `expected_version` |
| `load_statement` | `POST /statements` | reconciliation extension |
| `replace_statement` | `POST /statements/{id}/replace` | reconciliation extension |
| `book_line` | `POST /lines/{id}/book` | reconciliation extension |
| `link_line` | `POST /lines/{id}/link` | reconciliation extension; several parts make a split |
| `ignore_line` | `POST /lines/{id}/ignore` | reconciliation extension |
| `reexplain_line` | `POST /lines/{id}/reexplain` | reconciliation extension; corrects anything the old explanation booked |
| `register_document` | `POST /documents` | identity is the SHA-256; a repeat returns the existing record with `200` |
| `file_document` | `POST /entries/{id}/documents` | allowed in a closed period; meets an open expectation |
| `await_document` | `POST /entries/{id}/awaiting-document` | marks an entry provisional |
| `withdraw_expectation` | `POST /entries/{id}/awaiting-document/withdraw` | no document is coming |
| `record_unrecovered_tax` | `POST /entries/{id}/unrecovered-tax` | records an amount; posts nothing |
| `resolve_unrecovered_tax` | `POST /unrecovered-tax/{id}/resolve` | `recovered` (naming the entry) or `forgone` |
| `run_export` | `POST /exports` | hledger journal files for a date range (needs `export_root`) |

`docs/architecture.md` carries a generated count of these routes, checked by the test suite so it cannot drift.

## Python

The same commands are plain functions, for scripts and tests that run in-process:

```python
from haddon.commands import LegSpec, post_entry
from haddon.db.connection import make_engine
from haddon.repository.ctx import WriteCtx
from haddon.repository.transaction import write_transaction

engine = make_engine("var/acme.db")
with write_transaction(engine) as conn:
    posted = post_entry(WriteCtx.open(conn, "agent:bookkeeper"), dt="2026-04-01",
                        narration="Invoice 0041, services for April",
                        reason="the April invoice was raised on the 1st",
                        legs=[LegSpec("assets:receivables", "USD", "7316.67"),
                              LegSpec("income:sales", "USD", "-7316.67")])
```

The journal commands are in `haddon.commands`, the statement and line commands in
`haddon.reconciliation_commands`. The Python API is less settled than the HTTP API and may change between
releases.
