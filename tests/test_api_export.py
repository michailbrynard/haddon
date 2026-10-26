"""``POST /exports`` and the download beside it: the journal, over the wire.

The export itself is chunk C2a's and is tested there (``test_export_render.py`` on a small book,
``test_export_corpus_books.py`` on the corpus books).  What is tested here is the transport and nothing else:

* the route runs the real exporter, so the files on disk are the ones ``hledger check --strict`` passed;
* a retry under the same key replays rather than exporting a second time, and the reply that is replayed was
  recorded in the same transaction as the export;
* the files come back byte-for-byte, verified against the digest the immutable ``exports`` row recorded, and
  only from under the configured export root;
* every refusal leaves the ledger exactly as it was, compared against a full table-count snapshot as the batch
  tests do.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from conftest import ApiFixture, build_api, table_counts

CASH = "assets:cash"
OFFICE = "expenses:office"


def key() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def exporting(provisioned_template, fast_dir):
    """Fernside Instruments behind the real app, with an export root configured and two entries posted.

    A book with nothing posted in it cannot be exported at all (an empty directory is not an export), so the
    fixture posts the smallest journal that is a journal.
    """
    import shutil

    dst = fast_dir.joinpath("api-export.db")
    shutil.copy(provisioned_template, dst)
    con = sqlite3.connect(dst)
    try:
        entity_id = con.execute("SELECT id FROM entities").fetchone()[0]
    finally:
        con.close()
    root = fast_dir.joinpath("exports")
    config, client = build_api({entity_id: dst}, export_root=root)
    with client:
        api = ApiFixture(client=client, config=config, entity_id=entity_id, path=dst)
        for dt, amount, narration in (("2026-03-05", "100.00", "March rent"),
                                      ("2026-03-09", "12.50", "a box of pens")):
            posted = api.post("/entries", key=key(), json={
                "dt": dt, "narration": narration, "reason": "the books need something in them to export",
                "legs": [{"account": OFFICE, "commodity": "USD", "amount": amount},
                         {"account": CASH, "commodity": "USD", "amount": f"-{amount}"}]})
            assert posted.status_code == 201, posted.text[:400]
        yield api, root


def run(api: ApiFixture, body: dict | None = None, *, k: str | None = None):
    return api.post("/exports", key=k or key(), json={} if body is None else body)


# The export runs
@pytest.mark.slow
def test_an_export_runs_and_publishes_exactly_the_files_it_records(exporting):
    """The exporter ran, the files are on disk, and the row describes those bytes."""
    api, root = exporting
    response = run(api)
    assert response.status_code == 201, response.text[:600]
    body = response.json()

    assert body["entity_id"] == api.entity_id
    assert (body["range_start"], body["range_end"]) == ("2026-03-05", "2026-03-09")
    assert body["verifier"]["result"] == "pass" and body["verifier"]["tool"] == "hledger"
    assert body["counts"]["entries"] == 2 and body["counts"]["postings"] == 4
    assert body["ledger_dir"] == f"{api.entity_id}/2026-03-05_2026-03-09/ledger"
    names = sorted(a["path"] for a in body["artifacts"])
    assert names == ["2026-03-05_2026-03-09-haddon.journal", "2026-03-05_2026-03-09.journal",
                     "accounts.journal"]

    published = root.joinpath(api.entity_id, body["ledger_dir"])
    assert sorted(p.name for p in published.iterdir()) == names
    for artifact in body["artifacts"]:
        on_disk = published.joinpath(artifact["path"]).read_bytes()
        assert len(on_disk) == artifact["bytes"]

    # One export, one snapshot, one operation that succeeded, one event.
    assert api.scalar("SELECT count(*) FROM exports") == 1
    assert api.scalar("SELECT state FROM operations WHERE kind='export'") == "succeeded"
    assert api.scalar("SELECT count(*) FROM events WHERE kind='export.completed'") == 1

    # The read-back route describes the same export.
    read = api.json(f"/exports/{body['export_id']}")
    assert read["manifest_sha256"] == body["manifest_sha256"]
    assert read["artifacts"] == [{k: a[k] for k in ("path", "sha256", "bytes")} for a in body["artifacts"]]
    assert read["links"]["snapshot"] == f"{api.base}/snapshots/{body['snapshot_id']}"


@pytest.mark.slow
def test_the_export_links_its_events_to_the_request_that_asked_for_it(exporting):
    """The events the export writes name the ``requests`` row that commits beside them.

    ``execute_once`` gives every other write this property; this route owns no transaction and has to keep it
    itself."""
    api, _ = exporting
    body = run(api).json()
    request_id = api.scalar("SELECT id FROM requests WHERE path LIKE '%/exports'")
    assert api.scalar("SELECT request_id FROM events WHERE kind='export.completed'") == request_id
    assert api.scalar("SELECT http_status FROM requests WHERE id=?", (request_id,)) == 201
    assert api.scalar("SELECT operation_id FROM requests WHERE id=?", (request_id,)) == body["operation_id"]


# Exactly once
@pytest.mark.slow
def test_a_retry_under_the_same_key_replays_and_does_not_export_again(exporting):
    """The reply is recorded inside the export's own transaction.

    A retry after a dropped connection must not render, verify and publish a second time.  Nor must it find a
    recorded export with no recorded reply, which a second transaction would leave a window for.
    """
    api, _ = exporting
    once = key()
    first = run(api, k=once)
    assert first.status_code == 201, first.text[:400]
    again = run(api, k=once)
    assert again.status_code == 201
    assert again.headers.get("Idempotency-Replayed") == "true"
    assert again.json() == first.json()
    assert api.scalar("SELECT count(*) FROM exports") == 1
    assert api.scalar("SELECT count(*) FROM requests WHERE path LIKE '%/exports'") == 1


@pytest.mark.slow
def test_the_same_key_with_a_different_range_is_a_conflict(exporting):
    api, _ = exporting
    once = key()
    assert run(api, k=once).status_code == 201
    clash = run(api, {"year": 2026}, k=once)
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_conflict"
    assert api.scalar("SELECT count(*) FROM exports") == 1


@pytest.mark.slow
def test_exporting_an_unchanged_book_again_reuses_its_snapshot(exporting):
    """Chunk C2a's byte-identity guarantee holds over the transport.  One state is one snapshot, so the second
    export names the first's snapshot and the same manifest digest."""
    api, root = exporting
    first = run(api).json()
    second = run(api).json()
    assert second["state_hash"] == first["state_hash"]
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["manifest_sha256"] == first["manifest_sha256"]
    assert second["export_id"] != first["export_id"]
    assert api.scalar("SELECT count(*) FROM exports") == 2
    assert api.scalar("SELECT count(*) FROM snapshots") == 1
    assert [a["sha256"] for a in second["artifacts"]] == [a["sha256"] for a in first["artifacts"]]


@pytest.mark.slow
def test_a_published_export_is_replaced_only_when_the_caller_says_so(exporting):
    """``overwrite`` is published and defaults to true; false is the caller asking to be told instead."""
    api, _ = exporting
    assert run(api).status_code == 201
    refused = run(api, {"overwrite": False})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"][0]["reason"] == "exists"
    assert api.scalar("SELECT count(*) FROM exports") == 1


# Refusals
@pytest.mark.slow
def test_a_range_with_nothing_posted_in_it_is_refused_and_leaves_no_trace(exporting):
    api, root = exporting
    before = table_counts(api.path)
    refused = run(api, {"year": 1999})
    assert refused.status_code == 422, refused.text[:400]
    assert refused.json()["error"]["code"] == "validation"
    assert table_counts(api.path) == before
    assert not root.exists() or not any(root.rglob("*.journal"))


def test_year_cannot_be_combined_with_a_bound(exporting):
    api, _ = exporting
    refused = run(api, {"year": 2026, "from": "2026-03-01"})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"][0]["reason"] == "range_conflict"
    assert api.scalar("SELECT count(*) FROM exports") == 0


def test_an_unknown_field_is_refused_by_the_published_schema(exporting):
    api, _ = exporting
    assert run(api, {"out_root": "/tmp/anywhere"}).status_code == 422


def test_an_installation_with_no_export_root_refuses_and_writes_nothing(api_small):
    """The deployment decides where exports go.  Unset is not "choose one for me"."""
    before = table_counts(api_small.path)
    refused = api_small.post("/exports", key=key(), json={})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"][0]["reason"] == "export_root_unset"
    assert table_counts(api_small.path) == before


# The download
@pytest.mark.slow
def test_every_recorded_artifact_comes_back_byte_for_byte(exporting):
    api, root = exporting
    body = run(api).json()
    published = root.joinpath(api.entity_id, body["ledger_dir"])
    for artifact in body["artifacts"]:
        served = api.get(artifact["href"])
        assert served.status_code == 200, (artifact["path"], served.text[:300])
        assert served.content == published.joinpath(artifact["path"]).read_bytes()
        assert served.headers["etag"] == f'"{artifact["sha256"]}"'
        assert artifact["path"] in served.headers["content-disposition"]
        assert served.headers["x-content-type-options"] == "nosniff"
        assert served.headers["content-type"].startswith("text/plain")
    # The journal is what hledger reads: the main file names the others.
    main = api.get(f"{api.base}/exports/{body['export_id']}/artifacts/2026-03-05_2026-03-09.journal")
    assert "include accounts.journal" in main.text


@pytest.mark.slow
def test_a_file_that_no_longer_hashes_to_what_was_recorded_is_refused(exporting):
    """The digest on the immutable row is the claim.  A published journal somebody edited is not this export."""
    api, root = exporting
    body = run(api).json()
    artifact = next(a for a in body["artifacts"] if a["path"] == "accounts.journal")
    published = root.joinpath(api.entity_id, body["ledger_dir"], "accounts.journal")
    published.write_text(published.read_text() + "account assets:smuggled\n", encoding="utf-8")

    refused = api.get(artifact["href"])
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "evidence_unavailable"
    assert error["details"][0]["reason"] == "hash_mismatch"
    assert str(root) not in refused.text  # a refusal never names a path on this machine


@pytest.mark.slow
def test_a_file_the_export_did_not_record_is_not_found(exporting):
    api, _ = exporting
    body = run(api).json()
    missing = api.get(f"{api.base}/exports/{body['export_id']}/artifacts/passwd")
    assert missing.status_code == 404
    assert missing.json()["error"]["details"][0]["reason"] == "unknown_artifact"


@pytest.mark.slow
def test_an_export_whose_files_are_gone_says_so_without_naming_them(exporting):
    api, root = exporting
    body = run(api).json()
    artifact = body["artifacts"][0]
    for path in root.rglob("*.journal"):
        path.unlink()
    refused = api.get(artifact["href"])
    assert refused.status_code == 409
    assert refused.json()["error"]["details"][0]["reason"] == "missing"
    assert str(root) not in refused.text


def test_an_export_recorded_by_the_machine_is_read_back_but_not_downloadable(api_small):
    """An export ``bin/haddon-export`` published into an operator's own directory is a real row here and its
    bytes are somewhere this service does not serve from.  Refusing is the honest answer, not a second way to
    read files off this machine."""
    from haddon.repository import closing, exports, requests
    from haddon.repository.ctx import WriteCtx
    from haddon.repository.transaction import write_transaction

    engine = api_small.engine()
    try:
        with write_transaction(engine) as conn:
            ctx = WriteCtx.open(conn, "agent:on-the-machine")
            snap = closing.create_snapshot(ctx, through="2026-03-31", state_hash="elsewhere",
                                           manifest={"ledger_dir": "Fernside/2026/ledger"},
                                           report={}, checks={})
            op = requests.create_operation(ctx, kind="export", intent={"kind": "export"})
            exp = exports.record_export(ctx, snapshot_id=snap.id, operation_id=op.id,
                                        manifest_sha256="a" * 64, verifier={"result": "pass"},
                                        artifacts=[{"path": "2026.journal", "sha256": "b" * 64, "bytes": 10}])
    finally:
        engine.dispose()

    assert api_small.json(f"/exports/{exp.id}")["id"] == exp.id
    refused = api_small.get(f"/exports/{exp.id}/artifacts/2026.journal")
    assert refused.status_code == 409
    assert refused.json()["error"]["details"][0]["reason"] == "not_served"


def test_an_artifact_of_an_unknown_export_is_404(api_small):
    assert api_small.get("/exports/exp-nope/artifacts/2026.journal").status_code == 404


# Containment
def test_the_export_root_is_confined_to_one_directory_per_entity(fast_dir):
    """A book's own rows name the directory it exports into (``journal_mappings``), so two books sharing one
    root would let one of them name the other's."""
    from haddon.api.config import ApiConfig

    config = ApiConfig(entities={}, export_root=Path("/srv/exports"))
    assert config.export_dir_for("example-bv") == Path("/srv/exports/example-bv")
    assert config.export_refusal() is None
    assert ApiConfig(entities={}).export_refusal() is not None


def test_the_export_root_is_read_out_of_the_configuration_file_like_every_other_root(tmp_path):
    """A relative ``export_root`` resolves against the configuration file that named it, so a deployment stays
    movable.  ``blob_root`` and ``entity_root`` follow the same rule."""
    from haddon.api.config import load_config

    config_file = tmp_path.joinpath("haddon.json")
    config_file.write_text('{"entities": {"acme": "acme.db"}, "export_root": "exports"}', encoding="utf-8")
    loaded = load_config(config_file)
    assert loaded.export_root == tmp_path.joinpath("exports")
    assert loaded.export_dir_for("acme") == tmp_path.joinpath("exports", "acme")


def test_a_ledger_directory_the_manifest_did_not_name_plainly_is_refused():
    """The publication directory comes out of a manifest built from book data, so it is checked as data."""
    from haddon.api import exported
    from haddon.domain.errors import HaddonError

    assert exported.ledger_dir_of({"ledger_dir": "Fernside/2026/ledger"}) == "Fernside/2026/ledger"
    for bad in ({}, {"ledger_dir": ""}, {"ledger_dir": "../../etc"}, {"ledger_dir": "/etc/passwd"},
                {"ledger_dir": "Fernside/../../etc"}, "not a manifest"):
        with pytest.raises(HaddonError) as raised:
            exported.ledger_dir_of(bad)
        assert raised.value.details[0]["reason"] == "no_location"
