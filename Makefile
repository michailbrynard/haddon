PY := .venv/bin/python
PYTEST := .venv/bin/pytest
DB ?= var/dev.db

# The corpus the importer and the oracle verify against.  It is named here, once, rather than defaulted inside
# `bin/haddon-import` and `bin/haddon-oracle`: the loader refuses to run without HADDON_CORPUS on purpose
# (importing/corpus.py), because guessing which books are being verified is worse than refusing.  `?=` lets a
# caller name a different corpus without editing anything.
export HADDON_CORPUS ?= corpus/example.json

# The two test tiers.  These variables are their only definition: `tests/test_tier_completeness.py` reads them
# out of this file and proves, by collecting, that between them they still run every test in the suite.  A test
# cannot fall out of both tiers and silently stop being run.  Change them here and nowhere else.
FAST_SELECT := -m "not slow"
SLOW_SELECT := -m "slow"

.PHONY: install install-browser check test test-fast test-slow test-gate test-matrix test-oracle oracle oracle-book migrate inventory inventory-check lint-no-replace web web-install web-check web-dev site site-install site-dev browser-check backup-drill

install:
	.venv/bin/pip install -e '.[dev]'

# The reviewer bundle's type check and the full gate, as CI runs them.
check: test-gate web-check

# Only what the browser harness needs, and only when somebody is going to run it: Playwright plus its browser
# is a 100+ MB download, and the commit gate deliberately does not depend on either (browser/README.md).
install-browser:
	.venv/bin/pip install -e '.[browser]'
	.venv/bin/playwright install chromium

# The reviewer interface.  The bundle is built straight into the Python package, so `bin/haddon-serve` serves it with no copy step and
# there is one process in front of the reviewer.  It is gitignored: run this after a checkout and after a pull
# that touched `web/`.  Without it the service still serves `/v1` normally and the page says so.
web: web-install
	cd web && npm run build

web-install:
	cd web && npm ci --no-audit --no-fund

web-check: web-install
	cd web && npm run typecheck

# Iterate on the interface against a `haddon-serve` already running on :8080.  The dev server supplies the
# same proxy header nginx supplies in production, so both authenticate the same way.
web-dev:
	cd web && npm run dev

# The documentation site and homepage (site/README.md).  The pages come from docs/ and the README, and the API
# reference from the OpenAPI document this checkout's code produces, so it needs `make install` first.  The build
# fails on a broken link.
SITE_ENV := ASTRO_TELEMETRY_DISABLED=1 HADDON_PYTHON=$(abspath $(PY))

site: site-install
	cd site && $(SITE_ENV) npm run build

site-install:
	cd site && npm ci --no-audit --no-fund

# Serves the site on :4321 and reloads on edits under site/.  After editing docs/, restart it to pick them up.
site-dev: site-install
	cd site && $(SITE_ENV) npm run dev

# For iterating between commits: everything except the tests measured at 0.5 s or more.
# A green `test-fast` is not permission to commit.  Run `test-gate` for that.
test-fast:
	$(PYTEST) -q $(FAST_SELECT)

# The other tier on its own.  Useful only while working on the slow tests themselves; `test-gate` covers it.
test-slow:
	$(PYTEST) -q $(SLOW_SELECT)

# The commit gate: the complete suite, with no marker selection, and then a full oracle run.  Both must exit 0
# before anything is committed.  It takes about ten minutes, on purpose.
test-gate:
	$(PYTEST) -q
	bin/haddon-oracle all

# `make test` is deliberately the gate and not the fast tier: the habitual command must never under-test.
test: test-gate

test-matrix:
	$(PYTEST) -q tests/test_integrity_mutation_matrix.py

# The oracle: pinned source -> store -> reparsed export.  Both forms exit nonzero on any disagreement.
# `test-oracle` runs it inside the suite, `oracle` as a standalone command against the real checkout.
test-oracle:
	$(PYTEST) -q tests/test_oracle.py

oracle:
	bin/haddon-oracle all

oracle-book:
	bin/haddon-oracle book

# The reviewer workflow in a real browser.  Builds the bundle first, every time: a run against a stale bundle
# reports green without having tested the change, which is worse than no test at all.  The harness refuses such
# a run as well (browser/server.py), but the habitual command must not be able to get there.
browser-check: web
	bin/haddon-browser-check

migrate:
	mkdir -p $(dir $(DB))
	$(PY) -m haddon.db.migrate $(DB)

# The recovery drill.  Back DB up, verify the backup read-only, restore it, and compare the restored database
# against this one, row by row and by the journal it exports.  Exits nonzero on any disagreement, so it works as
# a cron check and a release gate.  It never writes to DB.
backup-drill:
	bin/haddon-backup drill $(DB)

# The generated inventory: the routes, schema objects, migrations and modules table in docs/architecture.md,
# taken from the code rather than counted by hand.  `inventory` rewrites it; `inventory-check` says whether it is
# stale.  The gate needs neither target, because `tests/test_inventory_is_generated.py` runs inside the suite and
# fails `make test` on a stale document.  These are for the person who then has to fix it.
inventory:
	$(PY) -m haddon.inventory --write

inventory-check:
	$(PY) -m haddon.inventory --check

lint-no-replace:
	$(PYTEST) -q tests/test_no_replace_in_src.py
