"""Fixtures for the browser harness: fresh books, a served bundle, and one page.

This lives outside `tests/` on purpose (see `browser/README.md`).  `pyproject.toml` sets `testpaths = ["tests"]`,
so the commit gate never collects this directory and `make test-gate` does not depend on a downloaded browser and
a built bundle.  Run it with `bin/haddon-browser-check`.

The books, the server and the browser are built once per session and shared by the tests that use them.  A
check that needs books in another state builds its own (see ``page``).  A failing run leaves a screenshot and the page's console output under `browser/artifacts/`, because the first
question about a browser failure is always "what did it actually look like".
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE.joinpath("artifacts")


def pytest_addoption(parser):
    parser.addoption("--headed", action="store_true", help="show the browser")
    parser.addoption("--slowmo", type=int, default=0, help="milliseconds to pause between actions")


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    from world import build

    root = tmp_path_factory.mktemp("haddon-browser")
    return build(root.joinpath("entity.db"), root.joinpath("blobs"))


@pytest.fixture(scope="session")
def served(world):
    from server import Harness

    with Harness({world.entity_id: world.db_path}, blob_root=world.blobs,
                 export_root=world.db_path.parent.joinpath("exports")) as running:
        yield running


@pytest.fixture(scope="session")
def browser(pytestconfig):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        instance = play.chromium.launch(headless=not pytestconfig.getoption("--headed"),
                                        slow_mo=pytestconfig.getoption("--slowmo"))
        yield instance
        instance.close()


@pytest.fixture
def page(browser, request):
    """One page, with the console and every failed request recorded so a failure can be read afterwards.

    Deliberately not tied to `served`: a check that needs books in a different state brings its own server
    (`test_statement_replacement.py` does, because a replacement into a closed period is refused and `world.py`
    ends closed).  A test asks for the server it wants; this fixture only ever supplies the page.
    """
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    log: list[str] = []
    page.on("console", lambda m: log.append(f"console.{m.type}: {m.text}"))
    page.on("pageerror", lambda e: log.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: log.append(f"requestfailed: {r.method} {r.url} {r.failure}"))
    page.on("response", lambda r: log.append(f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None)
    page.set_default_timeout(15_000)
    yield page
    if request.node.rep_call is not None and request.node.rep_call.failed:
        shutil.rmtree(ARTIFACTS, ignore_errors=True)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACTS.joinpath("failure.png")), full_page=True)
        ARTIFACTS.joinpath("page.html").write_text(page.content(), encoding="utf-8")
        ARTIFACTS.joinpath("console.log").write_text("\n".join(log) + "\n", encoding="utf-8")
        print(f"\nthe page at failure: {ARTIFACTS}")
        print("\n".join(log[-40:]))
    context.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Let the `page` fixture know whether the test it served failed, so it only writes artifacts when they help."""
    outcome = yield
    setattr(item, f"rep_{call.when}", outcome.get_result())
    for phase in ("setup", "call", "teardown"):
        if not hasattr(item, f"rep_{phase}"):
            setattr(item, f"rep_{phase}", None)
