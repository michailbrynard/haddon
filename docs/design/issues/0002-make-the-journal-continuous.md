# 0002 — Make the journal continuous

**Kind:** domain simplification + schema replacement  
**Risk:** high  
**Effort:** L

## Problem

Calendar-year books are encoded as storage identity rather than reporting scope. `fiscal_year_start` is
constrained to `'01-01'`, several rows repeat and validate `book_year` from their dates, entry creation must
resolve a book, report scope rejects cross-year ranges, and carry-forward logic connects separate years.

Those rules make a normal continuous ledger appear to reset each January. They also force agents to know a
storage partition that has no accounting meaning and make non-calendar reporting unnecessarily difficult.

## Desired model

- An entity has one continuous journal.
- Entries and source records carry dates, not a caller-supplied book identity.
- Reports accept any valid `[start, end]` range and group by month, quarter, fiscal year or custom period at
  query time.
- The scalar period lock remains: dates at or before `locked_through` cannot be mutated by actions that would
  change the books.
- Opening balances are ordinary, explicitly tagged entries or an import boundary—not a separate annual
  assertion system.
- Per-commodity reporting remains; this issue does not add FX translation.

## Evidence in the current design

- `src/haddon/db/migrations/sql/0001/schema.sql` pins `fiscal_year_start` to `01-01` and embeds calendar-year
  checks in multiple records.
- `src/haddon/repository/entries.py` derives and requires the book from an entry date.
- `src/haddon/reports/scope.py` refuses ranges spanning years.
- `src/haddon/reports/carry.py` reconstructs continuity that a continuous journal would already possess.
- Date/year helpers and statement periods repeat the same calendar boundary.

## Implementation approach

1. Add continuous report tests for same-year and cross-year trial balance, income statement and balance sheet
   ranges, using source journals as the expected accounting truth.
2. Remove `book_year` foreign keys/checks from dated facts, remove annual `books` records and change commands,
   links and UI navigation to use dates/ranges only.
3. Define fiscal periods as named query/report metadata. A configurable fiscal-year start may influence
   presets, but never entry storage validity.
4. Replace annual carry rows with direct balance queries over continuous postings. Represent a source opening
   balance as an ordinary tagged entry with provenance where one is needed.
5. Rebuild development databases and oracle fixtures from their source journals. Once the target schema is
   stable, squash the development migration chain into a clean initial schema.

## Acceptance criteria

- Posting an entry requires a date but no book or year identifier.
- Reports and registers work across December/January without a special carry operation.
- Existing same-year report totals and per-commodity balance invariants are unchanged.
- The lock rejects changes on or before its date and permits later changes, including across year boundaries.
- The public API, UI and main documentation no longer teach annual books as a user concept.
- Representative source journals rebuild deterministically into the new schema and retain source provenance.

## Delivered, and what was deferred

**Delivered.** Entries and dated facts carry a date and no book identity; reports take any inclusive
`[start, end]` range with either bound open; balances are direct queries over continuous postings; migration
0008 drops `books`, `entities.fiscal_year_start` and `book_year`; `bin/haddon-export` takes a range rather than
a year. December-to-January posting and reporting needs no carry operation.

**Deferred: optional fiscal-period presets.** The deliverable list said "arbitrary report ranges and optional
fiscal-period presets". The ranges shipped; the presets did not, and this records that as a decision rather
than leaving it unstated.

A preset is a *name* that resolves to a range — `2026-Q2`, `FY2026`, "last month". Two things make it the wrong
thing to build now:

1. **A preset over calendar periods is sugar over a feature that already exists, on the wrong side of the
   wire.** `haddon.statements.period` already derives a range's granularity, name, heading and comparative
   *from its own bounds*, precisely so a caller cannot ask for a period its dates contradict. A `?period=2026-Q2`
   parameter would move that derivation to the caller and give the service a second way to be told what a
   period is. The UI resolves period names to bounds today (`web/src/lib/period.ts`), which is where a
   presentation concern belongs.
2. **A preset over a non-calendar fiscal year would ship with no book that uses one.** It needs a stored
   `fiscal_year_start` — the column migration 0008 deliberately dropped — plus a fiscal-aware `classify`,
   `preceding` and `comparative`, plus headings. Every entity in the corpus starts its year on 1 January, so
   the whole non-calendar path would be exercised only by synthetic fixtures. Pitfall 1 below warns against
   exactly this half-feature, and the balance-sheet and comparative wording it would touch is the part of the
   statement code a reviewer just confirmed is right.

**The trigger to build it:** an entity whose financial year does not start on 1 January. At that point the
column comes back on the entity (reporting metadata only, never entry-storage validity, per "Desired model"),
`haddon.statements.period` grows a fiscal-year unit, and presets resolve at the HTTP edge. Until then the range
API answers every question a preset would, and nothing in the product is blocked on it.

## Pitfalls

1. Do not merely start reading `fiscal_year_start` while year checks remain; that creates a misleading partial
   fiscal-year feature.
2. Balance-sheet queries need all prior postings, while income statements need period activity. Preserve that
   distinction when removing carry logic.
3. The replacement must remove `book_year` assumptions from statements and snapshots, not only entries.
4. This issue does not justify changing entity isolation, amount representation or audit history at the same
   time.
