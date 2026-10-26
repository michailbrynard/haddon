# Corpus manifests

A **corpus** is a set of source journals a Haddon installation verifies itself against: where the checkout is,
which entities it holds, and which years the importer and the oracle read by default. It is a verification
fixture, not part of the product. `src/haddon` contains no checkout path, fixed year or entity list, and
`tests/test_generic_bootstrap.py` fails if one appears.

`example.json` is this repository's corpus. It describes three fictional companies whose books live in
`example/`, and the oracle's pinned figures for them are in `example-baselines.json`.

| Key | Entity | Books | What it is there to exercise |
|---|---|---|---|
| `ExampleInc` | Example Inc. (US, USD) | 2025, 2026 | scale (over a thousand transactions), many source files, posting comments, tags, an inherited account tag |
| `ExampleBV` | Example B.V. (NL, EUR) | 2025, 2026 | a small book whose 2026 holds only its opening restatement, and a chart that grows between years |
| `ExampleSL` | Example S.L. (ES, EUR) | 2024, 2025, 2026 | a company constituted mid-2024, a three-year chain, zero-posting year-end balance assertions |

Every name, amount and date in `example/` is invented. The journals are written by
[`generate_example.py`](generate_example.py), deterministically:

```bash
python corpus/generate_example.py            # rewrite corpus/example/
python corpus/generate_example.py --check    # exit 1 if the checked-in journals differ from what it writes
```

The baselines are measured from these journals by the oracle itself (`bin/haddon-oracle --json` reports the
measured fingerprint of every run). Only regenerate them on purpose, never to make a failing run pass. A
changed pin says the source changed, and every number that moves should be traceable to a change in the
generator.

Point the tooling at a corpus with `HADDON_CORPUS`; the Makefile sets it to this one:

```bash
HADDON_CORPUS=corpus/example.json bin/haddon-oracle all
```

Relative `checkout` and `read_only_roots` paths in a manifest resolve against the manifest's own directory.

## Verifying your own books

To run the importer and the oracle against a real set of journals, write a manifest of the same shape somewhere
outside this repository, with `checkout` pointing at a read-only copy of the journals, laid out as
`<Entity>/<year>/ledger/<year>.journal`. Measure its baselines, and set `HADDON_CORPUS` to it. The commit gate's
test suite is written against the sample corpus; `bin/haddon-oracle all` is what verifies any other.

Nothing in the journal itself reads a corpus. Initialising an entity, posting, correcting, reconciling and
closing need none; see [`../docs/quickstart.md`](../docs/quickstart.md).
