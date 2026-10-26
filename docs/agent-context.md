# Haddon: context for an agent

Haddon is an open-source double-entry ledger for AI agents. Agents write over an HTTP API: they post entries,
load bank statements, explain their lines and close periods. It comes with a web interface where people can
review the books, correct entries and close periods.

This page is meant to be pasted into an agent's instructions. The links at the end have everything else.

<!-- The documentation site serves this file at /agent-context.md and its homepage button copies it, with the
links made absolute. Links starting with / are to the site, not to this repository. -->

## Running it locally

Skip this if someone has given you a server address and a token. Otherwise, with Python 3.12 or later:

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

## Authenticating

Send `Authorization: Bearer <token>` with every request. The token decides two things: the actor every write
is recorded under, and which entities you can reach. A request without a valid token is `401
unauthenticated`; a request for an entity outside the token's scope is `403 forbidden`.

## Ask the service before doing anything

Don't assume what an installation offers. Read it:

1. `GET /v1/entities` lists the entities (sets of books) this token can reach, with each one's currencies and
   period lock (`lock_through`).
2. `GET /v1/entities/{id}/capabilities` lists every command and read this installation serves, the actor
   your writes are recorded under, how to write amounts and dates, and the page limits. Each command's `name`
   is the `operationId` of its route in `/openapi.json`, where the request and response schemas are.
3. `GET /v1/entities/{id}/accounts` is the chart of accounts you book against.

Everything else is under `/v1/entities/{id}/`.

## Writing

- Every `POST` under `/v1` needs an `Idempotency-Key` header, and is refused with `422` without one. Use one
  key per intent and reuse it when you retry. The same key with the same request returns the stored reply with
  `Idempotency-Replayed: true` and writes nothing twice; the same key with a different request is
  `409 idempotency_conflict`.
- Amounts are decimal strings in whole units (`"7316.67"`), never JSON numbers, with no more decimal places
  than the commodity has. Debits are positive and credits negative, and an entry's legs sum to zero per
  commodity. Dates are `YYYY-MM-DD`.
- `post_entry` posts the entry straight away. There is no draft to save and come back to.
- A posted entry is never edited. Fix one with `correct_entry` (`POST /v1/entities/{id}/entries/{entry_id}/correct`
  with a `reason` and a `replacement`), or cancel it with `reverse_entry`. The reason is kept as written, so
  say why the original was wrong.

## Refusals

- Every error has the same shape: `{"error": {"code", "message", "retryable", "details", "request_id"}}`.
  Branch on `code`, read `details` for the field and the reason, fix the call and send it again. Retry an
  unchanged request only when `retryable` is true. When the service knows the next step, `next_action` says it.
- A write dated on or before an entity's period lock is refused with `409 period_locked`. That refusal is
  correct: someone has closed that period, and its figures may already have been reported.
  Stop and report it. Don't look for a way around it, and don't reopen the period yourself.

## Where the rest is

- [llms.txt](/llms.txt): every documentation page as markdown, one line each.
- [llms-full.txt](/llms-full.txt): all of the documentation in one file.
- [openapi.json](/openapi.json): every route, request and response. A running service serves the same
  document at `/openapi.json`.
- [Using Haddon from an agent](using-from-an-agent.md): this page at greater length, with examples.
