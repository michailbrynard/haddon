"""Shared fixtures: one migrated+seeded template per lock state, restored into a fresh guarded connection per test.

The templates are built by the real Alembic migration (``haddon.db.migrate.upgrade``), so tests run against the
schema production gets.  Restoring through SQLite's backup API costs about a millisecond per test and keeps every
test isolated.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

# The corpus this suite verifies against.  ``src/haddon`` names no checkout, no year and no entity list
# (issue 0006): a corpus is a manifest outside the package and ``HADDON_CORPUS`` is how a process is told which
# one.  Set before anything imports the importer, and only if the environment has not already chosen one, so a
# run against a different corpus stays possible.
os.environ.setdefault("HADDON_CORPUS", str(Path(__file__).resolve().parents[1] / "corpus" / "example.json"))

from haddon.db.connection import sqlite_connect
from haddon.db.migrate import upgrade
# Importing the reconciliation extension registers its close checks with the core (``close_checks.policy``), as
# ``haddon.api.app`` does when it mounts the extension.  Without it a repository-level close refuses as
# not-installed.
from haddon.repository import close_coverage  # noqa: F401,E402

from seed import EXPECTED_COUNTS, EXPECTED_STATUS, lock, seed

TESTS_DIR = Path(__file__).parent


class FixtureSetupError(AssertionError):
    """A test's own setup failed: reported as SETUP-ERROR, never as a pass or a refusal."""


def build_template(path: Path, *, locked: bool) -> Path:
    upgrade(path)
    con = sqlite_connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        seed(con)
        if locked:
            lock(con)
        con.execute("COMMIT")
        # Self-check the seed so a broken fixture cannot pass as a green matrix.
        for table, n in EXPECTED_COUNTS.items():
            got = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if got != n:
                raise FixtureSetupError(f"seed self-check: {table} has {got} rows, expected {n}")
        for eid, status in EXPECTED_STATUS.items():
            got = con.execute("SELECT status FROM entries WHERE id=?", (eid,)).fetchone()[0]
            if got != status:
                raise FixtureSetupError(f"seed self-check: {eid} is {got}, expected {status}")
        through = con.execute("SELECT through FROM period_locks").fetchone()[0]
        if through != ("2026-02-28" if locked else None):
            raise FixtureSetupError(f"seed self-check: lock through={through!r} for locked={locked}")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise FixtureSetupError("seed self-check: foreign_key_check reported violations")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    return path


@pytest.fixture(scope="session")
def templates(tmp_path_factory) -> dict[bool, Path]:
    base = tmp_path_factory.mktemp("haddon-templates")
    return {
        False: build_template(base / "unlocked.db", locked=False),
        True: build_template(base / "locked.db", locked=True),
    }


def restore_into_memory(template: Path) -> sqlite3.Connection:
    """Fresh guarded in-memory connection holding a copy of the template database."""
    dst = sqlite_connect(":memory:")
    src = sqlite3.connect(template)
    try:
        src.backup(dst)
    finally:
        src.close()
    return dst


@pytest.fixture
def fresh_db(templates):
    """Factory: fresh_db(locked=False) -> guarded connection on a seeded copy."""
    opened: list[sqlite3.Connection] = []

    def _make(locked: bool = False) -> sqlite3.Connection:
        con = restore_into_memory(templates[locked])
        opened.append(con)
        return con

    yield _make
    for con in opened:
        con.close()


@pytest.fixture
def db(fresh_db):
    return fresh_db(locked=False)


@pytest.fixture
def locked_db(fresh_db):
    return fresh_db(locked=True)


# Chunk B: engine fixtures.
# The repository layer runs inside ``write_transaction(engine)``, which needs a file-backed database: an in-memory
# copy is private to one DBAPI connection and does not survive across the engine's pooled connections.  Templates
# are built once per session (migrated-empty, and migrated+provisioned through the real API) and copied per test.
# Copies live on tmpfs when /dev/shm is available.  Every write transaction is a WAL commit with synchronous=FULL,
# which on an ext4 root is an fsync (~30 ms) per transaction and on tmpfs is free.  The production pragmas are
# left as they are.

import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

from haddon.db.connection import make_engine  # noqa: E402

from repo_helpers import load_fixture, provision_small_entity  # noqa: E402

_FAST_DIR = Path("/dev/shm") if os.access("/dev/shm", os.W_OK) else None


def _checkpoint(path: Path) -> None:
    con = sqlite_connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory) -> Path:
    """A migrated, unprovisioned database file (no entity, no chart) produced by the real Alembic migration."""
    path = tmp_path_factory.mktemp("haddon-repo-template") / "migrated.db"
    upgrade(path)
    _checkpoint(path)
    return path


@pytest.fixture(scope="session")
def provisioned_template(migrated_template, tmp_path_factory) -> Path:
    """The migrated template plus ``repo_helpers.provision_small_entity`` (real provisioning API), once per session."""
    path = tmp_path_factory.mktemp("haddon-repo-template") / "provisioned.db"
    shutil.copy(migrated_template, path)
    eng = make_engine(path)
    try:
        provision_small_entity(eng)
    finally:
        eng.dispose()
    _checkpoint(path)
    return path


@pytest.fixture
def fast_dir(tmp_path) -> Path:
    """Per-test scratch directory for database files: tmpfs when available, else the ordinary tmp_path."""
    if _FAST_DIR is None:
        yield tmp_path
        return
    d = Path(tempfile.mkdtemp(prefix="haddon-test-", dir=_FAST_DIR))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _engine_over_copy(template: Path, fast_dir: Path, name: str):
    dst = fast_dir / name
    shutil.copy(template, dst)
    return make_engine(dst, busy_timeout_ms=200)


@pytest.fixture
def repo_db(migrated_template, fast_dir) -> Path:
    """Per-test file copy of the migrated (unprovisioned) template."""
    dst = fast_dir / "entity.db"
    shutil.copy(migrated_template, dst)
    return dst


@pytest.fixture
def repo_engine(repo_db):
    """Guarded engine over a fresh migrated file (nothing provisioned yet)."""
    eng = make_engine(repo_db, busy_timeout_ms=200)
    yield eng
    eng.dispose()


@pytest.fixture
def fx(provisioned_template, fast_dir):
    """Fernside Instruments, the synthetic company most of the suite runs against (``repo_helpers``).

    A small instrument maker with a chart and three currencies, provisioned through the real API into the session
    template and copied per test.  It is fictional on purpose: Haddon is a general ledger, so the fixture most
    tests use must not be one particular real company (chunk G1 part (c))."""
    eng = _engine_over_copy(provisioned_template, fast_dir, "provisioned.db")
    try:
        yield load_fixture(eng)
    finally:
        eng.dispose()


@pytest.fixture
def seeded_engine(templates, fast_dir):
    """Engine over a copy of chunk A's seeded (unlocked) template, for tests that need matches/rows/assertions."""
    eng = _engine_over_copy(templates[False], fast_dir, "seeded.db")
    yield eng
    eng.dispose()


@pytest.fixture
def locked_seeded_engine(templates, fast_dir):
    """The same seeded data with the lock already at 2026-02-28, so a close through 2026-03-31 has a window of
    exactly one month.  Chunk E2's gate tests need that: eligibility is evaluated over the span a close would
    newly close, and an unlocked entity's first close spans every book."""
    eng = _engine_over_copy(templates[True], fast_dir, "seeded-locked.db")
    yield eng
    eng.dispose()


# Chunk C1: corpus imports.
# Inc and B.V. are imported once per session from the corpus journals with the pinned binary, into files on tmpfs
# when available.  Tests read those files; tests that need a fresh target use ``repo_db``.
from dataclasses import dataclass as _dataclass  # noqa: E402


@_dataclass(frozen=True)
class Imported:
    entity: str
    path: Path
    result: object  # haddon.importing.importer.ImportResult


def _import_once(tmp_path_factory, entity: str) -> Imported:
    from haddon.importing.importer import import_entity

    base = Path(tempfile.mkdtemp(prefix=f"haddon-import-{entity}-", dir=_FAST_DIR)) if _FAST_DIR else tmp_path_factory.mktemp(f"import-{entity}")
    path = base / f"{entity}.db"
    upgrade(path)
    result = import_entity(entity, path)
    _checkpoint(path)
    return Imported(entity, path, result)


@pytest.fixture(scope="session")
def imported_inc(tmp_path_factory) -> Imported:
    imp = _import_once(tmp_path_factory, "ExampleInc")
    yield imp
    shutil.rmtree(imp.path.parent, ignore_errors=True)


@pytest.fixture(scope="session")
def imported_bv(tmp_path_factory) -> Imported:
    imp = _import_once(tmp_path_factory, "ExampleBV")
    yield imp
    shutil.rmtree(imp.path.parent, ignore_errors=True)


def table_counts(path: Path) -> dict[str, int]:
    """Row count of every table (``Fixture.counts()`` shape) for zero-trace assertions.

    SQLite's own bookkeeping tables are excluded.  ``sqlite_sequence`` is the one that matters.  It holds the
    AUTOINCREMENT high-water mark for ``events`` and gains a row the first time anything is audited, which would
    make "this operation wrote nothing to the ledger" fail on a write the ledger did not make."""
    con = sqlite3.connect(path)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


@pytest.fixture(scope="session")
def migrated_counts(migrated_template) -> dict[str, int]:
    """Counts of a migrated, empty database: what a rolled-back import must leave behind."""
    return table_counts(migrated_template)


# Chunk D1: the REST API.
# The API tests drive the real ASGI app over a real migrated database holding the imported corpus books.  The
# things most likely to break (running balances across a page boundary, a cursor surviving a concurrent write, a
# year-scoping refusal) are invisible against a three-row fixture.  The corpus books are here for their scale;
# tests assert properties of them, not their figures (chunk G1 part (b)).  The session-scoped imports are copied
# per test so a test that writes cannot disturb one that reads.

from haddon.api.config import ApiConfig, CursorKeyring  # noqa: E402

API_TOKEN = "token-under-test"
API_ACTOR = "agent:api-test"
OTHER_TOKEN = "token-scoped-elsewhere"
SECOND_TOKEN = "token-of-a-second-caller"
SECOND_ACTOR = "agent:second-caller"
CURSOR_SECRET = "cursor-secret-under-test"
#: The keyring the fixtures serve with: configured, so cursors survive a restart, and holding one key, which is
#: what ``cursor_secret`` in a config file produces.  ``build_api(cursor_keys=...)`` overrides it.
CURSOR_KEYS = CursorKeyring.of(CURSOR_SECRET)


@_dataclass
class ApiFixture:
    """One running app over one or more entity files, plus the plumbing a test needs to look behind it."""

    client: object
    config: ApiConfig
    entity_id: str
    path: Path
    token: str = API_TOKEN

    @property
    def base(self) -> str:
        return f"/v1/entities/{self.entity_id}"

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    #: paths that are already absolute; anything else is taken as relative to this entity
    ABSOLUTE = ("/v1/", "/healthz", "/readyz", "/openapi")

    def url(self, path: str) -> str:
        return path if path.startswith(self.ABSOLUTE) else f"{self.base}{path}"

    def get(self, path: str, *, token: str | None = None, **kw):
        headers = {"Authorization": f"Bearer {token or self.token}", **kw.pop("headers", {})}
        return self.client.get(self.url(path), headers=headers, **kw)

    def post(self, path: str, *, key: str | None = None, token: str | None = None, **kw):
        headers = {"Authorization": f"Bearer {token or self.token}", **kw.pop("headers", {})}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.post(self.url(path), headers=headers, **kw)

    def json(self, path: str, **kw) -> dict:
        r = self.get(path, **kw)
        assert r.status_code == 200, (path, r.status_code, r.text[:400])
        return r.json()

    def engine(self):
        """A guarded writer engine on the same file, for the out-of-band writes an expiry test needs."""
        return make_engine(self.path, busy_timeout_ms=200)

    def read_engine(self):
        """A guarded reader on the same file.

        It takes no write lock, so a test can hold it open across requests the app is serving, which comparing a
        route against ``haddon.reports`` requires."""
        return make_engine(self.path, busy_timeout_ms=200, immediate=False)

    def raw(self) -> sqlite3.Connection:
        return sqlite_connect(self.path)

    def scalar(self, sql: str, params: tuple = ()):
        con = self.raw()
        try:
            return con.execute(sql, params).fetchone()[0]
        finally:
            con.close()

    def row(self, sql: str, params: tuple = ()):
        con = self.raw()
        try:
            return con.execute(sql, params).fetchone()
        finally:
            con.close()


def build_api(entities: dict[str, Path], *, scoped_to: list[str] | None = None, **overrides):
    """An ``ApiConfig`` + ``TestClient`` over the given entity files.

    ``API_TOKEN`` is scoped to ``scoped_to`` (default: every entity given).  ``SECOND_TOKEN`` is a different caller
    scoped to every entity given: idempotency keys are per caller, and cross-entity tests need a credential that
    can see both.  ``OTHER_TOKEN`` is a valid credential scoped to an entity this process does not serve."""
    from fastapi.testclient import TestClient

    from haddon.api import create_app
    from haddon.api.config import Credential

    config = ApiConfig(
        entities=dict(entities),
        credentials=(Credential(API_TOKEN, API_ACTOR, tuple(scoped_to if scoped_to is not None else entities)),
                     Credential(SECOND_TOKEN, SECOND_ACTOR, tuple(entities)),
                     Credential(OTHER_TOKEN, "agent:elsewhere", ("an-entity-that-is-not-served",))),
        **{"cursor_keys": CURSOR_KEYS, **overrides})
    return config, TestClient(create_app(config))


def _copy_import(imported, fast_dir: Path, name: str) -> tuple[str, Path]:
    dst = fast_dir / name
    shutil.copy(imported.path, dst)
    con = sqlite3.connect(dst)
    try:
        entity_id = con.execute("SELECT id FROM entities").fetchone()[0]
    finally:
        con.close()
    return entity_id, dst


@pytest.fixture
def api_inc(imported_inc, fast_dir):
    """An imported corpus book behind the real app: a whole chart, thousands of postings, one 2025 book.

    Deliberately not described by its numbers.  The fixture is here for its scale, and the figures it happens to
    have are pinned in one place, ``corpus/example-baselines.json``, the file the corpus manifest names (chunk G1
    part (b))."""
    entity_id, path = _copy_import(imported_inc, fast_dir, "api-inc.db")
    config, client = build_api({entity_id: path})
    with client:
        yield ApiFixture(client=client, config=config, entity_id=entity_id, path=path)


@pytest.fixture
def api_two_entities(imported_inc, imported_bv, fast_dir):
    """Inc and B.V. served by one process: the fixture cross-entity isolation is checked against."""
    inc_id, inc_path = _copy_import(imported_inc, fast_dir, "api-inc.db")
    bv_id, bv_path = _copy_import(imported_bv, fast_dir, "api-bv.db")
    config, client = build_api({inc_id: inc_path, bv_id: bv_path}, scoped_to=[inc_id])
    with client:
        yield (ApiFixture(client=client, config=config, entity_id=inc_id, path=inc_path),
               ApiFixture(client=client, config=config, entity_id=bv_id, path=bv_path))


@pytest.fixture
def api_small(provisioned_template, fast_dir):
    """The real app over Fernside Instruments' chart (``repo_helpers.CHART``) and nothing else.

    ``api_inc`` needs a real hledger import to build, which puts every test using it in the slow tier.  Chunk F1's
    endpoint tests are about refusals and one loaded statement, which need no corpus book, so they run against this
    and stay in the fast tier.
    """
    dst = fast_dir / "api-small.db"
    shutil.copy(provisioned_template, dst)
    con = sqlite3.connect(dst)
    try:
        entity_id = con.execute("SELECT id FROM entities").fetchone()[0]
    finally:
        con.close()
    config, client = build_api({entity_id: dst})
    with client:
        yield ApiFixture(client=client, config=config, entity_id=entity_id, path=dst)


@pytest.fixture(scope="session")
def corpus():
    """The corpus manifest this suite verifies against (issue 0006).

    A fixture rather than an import so a test names what it depends on.  The journal core's tests need no corpus;
    the importer's, the oracle's and the export's do.
    """
    from haddon.importing.corpus import load_corpus

    return load_corpus()
