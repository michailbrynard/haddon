# Quickstart

This walks from an empty directory to a closed month using only the HTTP API. You'll create a set of books,
post an invoice and correct it, load a bank statement and explain its one line, close April, and then watch
the closed period refuse a late entry.

Every command here has been run as written against a fresh clone. You'll need `curl`, `jq`, and `uuidgen` for
idempotency keys.

## 1. Install

Python 3.12 or later.

```bash
git clone <repository-url> haddon && cd haddon
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
make install
```

`make install` is `pip install -e '.[dev]'`: the service plus the test and server dependencies. The hledger
binary is not needed for anything on this page; it is only used by the import, export and oracle tools (see
[hledger.md](hledger.md)).

## 2. Create a set of books

One SQLite file holds one entity (one legal entity's books).

```bash
bin/haddon-init var/acme.db --id acme --name "Acme Ltd" --legal-form Ltd \
  --country US --base-currency USD --base-precision 2
```

This creates and migrates the file, records the entity and its base currency, and adds a starter chart:
`assets:cash`, `assets:receivables`, `liabilities:payables`, `expenses:general`, `income:sales` and
`equity:retained-earnings`. Add `--commodity EUR:2` for each further currency. The output is the
`entity.initialized` audit event.

`var/` is gitignored, so it is a convenient place for local books and configuration.

## 3. Configure and start the service

```bash
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
cat > var/haddon.json <<EOF
{
  "entities": {"acme": "acme.db"},
  "tokens": {"$TOKEN": {"actor": "agent:bookkeeper", "entities": ["acme"]}},
  "cursor_keys": [{"id": "k1", "secret": "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"}]
}
EOF
echo "$TOKEN" > var/token          # so another shell can read it

bin/haddon-serve --config var/haddon.json --check
bin/haddon-serve --config var/haddon.json
```

`--check` opens every configured entity, prints a readiness report (migrations, a test write, the hledger
binary, the cursor keys) and exits 0 when the service is ready. A missing hledger binary shows as a failed
`verifier` check and makes `--check` exit 1; the service still starts and serves everything except exports.

Paths in the configuration are relative to the configuration file, so `acme.db` is `var/acme.db`. The token's
`actor` is the name recorded on everything written with it.

## 4. Look around

In a second shell:

```bash
TOKEN=$(cat var/token)
E=http://127.0.0.1:8080/v1/entities/acme
AUTH="Authorization: Bearer $TOKEN"
JSON='Content-Type: application/json'

curl -s -H "$AUTH" "$E/capabilities" | jq '{commands: [.commands[].name], limits, amount_contract}'
curl -s -H "$AUTH" "$E/accounts" | jq '.items[] | {path, type, commodities}'
```

`/capabilities` is the discovery document for an agent: which commands this installation serves, how amounts
and dates are written, and the page limits.

## 5. Post an entry

```bash
ENTRY=$(curl -s -X POST "$E/entries" -H "$AUTH" -H "$JSON" -H "Idempotency-Key: $(uuidgen)" -d '{
    "dt": "2026-04-01",
    "narration": "Invoice 0041, services for April",
    "reason": "the April invoice was raised on the 1st",
    "legs": [
      {"account": "assets:receivables", "commodity": "USD", "amount": "7316.67"},
      {"account": "income:sales",       "commodity": "USD", "amount": "-7316.67"}
    ]}' | jq -r .entry.id)
echo "$ENTRY"
```

Accounts are given by path. Amounts are decimal strings in whole units, converted using the currency's declared
precision; the response gives them back as integer micro-units (`"7316670000"`). The legs must sum to zero per
currency, or the request is refused with `422 unbalanced` and nothing is written.

## 6. Correct it

The invoice was really for 7,361.67. A posted entry is never edited: `correct` posts an exact mirror of the
original and the replacement in one transaction, and links the three.

```bash
curl -s -X POST "$E/entries/$ENTRY/correct" -H "$AUTH" -H "$JSON" -H "Idempotency-Key: $(uuidgen)" -d '{
    "reason": "the invoice total is 7361.67; the digits were transposed",
    "replacement": {
      "dt": "2026-04-01", "narration": "Invoice 0041, services for April",
      "legs": [
        {"account": "assets:receivables", "commodity": "USD", "amount": "7361.67"},
        {"account": "income:sales",       "commodity": "USD", "amount": "-7361.67"}
      ]}}' | jq '{correction_id, original_id, mirror_id, replacement_id}'

curl -s -H "$AUTH" "$E/entries/$ENTRY" | jq '{state, reversed_by, corrections: [.corrections[].reason]}'
curl -s -H "$AUTH" "$E/entries/$ENTRY/events" | jq '.items[] | {kind, reason, created_by}'
```

The original now reads `"state": "corrected"`, and its events show `entry.posted` followed by
`entry.corrected`, each with its reason and actor. `POST /entries/{id}/reverse` with just a `reason` is the
same act without a replacement, for a booking that should never have been made.

## 7. Load the month's bank statement

An agent reads the bank's file (CSV, PDF, whatever it is) outside the ledger and submits it as normalised lines.
The ledger checks it and keeps a reference to the source document by its SHA-256.

```bash
STATEMENT=$(curl -s -X POST "$E/statements" -H "$AUTH" -H "$JSON" -H "Idempotency-Key: $(uuidgen)" -d '{
    "account": "assets:cash", "commodity": "USD",
    "period_start": "2026-04-01", "period_end": "2026-04-30",
    "opening": "0.00", "closing": "-1450.00",
    "lines": [{"dt": "2026-04-02", "description": "OFFICE LEASE APR", "amount": "-1450.00",
               "currency": "USD", "reference": "TX-3301"}],
    "document": {"sha256": "'"$(printf 'april statement' | sha256sum | cut -c1-64)"'",
                 "filename": "2026-04.csv", "media_type": "text/csv", "size_bytes": 512,
                 "external_ref": "bank-portal:2026-04"},
    "locator": "page 1, closing balance",
    "reason": "the April statement, as downloaded"}' | jq -r .statement.statement_id)

LINE=$(curl -s -H "$AUTH" "$E/statements/$STATEMENT/lines" | jq -r '.items[0].line_id')
```

The statement is refused whole if its lines do not sum to closing minus opening, if a line falls outside the
period, if the currencies disagree, if the same document was already loaded, or if it overlaps another
statement for the same account.

## 8. Explain the line

The line is the April rent. The chart has no rent account yet, so add one, then book the line to it.

```bash
curl -s -X POST "$E/accounts" -H "$AUTH" -H "$JSON" -H "Idempotency-Key: $(uuidgen)" -d '{
    "path": "expenses:rent", "type": "expense", "commodities": ["USD"],
    "reason": "the office lease starts in April"}' | jq .account.path

curl -s -X POST "$E/lines/$LINE/book" -H "$AUTH" -H "$JSON" -H "Idempotency-Key: $(uuidgen)" -d '{
    "narration": "Office rent, April", "reason": "the lease invoice for April",
    "legs": [{"account": "expenses:rent", "commodity": "USD", "amount": "1450.00"}]}' \
  | jq '.line | {status, unexplained_micros, entry: .explanation.entry_id}'
```

`book` posts a new entry for the line. You supply the other side (`expenses:rent`); the cash leg comes from the
line itself. The other ways to explain a line are `link` (the money is already in the books: point at existing
postings, splitting across several if needed) and `ignore` (it will not be booked, and the reason says why).

## 9. Close April

Ask first. The answer lists every check with its status, plus the lock version the close has to quote
back.

```bash
curl -s -H "$AUTH" "$E/close-checks?through=2026-04-30" \
  | jq '{eligible, checks: [.checks[] | {check, status}], lock}'

VERSION=$(curl -s -H "$AUTH" "$E/close-checks?through=2026-04-30" | jq .lock.version)
curl -s -X POST "$E/close" -H "$AUTH" -H "$JSON" -H "Idempotency-Key: $(uuidgen)" -d '{
    "through": "2026-04-30", "reason": "April is reconciled and reviewed",
    "expected_version": '"$VERSION"'}' | jq '{close_id, lock, snapshot}'
```

All four checks pass. The cash account has a statement for April, its line is explained, nothing is half
allocated and nothing is in draft. Without step 7 the close would be refused with `409 close_not_eligible`,
naming `statement_coverage` and the account. You can still close over a failing check, but only by naming it in
`overrides` with a reason, which is stored on the close record.

`expected_version` guards against closing books that changed since you looked: if the lock has moved, the close
fails with `412 stale_version`.

## 10. See the lock hold

```bash
curl -s -X POST "$E/entries" -H "$AUTH" -H "$JSON" -H "Idempotency-Key: $(uuidgen)" -d '{
    "dt": "2026-04-20", "narration": "A late invoice", "reason": "it arrived after the close",
    "legs": [{"account": "expenses:general", "commodity": "USD", "amount": "10.00"},
             {"account": "liabilities:payables", "commodity": "USD", "amount": "-10.00"}]}' | jq .error
```

```json
{
  "code": "period_locked",
  "message": "2026-04-20 is on or before the period lock 2026-04-30; reopen the period through an earlier date before changing it",
  "retryable": false,
  "details": [{"path": "dt", "dt": "2026-04-20", "locked_through": "2026-04-30", "remedy": "reopen"}],
  "request_id": "req-..."
}
```

To book it, reopen first. A reopen is not gated on the checks, and it keeps the close record:

```bash
curl -s -X POST "$E/reopen" -H "$AUTH" -H "$JSON" -H "Idempotency-Key: $(uuidgen)" -d '{
    "through": "2026-03-31", "reason": "a late April invoice arrived",
    "expected_version": '"$((VERSION + 1))"'}' | jq .lock
```

## 11. Read the books

```bash
curl -s -H "$AUTH" "$E/reports/trial-balance?as_of=2026-04-30" | jq '.lines[] | {account_path, amount_micros}'
curl -s -H "$AUTH" "$E/reports/balance-sheet?as_of=2026-04-30" | jq .
curl -s -H "$AUTH" "$E/reports/income-statement?from=2026-04-01&to=2026-04-30" | jq .
curl -s -H "$AUTH" "$E/events" | jq '.items[] | {seq, kind, created_by, reason}'
```

Reports take an inclusive date range over one continuous journal (`as_of` for a balance, `from`/`to` for a
period); either bound may be left out. `/events` is the whole audit log in sequence order; pass `since=<seq>`
to read on from where you stopped.

## Retries

Every write needs an `Idempotency-Key`. Use one key per intent and reuse it when retrying: the same key with the
same request returns the original response with `Idempotency-Replayed: true` and writes nothing new; the same
key with a different request is `409 idempotency_conflict`. A refused request leaves no record, so the corrected
request may reuse its key.

## Next

- [agent-guide.md](agent-guide.md): wiring these commands into an LLM agent
- [reviewer-ui.md](reviewer-ui.md): looking at the same books in the web interface
- [operations.md](operations.md): running it for real

The same flow is a test: `tests/test_generic_bootstrap.py` drives a new entity through these steps with only
the published commands.
