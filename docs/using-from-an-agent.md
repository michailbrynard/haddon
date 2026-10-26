# Using Haddon from an agent

The short version of the contract an agent works under: how to find out what it can do, how to write safely,
how mistakes get fixed, and how to read a refusal. It is written for the agent's instructions as much as for
the person writing them. [agent-guide.md](agent-guide.md) has the longer story, with tool definitions, the
monthly workflows and the guardrails we recommend.

The examples use the books from the [quickstart](quickstart.md): one entity, `acme`, and a token whose actor is
`agent:bookkeeper`. Responses are trimmed but otherwise as the service returns them.

## Start by asking the service

Don't hard-code what an installation offers. Two reads tell an agent where it is.

`GET /v1/entities` lists the books this credential can reach, with each one's currencies and period lock:

```json
{
  "items": [
    {
      "entity_id": "acme",
      "legal_name": "Acme Ltd",
      "base_currency": "USD",
      "commodities": ["USD"],
      "lock_through": "2026-04-30",
      "lock_version": 2,
      "lock_close_id": "close-6a0dc69eab9749188032cd71b94491d2",
      "links": {
        "self": "/v1/entities/acme",
        "capabilities": "/v1/entities/acme/capabilities",
        "events": "/v1/entities/acme/events"
      }
    }
  ],
  "next_cursor": null
}
```

An entity the token isn't scoped to is not in the list, and a request for it is `403 forbidden`.

`GET /v1/entities/{id}/capabilities` describes that entity's surface: every command this process serves (name,
method and path), every read, the actor your writes will be recorded under, the contracts for amounts, dates,
idempotency and pagination, the page limits, and which commodities each account accepts.

```json
{
  "actor_recorded_as": "agent:bookkeeper",
  "commands": [
    {"name": "post_entry", "method": "POST", "path": "/v1/entities/acme/entries"},
    "..."
  ],
  "limits": {
    "default_limit": 50, "max_limit": 200,
    "event_default_limit": 100, "event_max_limit": 500,
    "max_entry_legs": 1000, "max_batch_entries": 1000
  },
  "idempotency": {
    "header": "Idempotency-Key",
    "required_on": ["POST"],
    "hash_binds": ["method", "path", "canonical body", "caller", "entity"],
    "replay_header": "Idempotency-Replayed",
    "conflict": "409 idempotency_conflict when the same key carries a different request"
  },
  "commodities": [{"code": "USD", "precision": 2, "quantum": 10000}]
}
```

The list is built from the routes the process actually mounted. If the reconciliation extension is switched
off, its commands are missing from it rather than listed and failing. Each command's `name` is also the
`operationId` of its route in `/openapi.json`, which is where the request and response schemas are. Fetch
`/accounts` as well: the chart is what an agent books against.

## Send an Idempotency-Key with every write

Every `POST` under `/v1` needs an `Idempotency-Key` header. Without one the request is refused before it
reaches a command (`422`, `code: validation`, `details[0].reason: idempotency_key_required`).

Use one key per intent and reuse it on retries. The tool call's id is a good key: a retried HTTP request
carries the same id, and a new decision by the model gets a new one.

- The same key with the same request returns the stored status and body, with `Idempotency-Replayed: true`.
  Nothing is written twice.
- The same key with a different request is `409 idempotency_conflict`. That is a harness bug: a new intent
  needs a new key.
- The comparison uses the method, the path, the body as canonical JSON, the caller and the entity. Re-encoding
  the same body with different key order or whitespace still replays.
- A refused write stores nothing, so once the request is fixed it can go out again under the same key.

## Entries are posted, then corrected

There is nothing to save as a draft and come back to. `post_entry` checks the entry and posts it in the same
transaction, and the reply has `"status": "posted"`. `post_batch` does the same for many entries at once, all
or nothing. The schema does have a `draft` status, used inside a write while it is being checked, and one of
the close checks (`nothing_in_draft`) looks for any left in the period. No API command leaves one behind.

A posted entry is never edited. To change one, post a correction:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/entities/acme/entries/$ENTRY/correct \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: fix-0042" \
  -H 'Content-Type: application/json' -d '{
    "reason": "the invoice was for 120.00, not 100.00",
    "replacement": {"dt": "2026-04-03", "narration": "Invoice 0042", "legs": [
      {"account": "assets:receivables", "commodity": "USD", "amount": "120.00"},
      {"account": "income:sales",       "commodity": "USD", "amount": "-120.00"}]}}'
```

In one transaction this posts a mirror of the original, which cancels it, and the replacement. The reply names
all three (`original_id`, `mirror_id`, `replacement_id`), and `GET /entries/{id}` on the original then shows
`"state": "corrected"` and the correction chain. `reverse_entry` posts the mirror alone, for an entry that
should not exist at all. For a statement line, `reexplain_line` replaces the old explanation and corrects
anything it booked.

An entry can be corrected once. Correcting the same original again is `409 immutable`, and `details` gives the
`replacement_id` to correct instead. The `reason` is kept verbatim, so it should say why the original was
wrong ("the invoice was for 120.00") rather than what the command does.

## A locked period says no, and that is the right answer

Each entity has a period lock (`lock_through` above). Closing a period moves the lock forward. After that, any
write dated on or before it is refused, and so is a correction or reversal of an entry dated there:

```json
{
  "error": {
    "code": "period_locked",
    "message": "2026-04-15 is on or before the period lock 2026-04-30; reopen the period through an earlier date before changing it",
    "retryable": false,
    "details": [
      {"path": "dt", "dt": "2026-04-15", "locked_through": "2026-04-30", "remedy": "reopen"}
    ],
    "request_id": "req-35d56fa66098441b8467827ba44045be"
  }
}
```

A closed period is one someone, a person or an agent, has closed, and its figures may already be in a report or a
tax return. If a late entry could slip into it, the books would stop matching what was reported, and nobody
would know. So the refusal is the system working. An agent that receives it should stop and report it rather
than look for a way around it. Whoever is responsible for the books can then post the change in an open period, or reopen the period with
`reopen_period`, which records who reopened it and why. Agents should not reopen periods on their own.

Two things still work in a closed period. Reads are unaffected, and `file_document` can attach evidence to an
entry dated there, because filing a document changes no figures.

`close_period` and `reopen_period` both take `expected_version`, the `lock_version` you last read. If the lock
has moved since, the request is `412 stale_version` with `"next_action": {"action": "reload", ...}`: read
`/close-checks` again and decide again.

## Read the refusal and fix the call

Every error has the same shape:

```json
{
  "error": {
    "code": "precision",
    "message": "legs[0].amount: '100.001' has 3 decimal places; USD supports 2",
    "retryable": false,
    "details": [
      {"path": "legs[0].amount", "reason": "unsupported_precision", "places": 3,
       "commodity": "USD", "precision": 2, "value": "100.001"}
    ],
    "request_id": "req-ac4bf1119ea24f4398b6af0147c7cfa7"
  }
}
```

- `code` is stable. Branch on it, not on `message`.
- `details` usually names the field (`path`) and why it failed (`reason`).
- `retryable` is true only when the same request may succeed later unchanged. In practice that is `503 busy`,
  when another writer held the database; retry it with backoff.
- `next_action` appears when the service knows the next step: `resync` with a `url` for an expired cursor or
  an event gap, and `reload` for a stale lock version.
- `request_id` is also in the `X-Request-Id` response header. Quote it when reporting a `500`.

Hand refusals back to the model as the tool result instead of raising them. Most models fix a bad call on the
next turn when they can read why it failed. The full list of codes is in
[agent-guide.md](agent-guide.md#reading-refusals).

## Page through lists with the cursor

Lists return `{items, next_cursor, snapshot_seq}`. Pass `next_cursor` back as `cursor`, unchanged, until it is
`null`. The default page is 50 items and the maximum 200. A larger `limit` is refused (`422`, "limit must be
between 1 and 200"), not quietly capped.

Cursors are opaque and signed, and they bind the entity, the filters, the ordering and the snapshot, so keep
the same filters while paging. A cursor whose snapshot has expired is `409 cursor_expired`, and its
`next_action` has the link to start again from.

The audit log is paged by sequence number instead: `GET /events?since=<seq>` returns `since`, `next_since` and
`high_water`, 100 events by default and at most 500. Keep the last `next_since` between sessions to see only
what changed.

## Amounts and dates

Send amounts as decimal strings in whole units (`"7316.67"`), never as JSON numbers, and with no more decimal
places than the commodity has. They come back as integer strings of millionths (`"7316670000"`). Debits are
positive and credits negative, and each entry's legs must sum to zero per commodity. Dates are `YYYY-MM-DD`;
ranges are an inclusive `from` and `to`, either of which may be left out. Unknown query parameters are ignored,
so check the name of a filter before relying on it.
