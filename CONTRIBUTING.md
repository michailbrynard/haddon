# Contributing to Haddon

Thanks for taking an interest. Bug reports, questions, documentation fixes and code are all welcome.

For anything bigger than a small fix, please open an issue first so we can agree on the approach before you
spend time on it. Haddon's design leans hard on a few rules (listed below), and a change that crosses one is
much easier to reshape early than to review late.

## Development setup

You need Python 3.12 or later, and Node.js 20.19 or later (the minimum for Vite 7) if you are working on the
reviewer UI.

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
make install                 # editable install with the test dependencies

# hledger 1.52.1 for the import, export and oracle tests (Linux x86_64 shown; see docs/hledger.md)
curl -sSLf https://github.com/hledgerorg/hledger/releases/download/1.52.1/hledger-linux-x64.tar.gz \
  | tar -xz -C vendor hledger

cd web && npm ci             # only for UI work
```

The PDF statements use WeasyPrint, which needs Pango from your system package manager (on Debian and Ubuntu,
`libpango-1.0-0` and `libpangoft2-1.0-0`; on macOS, `brew install pango`).

## Tests

| Command | What it runs | When |
|---|---|---|
| `make test-fast` | every test not marked `slow` | while iterating |
| `make test-gate` | the whole suite, then the oracle over the sample corpus | before you open or update a pull request |
| `make web-check` | `npm ci` and the TypeScript type check | when you touch `web/` |
| `make browser-check` | Playwright through the reviewer's main paths (needs `make install-browser`) | when you change how the UI reads or writes |

`make test-gate` must pass. A green `test-fast` isn't enough, because it skips the corpus imports and exports,
most of the HTTP surface and everything that runs hledger. CI runs `make test-gate` and `make web-check`.

If a test takes 0.5 seconds or more, mark it `@pytest.mark.slow` (or set `pytestmark` for the module) so it
lands in the slow tier. `tests/test_tier_completeness.py` checks that the two tiers together still run every
test.

## Rules a change has to keep

1. The journal core does not import extensions. Reconciliation, review, artifacts and local tax records may
   import the core, never the other way round. `tests/test_core_boundaries.py` enforces this, and
   [docs/architecture.md](docs/architecture.md) explains the split.
2. Money is integer micro-units. No float may reach the database or the wire. Amounts come in as decimal
   strings and go out as integer strings.
3. Posted facts are append-only. You fix things by adding a record that supersedes, mirrors or corrects, never
   by updating or deleting a posted one. If you find yourself writing an `UPDATE` against a posted row, stop
   and open an issue.
4. Integrity lives in the schema too. A new invariant gets a CHECK or trigger wherever SQLite can express it,
   plus a row in `tests/test_integrity_mutation_matrix.py` showing that the database refuses the bad write on
   its own.
5. Every write is a named command. A new write needs a function, a route whose `operation_id` is that
   function's name, a strict request schema, an audit action with a reason, and an `Idempotency-Key`. It will
   then show up in `/capabilities` and the OpenAPI document without further work.
6. Errors say what to fix. Refuse with a stable code from `haddon.domain.errors` and `details` that name the
   field. An LLM agent will read the error and try again, so give it what it needs to do that.
7. The schema changes by migration. The SQL files under `src/haddon/db/migrations/` are SHA-256-pinned, so a
   schema change is a new Alembic revision, not an edit to an old one.
8. Generated documentation stays generated. If you add a route, table or module, run `make inventory` to update
   the counts in `docs/architecture.md`. The suite fails while they are stale.
9. No real books in the repository. Fixtures are invented: Fernside Instruments in the tests and the `Example`
   companies in `corpus/example`. The corpus is written by `corpus/generate_example.py`, so change the
   generator rather than the journals, and only update `corpus/example-baselines.json` when you mean to change
   the source. `python3 corpus/generate_example.py --check` must pass.
10. Record decisions that change accounting meaning. Add a section to the end of
    [docs/design/decisions.md](docs/design/decisions.md) saying what changed and why, and update the
    user-facing docs in `docs/`.

## Style

Python follows the surrounding code: type hints, dataclasses for value types, SQLAlchemy Core rather than the
ORM, and comments that explain why rather than what. The web UI is TypeScript with React, TanStack Query and
Tailwind; `make web-check` is the bar.

Write commit messages in the imperative. Say what the change does, and why if that isn't obvious. Keep each
pull request to one concern, and fill in the template, including how you tested it.

## Reporting bugs

Open an issue saying what you did, what you expected and what happened instead. Include the `request_id` from
any error envelope, and the output of `bin/haddon-serve --check` if the service is involved. Please don't attach
real books; reduce the problem to invented data first. Security issues go through [SECURITY.md](SECURITY.md),
not the public tracker.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
