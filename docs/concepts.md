# Concepts and the accounting model

What Haddon stores, which rules it enforces, and what its accounting terms mean. You don't need a bookkeeping
background to follow it.

## Books, entities and files

An **entity** is one legal entity's set of books: a company, a subsidiary, a sole trader. Each entity lives in
its own SQLite file. Every API path below `/v1/entities/{entity_id}` is answered from that one file, so books
for different entities cannot mix, and backing up or moving one entity is one file.

An entity has one **continuous journal**. There is no "2025 book" and "2026 book". Entries carry dates, and
every report takes a date range, so a fiscal year is just a range you ask for, not a way of storing data.

## Accounts and the chart

The **chart of accounts** is the list of places money can be recorded. Accounts are addressed by a
colon-separated path, such as `expenses:professional-services:audit`, and each has a **type**:

| Type | What it records | Normal sign of its balance |
|---|---|---|
| asset | what the entity owns or is owed (cash, receivables, equipment) | positive |
| liability | what it owes (payables, loans, tax due) | negative |
| equity | the owners' stake, including results kept from earlier periods | negative |
| income | what it earned in a period | negative |
| expense | what it spent in a period | positive |

Haddon uses the signed convention that hledger and most ledger tools use: a debit is positive and a credit is
negative. An income account therefore carries a negative balance, and a profitable period has a negative
result before it is presented. The shaped statements (`/reports/statements`, and the PDF) present figures with
conventional signs.

The chart is written down, never inferred. Creating an account requires its type and the currencies it may
hold, and the type is not guessed from the path. An account marked `is_cash` is one a bank statement is reconciled
against, and the close checks expect statements for it. Accounts are archived rather than deleted: an archived
account keeps its history in every report and refuses new postings. `restore_account` undoes an archive.

New entities start with `equity:retained-earnings`, where opening balances and results from earlier years
land, and optionally a small starter chart.

## Commodities and amounts

A **commodity** is a currency or other unit, declared per entity with its number of decimal places (USD 2,
JPY 0). Amounts are stored as signed 64-bit integers of **micro-units** (millionths of one unit), so there is no
floating-point arithmetic anywhere between the API and the database.

- Going in, an amount is an exact decimal string in whole units: `"-12.00"`. It is converted using the
  commodity's declared precision. More decimal places than the commodity has (`"12.001"` for USD) is refused,
  not rounded. A JSON number is refused.
- Coming out, an amount is a base-10 integer string of micro-units: `"-12000000"`.
- Every stored amount must be a whole multiple of the commodity's smallest unit (its quantum). The database
  checks this too.

Each string has exactly one accepted spelling: no leading `+`, no leading zeros, no `-0`, no whitespace.
`GET /capabilities` publishes the full contract under `amount_contract`.

## Entries and postings

An **entry** (a journal entry, or transaction) is one dated event: an invoice raised, rent paid, a salary
accrued. It has a narration (how it reads in reports) and two or more **postings**, also called legs. Each
posting puts an amount of one commodity into one account.

**Double entry** means the postings of an entry sum to zero for each commodity. Receivables up 100, sales up
100 (written as -100): the entry balances. An entry may touch several commodities as long as each one nets to
zero on its own; Haddon does not translate between currencies.

Each entry also records an **origin** (`manual`, `invoice`, `payroll`, `opening`, `reversal` and so on), who
created it, and optionally an hledger transaction code, comment and tags, so that imported journals round-trip.

### Status

Every write through the API produces a **posted** entry, which appears in the reports immediately. The schema also has
a `draft` status, used inside a single transaction while an entry is being assembled and checked, and a `void`
status for a draft that was abandoned. The `nothing_in_draft` close check refuses a period that still holds a
draft.

### Corrections and reversals

A posted entry cannot be edited or deleted; triggers in the database refuse it, whoever the caller is. There
are two ways to change what the books say:

- **Reverse**: post the entry's exact **mirror** (every leg with its sign flipped), so the two cancel. Used for
  a booking that should never have been made.
- **Correct**: post the mirror and a **replacement** together, in one transaction, linked by a correction
  record that holds the reason. There is never a moment when the original is withdrawn and nothing replaces it.

The original stays in the journal. `GET /entries/{id}` reports its derived state (`corrected`, `reversed`) and
the chain of corrections, so the history of what was believed and when is always readable. An entry that has
already been corrected cannot be corrected again; correct its replacement instead.

### Batches

`post_batch` posts many entries as one unit, for example a year converted from another system. The caller
states how many entries the batch holds and, per commodity, the sum of its debits and credits as counted from
its own source. If the batch does not match those figures, or any entry fails validation, nothing is posted.

## The period lock and closing

Each entity has one **period lock**: a date on or before which nothing that changes the books may be written.
Posting, correcting, reversing and explaining statement lines dated inside the locked period are all refused
with `409 period_locked`.

**Closing** a period moves the lock forward. Before it moves, the service runs the **close checks** over every
month between the previous lock and the new one:

| Check | Passes when | Provided by |
|---|---|---|
| `nothing_in_draft` | no entry dated in the window is still a draft | journal core |
| `statement_coverage` | every cash account has a statement for every day of the window, and each statement's closing balance agrees with the ledger | reconciliation extension |
| `lines_explained` | every statement line in the window is booked, linked or ignored | reconciliation extension |
| `nothing_partial` | no line or posting in the window is only partly allocated | reconciliation extension |

The checks are **fail-closed**: a check passes only when it has positively established its condition. A check
that cannot tell (for example, `lines_explained` when no statement was loaded) reports `unevaluable`, and that
blocks the close just as a failure does.

A blocking check can be closed over with an **override** that names the check and gives a reason. The override
is stored on the close record with the check's status and outstanding items as they stood. An override for a
check that is not blocking is refused, so a stored list of overrides cannot be replayed blindly.

Every close records a **snapshot**: a hash of the journal's state at the lock date, computed inside the same
transaction that moves the lock. A **reopen** moves the lock back to an earlier date, with a reason. It is not
gated on the checks, and it does not delete the close record; both are kept.

`close_period` and `reopen_period` both require `expected_version`, the lock version the caller last saw. If the
lock moved in the meantime the command fails with `412 stale_version`.

## Statements and reconciliation

Reconciliation is an optional extension (on by default; `HADDON_RECONCILIATION=off` removes it). It connects
the books to bank statements.

Haddon has no bank-format parsers, and we don't plan to add any. An agent reads the bank's file outside the
ledger and submits a **statement**, which carries the account and commodity, the period, the opening and
closing balances, the lines, and a reference to the source **document** (its SHA-256, filename, media type,
size, and either a storage path or an external reference). The ledger then enforces the things a parser can't
be trusted with:

1. the lines must sum to closing minus opening;
2. every line must fall inside the statement's period;
3. the currency is declared on the statement and on every line, and they must agree;
4. the same document (by hash) cannot be loaded twice, and two statements for one account may not overlap.

A submission that breaks any rule is refused whole. A statement's opening balance is checked against the
previous statement's closing balance when there is one; when there is not, the statement says so
(`opening_verified: false`) instead of assuming zero.

A statement loaded wrongly is **replaced**, not edited: the replacement points at its predecessor, which stays
readable, and the predecessor's lines become `superseded_input`.

### Explaining a line

Every statement line has exactly one current **explanation**:

| Status | Meaning | Command |
|---|---|---|
| `unexplained` | nothing has been said about it yet | (initial state) |
| `booked` | the line is a new entry; the cash leg comes from the line | `book_line` |
| `linked` | the line is money already in the books; it points at existing postings, and several parts make a split | `link_line` |
| `partly_explained` | a link covers less than the line's amount | `link_line` |
| `ignored` | it will not be booked, and the reason says why | `ignore_line` |
| `superseded_input` | its statement was replaced | `replace_statement` |

Explanations are append-only. `reexplain_line` writes a new explanation that names the one it supersedes, and if
the old one had booked an entry, the books are corrected through the normal correction path in the same
transaction.

An **allocation** (the link between a line and a posting) never creates money: linking posts nothing and no
balance changes. The database refuses an allocation that disagrees with the line or the posting it points at,
and one that would explain either for more than its amount.

`GET /reconciliation` answers two separate questions for each cash account: does the bank's balance agree with
the books (`agrees`), and is every line explained (`explained`)? A statement can balance perfectly with every
line unexplained, so they are reported side by side.

## Documents and provenance

A **document** is any file the books rest on: a statement, an invoice, a receipt, a contract. Haddon stores a
reference to it, not the bytes. Identity is the SHA-256, so registering the same file twice returns the existing
record. A document can be **filed** against an entry with a page, a locator ("the total box") and a caption.

`GET /provenance` answers "where did this come from?" from either end: from a statement line to its document,
or from an entry or posting to the lines and documents behind it. If the chain is inconsistent it refuses
rather than guessing. When `blob_root` is configured, `GET /documents/{id}/content` serves the bytes, checked
against the recorded hash.

Filing a document does not change any figure, so it is allowed in a closed period.

## Provisional bookings

Sometimes a cost is real but the document that settles it has not arrived: a receipt now, the full invoice
later, and the invoice may change how it is booked. `await_document` marks a posted entry as **awaiting** a
named document, with a reason. `GET /provisional` lists everything awaiting.

The record clears by doing the work: filing a document against the entry marks the expectation `met` and
links the filing. If no document is coming, `withdraw_expectation` records that, with a reason. These records
form an append-only chain beside the entry; the entry itself is not modified.

## Recoverable tax not recovered

When a cost is booked including a tax that could in principle have been reclaimed (for example, because the
receipt does not support a claim), the amount disappears into the cost or an asset. `record_unrecovered_tax`
records the amount, the posting it sits in, the entity's own tax code, the last date it could still be
reclaimed, and why it was not. `GET /unrecovered-tax` lists them with totals per commodity. Each record ends as
`recovered` (naming the entry that recovered it) or `forgone` (with a reason).

Haddon knows nothing about any country's tax rules. The caller supplies the dates and codes, and nothing here
posts an entry.

## Audit, idempotency and actors

Every write runs in one SQLite `BEGIN IMMEDIATE` transaction and writes exactly one **audit event** alongside its
data: the command name, the subject, the actor, the time, the reason, a hash of the canonical input and the
result. If any part of the write fails, the whole transaction rolls back, event included. `GET /events` is the
log for an entity; `GET /entries/{id}/events` is one entry's history.

A caller's **actor** is an opaque string from its credential (`agent:bookkeeper`, `ui:alice`). It is recorded
verbatim and never parsed. No code path treats agents and people differently; the record shows who did what,
and there is no approval workflow.

**Idempotency** is part of the transaction: the stored response for an `Idempotency-Key` commits in the same
transaction as the change it describes, so a retry either finds the finished result or finds nothing.

## Reports

| Report | Path | Notes |
|---|---|---|
| Trial balance | `/reports/trial-balance?as_of=` | every account's balance; totals per commodity are zero |
| Balance sheet | `/reports/balance-sheet?as_of=` | includes the result to date from the start of the journal |
| Income statement | `/reports/income-statement?from=&to=` | movements in the range |
| Statements | `/reports/statements`, `/reports/statements.pdf` | the two statements arranged for presentation, with sections, subtotals and a balance check; the PDF is drawn from the same data |
| Composition | `/reports/composition` | how the journal was built: entry counts by origin and by source journal |
| Register | `/accounts/{id}/register?from=&to=` | an account's postings with a running balance |

All reports are per commodity. Haddon does not convert currencies.

## How this compares with plain-text accounting

Haddon reads and writes [hledger](https://hledger.org) journals, and borrows its sign convention and account
paths. The difference is where the rules live. A plain-text journal accepts any edit and checks it when you run
the tool. Haddon refuses an invalid write when it is attempted, keeps every change as an append-only fact with
an author and a reason, and serves the books over an API with concurrency control. Export to hledger whenever
you want the plain-text view or hledger's reports.
