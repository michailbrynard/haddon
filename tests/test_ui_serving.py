"""Serving the reviewer bundle out of the same process that serves ``/v1``.

The claims a reverse-proxy config cannot make on its own, and that would each break something specific:

* A client-side route survives a reload.  ``/e/<entity>/month`` exists only in the browser, so the service
  hands back the entry document for it.  Without this, every link into a month is a 404 on first load, which is
  the only kind of link a reviewer actually sends anybody.
* The fallback never answers for the API.  An unknown path under ``/v1`` stays a refusal in the API's own
  envelope.  Returning HTML there turns a typo into a JSON parse error three layers away from the mistake.
* ``/v1`` did not open.  Adding an HTML surface to this app must not make the API reachable without a
  credential.
* The interface is not in the published contract.  ``/openapi.json`` describes the API; a set of HTML
  routes in it would be describing something no caller can use.
* A missing build says so.  With no bundle on disk the page explains that ``make web`` has not been run,
  rather than 404ing as though the service were broken.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from haddon.ui import routes as ui

from conftest import API_TOKEN, build_api

pytestmark = pytest.mark.slow

ENTITY = "fernside-instruments"
MARKER = "<!doctype html><title>bundle</title><div id=root></div>"


@pytest.fixture
def entity_file(provisioned_template, fast_dir) -> Path:
    path = fast_dir / "ui.db"
    shutil.copy(provisioned_template, path)
    return path


def serve(monkeypatch, entity_file: Path, static: Path | None):
    """The app with the bundle directory pointed wherever this test wants it, including nowhere."""
    root = static if static is not None else Path("/nonexistent-bundle")
    monkeypatch.setattr(ui, "STATIC", root)
    monkeypatch.setattr(ui, "INDEX", root.joinpath("index.html"))
    monkeypatch.setattr(ui, "ASSETS", root.joinpath("assets"))
    return build_api({ENTITY: entity_file})[1]


@pytest.fixture
def built(tmp_path) -> Path:
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(MARKER, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("export default 1\n", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return root


# with a bundle on disk
def test_the_root_serves_the_entry_document(monkeypatch, entity_file, built):
    with serve(monkeypatch, entity_file, built) as client:
        response = client.get("/")
    assert response.status_code == 200 and MARKER in response.text


@pytest.mark.parametrize("path", ["/e/fernside-instruments/month",
                                  "/e/fernside-instruments/accounts/acct-1",
                                  "/e/fernside-instruments/lines/row-9?month=2026-03"])
def test_a_client_side_route_survives_a_reload(monkeypatch, entity_file, built, path):
    with serve(monkeypatch, entity_file, built) as client:
        response = client.get(path)
    assert response.status_code == 200 and MARKER in response.text


def test_the_entry_document_is_never_cached_but_its_hashed_assets_are(monkeypatch, entity_file, built):
    """The filenames under ``assets/`` carry a content hash, so a year is safe.  ``index.html`` names this
    deploy's hashes, so caching it would make a deploy invisible until somebody cleared a cache."""
    with serve(monkeypatch, entity_file, built) as client:
        index = client.get("/")
        asset = client.get("/assets/index-abc123.js")
    assert index.headers["cache-control"] == "no-cache"
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]


def test_an_unknown_path_under_v1_stays_a_refusal_in_the_apis_own_envelope(monkeypatch, entity_file, built):
    with serve(monkeypatch, entity_file, built) as client:
        response = client.get("/v1/entities/fernside-instruments/no-such-thing",
                              headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_found"


def test_the_health_probes_are_still_json(monkeypatch, entity_file, built):
    with serve(monkeypatch, entity_file, built) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/readyz").headers["content-type"].startswith("application/json")


def test_adding_an_html_surface_did_not_open_the_api(monkeypatch, entity_file, built):
    with serve(monkeypatch, entity_file, built) as client:
        assert client.get(f"/v1/entities/{ENTITY}/accounts").status_code == 401


def test_the_interface_is_not_in_the_published_contract(monkeypatch, entity_file, built):
    with serve(monkeypatch, entity_file, built) as client:
        document = client.get("/openapi.json").json()
    assert not any(p.startswith("/e/") for p in document["paths"])
    assert "/{path}" not in document["paths"]
    assert all(not p.startswith("/assets") for p in document["paths"])


# with no bundle on disk
def test_a_missing_build_says_which_command_was_not_run(monkeypatch, entity_file):
    with serve(monkeypatch, entity_file, None) as client:
        response = client.get("/")
    assert response.status_code == 503
    assert "make web" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_a_missing_build_does_not_take_the_api_down_with_it(monkeypatch, entity_file):
    with serve(monkeypatch, entity_file, None) as client:
        assert client.get("/healthz").status_code == 200
        body = client.get(f"/v1/entities/{ENTITY}/accounts",
                          headers={"Authorization": f"Bearer {API_TOKEN}"})
    assert body.status_code == 200
