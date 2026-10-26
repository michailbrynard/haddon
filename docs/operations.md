# Operating a Haddon deployment

What to run, when, and how to read the result. Every command here exits non-zero when something is wrong, so
all of it works from cron or a release gate. Apart from the deployment section, which describes example
configuration files for you to adapt, everything on this page is something `bin/` can run and `tests/`
checks.

## Installing

From a clone, into a virtual environment in the clone (the `bin/` scripts run `.venv/bin/python`):

```
python3 -m venv .venv && .venv/bin/pip install --upgrade pip && make install
make web                  # build the reviewer bundle (needs Node.js)
```

Put hledger 1.52.1 at `vendor/hledger` if the deployment will export journals, over the API or with
`bin/haddon-export`; see [hledger.md](hledger.md#the-pinned-binary). Without it `/readyz` answers `503` with
the `verifier` check failing (so do not use `/readyz` as a load-balancer health check on such a deployment;
`/healthz` is liveness only), and exports are refused. Everything else works. WeasyPrint, used for the PDF
statements, needs Pango from the system package manager.

## Configuration

`bin/haddon-serve` reads one JSON file, named by `--config` or `$HADDON_CONFIG`. Relative paths in it resolve
against the directory the file is in. A minimal one:

```json
{
  "entities": {"acme-holdings": "acme.db"},
  "tokens": {"<a long random bearer token>": {"actor": "agent:bookkeeper", "entities": ["acme-holdings"]}},
  "cursor_keys": [{"id": "2026-09", "secret": "<random hex>"}]
}
```

| Key | What it is | Unset |
|---|---|---|
| `entities` | entity id → database file. One file is one entity, and the id must match the one stored in the file. | required |
| `tokens` | bearer token → `{actor, entities}`. The actor is recorded verbatim on everything the token writes; `entities` is the scope. | no token can reach `/v1` |
| `cursor_keys` | the pagination-cursor keyring, newest first: the first key signs, every key verifies. `cursor_secret` is the one-key spelling. | the service invents a key per process, so cursors break at every restart, and `/readyz` reports `not_ready` |
| `proxy_auth` | `{header, entities, prefix?}`: lets a reverse proxy that has already authenticated a person pass who it was in `header`, recorded as `ui:<user>`. Configure it **only** when nothing can reach the service without passing through that proxy. | the reviewer UI's API entrance (`/ui/v1`) has no credential |
| `entity_root` | where `POST /v1/entities` creates new database files | `422 entity_root_unset` |
| `blob_root` | the only directory `GET /documents/{id}/content` reads document bytes from | no document content is served |
| `export_root` | where `POST /exports` writes journal files, one subdirectory per entity | `422 export_root_unset` |
| `default_limit`, `max_limit`, `event_default_limit`, `event_max_limit` | page sizes | the published defaults |

The configuration holds secrets (the tokens and the cursor keys). Keep it out of version control and readable
only by the service's user.

Three process settings are environment variables rather than configuration:

| Variable | Meaning |
|---|---|
| `HADDON_CONFIG` | the configuration file, when `--config` is not given |
| `HADDON_RECONCILIATION` | `off` switches the reconciliation extension off: its routes are not mounted, `/capabilities` does not advertise them, and its close checks are not asked |
| `HADDON_CORPUS` | the corpus manifest the importer and the oracle verify against (not needed to serve) |

`bin/haddon-serve --check --config <file>` opens every configured entity, prints the readiness report and exits
0 when ready, 1 when not, without binding a port.

## Adding a set of books

One database file is one entity, so a new set of books means a new file that is migrated, provisioned and
named in the service's configuration. You can do that over the API or on the machine itself.

### Over the API (an agent, or anyone with a token)

```
curl -X POST https://<host>/v1/entities \
  -H 'Authorization: Bearer <token>' -H 'Idempotency-Key: '"$(uuidgen)" \
  -H 'Content-Type: application/json' -d '{
        "entity_id": "acme-holdings", "legal_name": "Acme Holdings Ltd", "legal_form": "Ltd",
        "country": "GB", "base_currency": "GBP", "base_precision": 2}'
```

This is `initialize_entity`, the same command `bin/haddon-init` runs. It creates the file under `entity_root`,
migrates it to head, provisions the entity and writes the new mapping back into the configuration file the
service was started from, so the book survives a restart, and then serves it immediately. The reply is the
same `EntitySummary` that `GET /v1/entities` lists.

Two things in the configuration decide whether this is available at all. Without them the request is refused
rather than half-done:

| Key | Without it |
|---|---|
| `entity_root` | `422 entity_root_unset`: there is nowhere to put a database file. Leave it unset on a deployment whose entity map is managed by hand. |
| a configuration *file* (`--config` / `$HADDON_CONFIG`) | `422 config_not_durable`: a service built from an in-process config has nowhere durable to register the entity. |

The entity id is both the book's permanent address and the name of its file, so it has to be a plain slug of
1–63 lower-case letters, digits and hyphens. Anything else is refused before a file is created.
Creating an id that already exists is `409`, not an update; retrying the *same* creation with the same
`Idempotency-Key` replays the original `201`.

`starter_chart` defaults to false, since a book you are about to import or author brings its own chart. The
retained-earnings equity account is created either way, so the book can always take an opening balance.

### On the machine (the bootstrap)

```
bin/haddon-init var/acme.db --id acme-holdings --name "Acme Holdings Ltd" \
  --legal-form Ltd --country GB --base-currency GBP --base-precision 2
```

Then add `"acme-holdings": "acme.db"` to the configuration's `entities` map and restart. This is the path for
the *first* book on a deployment. The configuration's `entities` map may not be empty, so a service has to have
one before it can be asked for another.

## Exporting the journal

Exports write the books out as hledger journal files. The bytes are deterministic, the pinned `hledger` binary
checks them before anything is published, and an immutable `exports` row records each file and its `sha256`.
You can run an export over the API or on the machine; both use the same function.

### Over the API (an agent, or the reviewer interface)

```
curl -X POST https://<host>/v1/entities/acme-holdings/exports \
  -H 'Authorization: Bearer <token>' -H 'Idempotency-Key: '"$(uuidgen)" \
  -H 'Content-Type: application/json' -d '{"year": 2025}'
```

The body is the range and nothing else: `{}` exports the whole journal, `{"year": 2025}` is the calendar-year
shorthand, and `{"from": "2025-01-01", "to": "2025-06-30"}` is any inclusive range with either side open.
`{"overwrite": false}` asks to be refused rather than replacing an export already published for that range;
the default is to replace it.

The reply names the `exports` row, the snapshot, the `state_hash`, the verifier's verdict and one `href` per
file. Fetch a file with an ordinary GET:

```
curl -O -J https://<host>/v1/entities/acme-holdings/exports/<export_id>/artifacts/2025.journal \
  -H 'Authorization: Bearer <token>'
```

| Key | Without it |
|---|---|
| `export_root` | `422 export_root_unset`: there is nowhere to write the files, and the route refuses rather than choosing a directory. Leave it unset on a deployment that exports only on the machine. |

Files land in `export_root/<entity_id>/<Entity>/<label>/ledger/`: one directory per entity, which is the
confinement, and inside it the directory the book's own import provenance names. The label is the year when
the range is a whole calendar year and the two dates otherwise.

Two things to know before you wire this into anything. Re-exporting an unchanged book doesn't create a new
state: `snapshots.state_hash` is unique, so the second export reuses the first one's snapshot and writes
byte-identical files, and all you get is a second `exports` row pointing at the same snapshot. And a retry
replays. The reply is recorded in the same transaction as the export, so the same `Idempotency-Key` returns the
original `201` instead of rendering, verifying and publishing again.

Haddon owns the export directory outright. The only names it ever writes, replaces or deletes there are
`<label>.journal`, `accounts.journal` and `<label>-<group>.journal`, and if it finds anything else the whole
export is refused rather than the stray file being cleaned up.

### On the machine

```
bin/haddon-export var/acme.db /srv/exports --year 2025 --overwrite --manifest /tmp/manifest.json
```

This is the same export, with the out-root as an argument and the manifest written outside the managed
directory. Files published this way are recorded in the entity's `exports` table and read back by
`GET /exports/{id}` like any other, but their bytes are not served unless the out-root was exactly
`export_root/<entity_id>`: the download reads from there and nowhere else. Otherwise it answers
`evidence_unavailable` (`not_served` when no root is configured, `missing` when the files are not under it)
rather than reading an arbitrary path off the machine.

## Deploying behind a reverse proxy

`deploy/` holds examples, not a supported installer. Copy them, then replace the placeholders (the
hostname `ledger.example.com`, the install directory `/srv/haddon` and the service user) with your own.

- `deploy/haddon.service.example` is a systemd unit that runs
  `bin/haddon-serve --config <file> --host 127.0.0.1 --port 8080` from the install directory, restarts it on
  failure, and keeps it bound to loopback so the proxy is the only way in.
- `deploy/haddon.nginx.conf.example` is an nginx server block for one hostname, with three locations in front
  of the one service:
  - `/` serves the reviewer UI (the built bundle, its client-side routes, `/docs`, `/openapi.json`, `/healthz`
    and `/readyz`) and requires the proxy's own authentication, so a route added to the UI later is protected by
    default;
  - `/ui/v1/` is the same API for the browser: the proxy authenticates the person, passes who it was in the
    header `proxy_auth` names, and strips the `/ui` prefix, so the service sees an ordinary `/v1` request;
  - `/v1/` is the agents' entrance, authenticated by bearer token. The proxy blanks the user header here,
    because this entrance takes no password and a caller must never be able to name itself.

How the proxy authenticates people is your choice: HTTP basic auth, an identity-aware proxy, single sign-on.
Whatever it is, `proxy_auth` is only safe while the service is reachable through the proxy alone: anything that
can reach the port directly can set the header. If the proxy forwards a signed identity assertion, verifying it in
the service is the stronger guarantee.

`make web` builds the reviewer bundle into `src/haddon/ui/static`, which is gitignored, so an update is:

```
git pull && make install && make web && sudo systemctl restart haddon
```

Take a backup before any update that includes a migration (below), and see "Before a deploy" at the end of this
page.

## Backup and restore

One entity is one SQLite file, so it is tempting to back it up with `cp`. Don't.

Every connection the service opens runs in WAL mode (`haddon.db.connection.apply_pragmas`), so at any moment
some committed transactions are in `<db>-wal` and haven't been written back into `<db>` yet. Copying the one
file loses them, and the copy still opens cleanly, passes `PRAGMA integrity_check` and shows a plausible set of
books. Nothing about it looks wrong, which is what makes it dangerous. `tests/test_backup_restore.py` does
exactly this and watches a committed row disappear.

### Taking one

```
bin/haddon-backup create var/fernside.db /var/backups/haddon --keep 30
```

`create` uses `VACUUM INTO`. It runs inside a read transaction, so writers aren't blocked and no checkpoint is
needed first. The result is a single self-contained file with no `-wal` or `-shm` beside it, so there is one
file to copy off the machine rather than three to keep together.

Backups are named `<db-stem>-<YYYYmmddTHHMMSSZ>.backup.db`. The timestamp sorts lexically in time order, so
retention can go by filename without opening anything. A backup never overwrites an earlier one; two in the
same second is an error. `--keep N` deletes all but the newest N afterwards. `--keep 0` is refused, on the
basis that keeping nothing is a typo rather than a policy.

### Checking one

```
bin/haddon-backup verify /var/backups/haddon/fernside-20260915T030000Z.backup.db
```

`verify` opens the backup read-only. It deliberately doesn't go through `haddon.db.connection`, which would
set `journal_mode=WAL` and so write to the file it is meant to be checking. It reports:

| Check | A failure means |
|---|---|
| `PRAGMA integrity_check` | the file is not a structurally sound database. Do not restore it. |
| `PRAGMA foreign_key_check` | it references rows that are not there, the shape of a copy taken mid-transaction. |
| Alembic revision | which schema this backup is. A backup older than the service's schema is a *migration*, not a recovery, and you want to know that before anything opens it. |
| Row digest | every row of every table, ordered and hashed. `integrity_check` says the file is not corrupt; only this says it holds the same books. |

A file SQLite can't read at all comes back as a failed report containing SQLite's own error, not as a
crash.

### Putting one back

```
bin/haddon-backup restore /var/backups/haddon/fernside-20260915T030000Z.backup.db var/fernside.db
```

The backup is verified first, and the restore refuses to go ahead if it doesn't pass, since a database that
fails `integrity_check` gives you a service that starts and then reports wrong numbers. The restore writes to
a new path and leaves the backup alone, so if you restored the wrong one you can simply do it again.

It won't replace an existing file without `--overwrite`, and when it does overwrite, it removes that path's
`-wal` and `-shm` first. Otherwise SQLite would read the stale write-ahead log and the restored books would
pick up a transaction that was never in the backup.

The restored file comes back in the default journal mode. The first guarded connection switches it to WAL,
which is a write, so archive the backup, not the restored file.

### The drill

```
make backup-drill DB=var/fernside.db          # or: bin/haddon-backup drill var/fernside.db
```

You don't know a backup works until you have restored it. The drill does the whole round trip on a copy and
compares the result with the live database in two ways. The row digest shows the restored database holds the
same rows. The journal `state_hash` shows it exports the same journal: the digest already catches a changed
amount, and this catches rows that survived while the books no longer render, which is what you actually need
back from a recovered ledger.

The drill never writes to the database it is testing, and it exits non-zero on any disagreement.

```
backup   /tmp/haddon-drill-x/backups/fernside-20260915T074015Z.backup.db
verify   ...: OK  integrity=ok foreign_key_violations=0 revision=0001 tables=24 bytes=458752 digest=c2b65ee3
restore  /tmp/haddon-drill-x/restored.db
rows     digest c2b65ee394e00557 matches the live database
journal  state_hash bab9574c2c439b2a renders identically
DRILL PASSED
```

Run it before the first persistent deployment, and on a schedule after that. `tests/test_backup_restore.py`
checks that the drill can fail as well as pass.

### Suggested schedule

| When | What |
|---|---|
| Hourly | `bin/haddon-backup create <db> <dir> --keep 48` |
| Nightly | `bin/haddon-backup create <db> <archive-dir> --keep 30`, copied off the machine |
| Weekly | `bin/haddon-backup drill <db>`, and alert on non-zero |
| Before any migration or restore | `bin/haddon-backup create` first, then `verify` the result |

## The reviewer workflow, in a browser

```
make browser-check
```

This drives the reviewer's main paths in headless Chromium against a real server and the built bundle. It
catches something no other test here can see: a write landing while the page still shows what it knew before.
The two main paths are these. One opens a month, reads what is unexplained, follows a line to its entry and the
document behind it, closes the period and writes again. The other finds a statement that can't be replaced and
the line holding it, gets refused for a document that is already loaded and follows that to its statement,
then replaces one and reads the chain from both ends. Other tests cover the review inbox, writing a manual entry and downloading an export. `browser/README.md`
explains why these paths were chosen.

It is not part of `make test-gate`, because it needs a built bundle and a downloaded browser and the gate has
to run on a fresh checkout. Install it with `make install-browser`.

## Before a deploy

```
bin/haddon-serve --check --config <config.json>   # opens every configured entity, prints readiness, exits
make test-gate                                    # the whole suite, then a full oracle run
make browser-check                                # the reviewer loop, in a browser
bin/haddon-backup drill <db>                      # the books can be recovered
```
