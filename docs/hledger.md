# hledger import, export and the oracle

Haddon reads and writes [hledger](https://hledger.org) journals, the plain-text double-entry format. That lets
you bring existing books in and take them out again whenever you like, and anything hledger can report on, it
can report on from an export. It also gives us an independent check. hledger is a mature implementation of the
same accounting, so when Haddon agrees with it, that is good evidence the store and the reports are right.

You don't need any of this to run the ledger. Initialising books, posting, reconciling and closing don't use
hledger.

## The pinned binary

All three tools use hledger 1.52.1 at `vendor/hledger`, which is gitignored. Before every run the binary's
SHA-256 is checked against `vendor/hledger.sha256` (one line per platform) and its `--version` must report
1.52.1. A platform with no pin is refused.

```bash
# Linux x86_64
curl -sSLf https://github.com/hledgerorg/hledger/releases/download/1.52.1/hledger-linux-x64.tar.gz \
  | tar -xz -C vendor hledger
# macOS arm64
curl -sSLf https://github.com/hledgerorg/hledger/releases/download/1.52.1/hledger-mac-arm64.tar.gz \
  | tar -xz -C vendor hledger
```

Both pinned hashes are those of the official release assets. On another platform, build or download 1.52.1,
verify it yourself, and add a `<platform> <sha256>` line for it.

## Corpora and manifests

The importer and the oracle read a **corpus**: a directory of journals laid out as
`<Entity>/<year>/ledger/<year>.journal`, described by a JSON manifest. `HADDON_CORPUS` (or `--corpus`) names the
manifest; nothing defaults it outside the `Makefile`, so the tools never guess which books they are verifying.

This repository ships `corpus/example.json`, three fictional companies in `corpus/example/`:

| Key | Entity | Years | What it exercises |
|---|---|---|---|
| `ExampleInc` | Example Inc. (US, USD) | 2025, 2026 | scale (over a thousand transactions), many source files, posting comments and tags |
| `ExampleBV` | Example B.V. (NL, EUR) | 2025, 2026 | a small book whose second year is only its opening, and a chart that grows |
| `ExampleSL` | Example S.L. (ES, EUR) | 2024 to 2026 | a company formed mid-year, a three-year chain, zero-posting balance assertions |

The journals are written by `corpus/generate_example.py`, deterministically. `python3
corpus/generate_example.py --check` exits non-zero if the checked-in files differ from what it would write. The
oracle's expected figures are in `corpus/example-baselines.json`. See [corpus/README.md](../corpus/README.md) for
the manifest format and how to point the tools at your own books.

## Import

```bash
export HADDON_CORPUS=corpus/example.json
bin/haddon-import ExampleBV var/example-bv.db                              # the manifest's default year
bin/haddon-import ExampleBV var/example-bv.db --year 2026 --into-existing  # extend with the next year
```

The import creates and migrates the database if needed, provisions the entity from the manifest, and loads the
year's transactions with their codes, comments and tags. A journal that fails `hledger check --strict` is
refused (exit 3, nothing written); `--skip-blocked` records the refusal as an event instead (exit 4).

Corpora are usually kept as self-contained annual books, where each year opens by restating the previous year's
closing balance sheet. Haddon keeps one continuous journal, so when a later year is imported with
`--into-existing`, its opening transaction is **verified rather than posted**: it must equal the position the
journal already holds, apart from the year's result moving into retained earnings. Posting it would double every
balance-sheet account while still balancing. Anything else ends the import with `422 unbalanced`, naming the
account.

## Export

```bash
bin/haddon-export var/example-bv.db exports                           # the whole journal
bin/haddon-export var/example-bv.db exports --year 2025 --overwrite
bin/haddon-export var/example-bv.db exports --from 2025-04-01 --to 2025-06-30
```

The same export is available over the API as `POST /exports`; see [operations.md](operations.md#exporting-the-journal).

An export writes `<Entity>/<label>/ledger/` under the target: `<label>.journal` (which includes the rest),
`accounts.journal`, and one `<label>-<source>.journal` per source group. The label is the year when the range is
exactly one calendar year, and `<start>_<end>` otherwise. Only posted entries are exported; a reversed entry
appears beside its reversal.

Exports are deterministic: two exports of the same books are byte-identical, across processes and restarts.
Before anything is published the output must pass `hledger check --strict` with the pinned binary. The published
files are then re-hashed, and one transaction records an `exports` row, a snapshot of the journal's state hash,
and an `export.completed` event listing every file's SHA-256. A target directory that contains files the export
did not write is refused rather than cleaned.

## The oracle

```bash
make oracle                               # every entity, book and continuity checks
bin/haddon-oracle book ExampleBV          # one entity's default year
bin/haddon-oracle continuity ExampleSL    # every year of the chain, in one journal
bin/haddon-oracle book ExampleInc --json  # the full report as JSON
```

The book oracle imports one year into an empty database, exports it, re-parses the export with hledger, and
compares the source, the store and the export. Eleven gates must pass. They include per-account and per-month
totals, transaction and posting counts, the exact multiset of transactions with every leg in integer micros
(which catches a same-month date swap or a compensating pair of errors that totals would miss), round-tripped
codes, comments and tags, the mapping between source transactions and stored entries, the re-hashed artifacts,
and the figures pinned in the corpus baselines.

The continuity oracle loads an entity's whole chain of years into one journal, the way the importer does, and
checks every year boundary against hledger's closing position for each annual book. It is there to catch
double counting at year ends, which a single-year run can't show.

The exit code is `0` when every gate passed, `2` when a gate failed or did not run, and `1` when the command
could not run. Work happens in a temporary directory; `--into` publishes the verified database only when every
gate passed. The corpus and `vendor/` are never written.

`make test-gate` runs the full test suite and then `bin/haddon-oracle all` over the sample corpus.

## Verifying your own books

Write a manifest of the same shape outside the repository, pointing `checkout` at a read-only copy of your
journals, and write its baselines file: `--json` reports each run's measured `facts.fingerprint`, which holds the
fields `corpus/example-baselines.json` pins. Review the figures before saving them. Then:

```bash
HADDON_CORPUS=/path/to/your-corpus.json bin/haddon-oracle all
```

Regenerate baselines only when the source journals change on purpose; a changed baseline is a claim that the
books changed.
