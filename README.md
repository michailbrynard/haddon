# Haddon

Haddon is an open-source double-entry ledger for AI agents. An agent loads bank statements, books invoices
and accruals, files the documents behind them, reconciles each account and closes each period over an HTTP
API. It comes with a web interface where people can review the books, correct entries and close periods. If
you know Xero, think of its ledger, reconciliation and close, with an agent doing the data entry.

The API assumes the agent will sometimes be wrong. Every write is a named command, validated before it lands
and recorded with who made it and why. Posted entries are never edited, only reversed or corrected, and a
closed period refuses changes until someone reopens it. Each legal entity's books are one SQLite file, and they
import from and export to [hledger](https://hledger.org) journals, checked by a pinned hledger binary.

Haddon is pre-1.0. The schema and API are stable enough to build on, but we don't make compatibility promises
yet.

## Try it

You need Python 3.12 or later.

```bash
git clone <repository-url> haddon && cd haddon
python3 -m venv .venv && .venv/bin/pip install --upgrade pip
make install

# One set of books for one entity, with a starter chart of accounts
bin/haddon-init var/acme.db --id acme --name "Acme Ltd" --legal-form Ltd \
  --country US --base-currency USD --base-precision 2

# A configuration naming that file and one agent credential
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
cat > var/haddon.json <<EOF
{
  "entities": {"acme": "acme.db"},
  "tokens": {"$TOKEN": {"actor": "agent:bookkeeper", "entities": ["acme"]}},
  "cursor_keys": [{"id": "k1", "secret": "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"}]
}
EOF

bin/haddon-serve --config var/haddon.json     # serves on 127.0.0.1:8080
```

In another shell, with the same `TOKEN`, post an invoice:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/entities/acme/entries \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: invoice-0041" \
  -H 'Content-Type: application/json' -d '{
    "dt": "2026-04-01",
    "narration": "Invoice 0041, services for April",
    "reason": "the April invoice was raised on the 1st",
    "legs": [
      {"account": "assets:receivables", "commodity": "USD", "amount": "7316.67"},
      {"account": "income:sales",       "commodity": "USD", "amount": "-7316.67"}
    ]}'
```

The reply (`201 Created`, trimmed) has the posted entry, attributed to the token's actor. Amounts go in as
decimal strings and come back as integer millionths:

```json
{
  "entry": {
    "id": "e-b3528913b5d84e7db6b3d25403632652",
    "status": "posted",
    "created_by": "agent:bookkeeper",
    "postings": [
      {"seq": 1, "account_path": "assets:receivables", "commodity": "USD", "amount_micros": "7316670000"},
      {"seq": 2, "account_path": "income:sales", "commodity": "USD", "amount_micros": "-7316670000"}
    ]
  }
}
```

Send it again with the same `Idempotency-Key` and you get the same body back with `Idempotency-Replayed: true`,
not a second invoice. Send `"100.001"` and it is refused rather than rounded, with an error a model can act on:

```json
{"error": {"code": "precision",
           "message": "legs[0].amount: '100.001' has 3 decimal places; USD supports 2",
           "retryable": false,
           "details": [{"path": "legs[0].amount", "reason": "unsupported_precision", "places": 3,
                        "commodity": "USD", "precision": 2, "value": "100.001"}],
           "request_id": "req-ac4bf1119ea24f4398b6af0147c7cfa7"}}
```

[docs/quickstart.md](docs/quickstart.md) carries on from here: correcting the entry, loading a bank statement,
explaining its line and closing the month.

## Giving it to an agent

Each write in `/openapi.json` has its command name as its `operationId`, so tool definitions come straight out
of the schema. [`examples/haddon_tools.py`](examples/haddon_tools.py) does that with the standard library and
is meant to be copied into your harness:

```python
from haddon_tools import load_tools, call

tools, routes = load_tools("http://127.0.0.1:8080",
                           allow={"post_entry", "correct_entry", "book_line", "ignore_line"})

# pass `tools` to the model; then, for each tool call it makes:
result = call(routes, tool_call.name, tool_call.input,
              entity="acme", token=AGENT_TOKEN, call_id=tool_call.id)
```

It uses the tool call's id as the `Idempotency-Key`, so an HTTP retry can't post twice, and it hands refusals
back to the model instead of raising them. `allow` is how you keep `close_period` for a person if that's your
process. `GET /v1/entities/{id}/capabilities` tells an agent what this installation serves, how to write amounts
and dates, and the page limits. Haddon doesn't ship an MCP server; the
[agent guide](docs/agent-guide.md) covers reads, errors and the guardrails we recommend.

## What it does

Haddon keeps books for any number of legal entities, each with its own chart of accounts, currencies and period
lock.

- Bank reconciliation. The agent reads a statement in whatever format the bank provides and submits its lines.
  Each line is booked as a new entry, linked to entries already in the books (splits included), or ignored with
  a reason, and Haddon reports whether each account agrees with the bank.
- Journal entries for invoices, bills, accruals, payroll, depreciation and adjustments, posted singly or in
  all-or-nothing batches checked against the caller's own counts and totals.
- Documents, registered by hash and filed against the entries they support. A booking made on incomplete
  evidence can be marked as awaiting a document.
- Month-end and year-end close. Before a period closes Haddon checks that every statement is loaded and every
  line explained, and a check that can't tell counts as a failure. You can close over a failing check only with
  a recorded override.
- Tax preparation. Accounts and postings carry tax codes in the entity's own scheme, and a separate register
  records recoverable tax (VAT, GST and the like) deliberately not reclaimed, with the reason and the last date
  a claim is possible.
- Trial balance, income statement and balance sheet with a comparative period (JSON or PDF), account registers,
  and an append-only audit log of every change.

The write commands are all named, and there is no generic update endpoint:

| Area | Commands |
|---|---|
| Chart | `create_account`, `archive_account`, `restore_account` |
| Journal | `post_entry`, `post_batch`, `correct_entry`, `reverse_entry` |
| Period | `close_period`, `reopen_period` |
| Statements | `load_statement`, `replace_statement`, `book_line`, `link_line`, `ignore_line`, `reexplain_line` |
| Documents | `register_document`, `file_document`, `await_document`, `withdraw_expectation` |
| Tax not recovered | `record_unrecovered_tax`, `resolve_unrecovered_tax` |
| Export | `run_export` |
| Books | `initialize_entity` (`POST /v1/entities`) |

Each one runs in a single transaction and writes an audit event with the actor, the reason and a hash of the
input. The rule against editing posted entries is enforced by database triggers as well as the API. A
correction posts an exact mirror of the original plus a replacement, so the history of what was believed and
when stays readable. Nothing in the data model tells an agent from a person: a credential is an actor string
such as `agent:bookkeeper` or `ui:alice`, and the same rules apply to both.
[docs/concepts.md](docs/concepts.md) covers the accounting model in full.

## How it compares

Haddon covers the ledger, reconciliation, close and reporting parts of a product like Xero. It doesn't try to
be the rest of it.

| | Haddon | Xero and similar |
|---|---|---|
| Data entry | an AI agent, through an API built for it | people, with rules and suggestions |
| Bank data | statements the agent reads and submits (CSV, PDF, anything) | live bank feeds |
| Ledger | posted entries immutable, every change attributed and explained | editable transactions with an audit history |
| Close | checked month-end and year-end close with recorded overrides | lock dates |
| Hosting | self-hosted, one SQLite file per entity, MIT licence | hosted SaaS |
| Invoicing, bills, payments | recorded as entries, not sent or paid | yes |
| Payroll | booked as journal entries | in some countries |
| Tax returns | keeps codes and figures, knows no country's rules, files nothing | VAT/GST/sales tax in many countries |
| Multi-currency | per-currency balances, no conversion or revaluation | conversion, revaluation, gain/loss |
| Users and roles | a bearer token per agent, a reverse proxy for people, no roles | multi-user with roles |
| Contacts, inventory, projects | no | yes |

If you need those things, Haddon won't replace your accounting system. It's for when you want an agent to keep
the books, with every step recorded and checkable by whoever needs to. For how it sits next to hledger,
Beancount, Ledger and GnuCash, see [docs/comparison.md](docs/comparison.md).

## Web interface

Haddon comes with a web interface where you can browse the books, review and correct entries and close
periods, if you want to. It is a React app served by the same process (`make web` builds it). It has no logic
of its own and goes through the same API, so anything it shows an agent can query, and everything a person does
in it is recorded under their own actor name.

The Review page (`/e/{entity}/month`) takes one month and shows whether the bank agrees with the books and what
still needs a decision, all of which can be finished on the page. The Inbox (`/e/{entity}/review`) collects
everything unfinished across periods, and Closing (`/e/{entity}/close`) shows each close check and is where a
period is closed or reopened. There are also pages for entries with their correction chain, accounts and
registers, and the reports.

When someone changes something already posted, the interface asks for one sentence on why the earlier
booking was wrong, because that is the part nobody can reconstruct later. See
[docs/reviewer-ui.md](docs/reviewer-ui.md).

## hledger import and export

`bin/haddon-import` brings in existing books in hledger's journal format, and `bin/haddon-export` (or
`POST /exports`) writes any date range back out. Exports are deterministic and must pass
`hledger check --strict` with the pinned binary before they are published. The oracle (`bin/haddon-oracle`)
loads a corpus of journals and compares the source, the store and the re-exported journal for every book. See
[docs/hledger.md](docs/hledger.md).

## Running it

The service reads one JSON file, given by `--config` or `$HADDON_CONFIG`:

| Key | Purpose |
|---|---|
| `entities` | entity id to database file, one entity per file |
| `tokens` | bearer token to `{actor, entities}`; the actor is recorded on everything the token writes |
| `cursor_keys` | signing keys for pagination cursors, newest first |
| `proxy_auth` | lets an authenticating reverse proxy pass a person's name in a header (`ui:<name>`) |
| `entity_root`, `blob_root`, `export_root` | directories for books created over the API, document bytes and exports; each feature is off until set |

`HADDON_RECONCILIATION=off` switches off the statement extension, its routes and its close checks.

A deployment is one Python process on loopback behind a reverse proxy. `deploy/` has an example systemd unit,
service configuration and nginx server block. [docs/operations.md](docs/operations.md) covers adding books,
exports, backups that don't lose WAL data, rehearsing a restore, and a checklist for before you deploy.

## Development

```
src/haddon/
  commands.py                 journal commands: one function per write, shared by HTTP and Python callers
  reconciliation_commands.py  statement and line commands (an optional extension)
  api/                        FastAPI app, routes, schemas, auth, idempotency, pagination
  repository/                 database reads and writes, close checks, corrections
  db/migrations/              the schema: STRICT tables, CHECKs and triggers
  reports/  statements/       balances, registers, trial balance, income statement, balance sheet, PDF
  importing/  exporting/      hledger journal import and deterministic export
  oracle/                     the source / store / export cross-check
web/                          the reviewer UI (React, TypeScript, Vite)
corpus/                       a fictional three-company sample corpus and its generator
docs/design/                  the decision record kept while Haddon was built
```

```bash
make test-fast     # about a minute: everything except the slow tests
make test-gate     # the full suite and the oracle over the sample corpus (about ten minutes)
make web-check     # type-check the reviewer UI
make site          # build the documentation site into site/dist (Node 22; see site/README.md)
```

A change has to pass `make test-gate`. The import, export and oracle tests need hledger 1.52.1 at
`vendor/hledger`, checked against the hashes in `vendor/hledger.sha256`:

```bash
# Linux x86_64 (macOS arm64: use hledger-mac-arm64.tar.gz)
curl -sSLf https://github.com/hledgerorg/hledger/releases/download/1.52.1/hledger-linux-x64.tar.gz \
  | tar -xz -C vendor hledger
```

PDF statements use [WeasyPrint](https://weasyprint.org), which needs Pango installed on the system.

## Documentation

- [docs/quickstart.md](docs/quickstart.md): from an empty directory to a closed month, over HTTP
- [docs/using-from-an-agent.md](docs/using-from-an-agent.md): the contract an agent works under, on one page
- [docs/agent-guide.md](docs/agent-guide.md): connecting an LLM agent, tool definitions, errors, guardrails
- [docs/concepts.md](docs/concepts.md): the accounting model and the rules the ledger enforces
- [docs/api.md](docs/api.md): API conventions and the route list
- [docs/reviewer-ui.md](docs/reviewer-ui.md): the web interface
- [docs/hledger.md](docs/hledger.md): import, export, the oracle and corpora
- [docs/operations.md](docs/operations.md): configuration, deployment, backup and restore
- [docs/architecture.md](docs/architecture.md): the journal core, the extensions and the dependency rule
- [docs/comparison.md](docs/comparison.md): Haddon next to hledger, Beancount and Fava, Ledger and GnuCash

## Contributing

Bug reports, questions and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has the development
setup and what a change needs before it can be merged. Please report vulnerabilities as described in
[SECURITY.md](SECURITY.md). Participation is covered by the [code of conduct](CODE_OF_CONDUCT.md).

## License

MIT. See [LICENSE](LICENSE).
