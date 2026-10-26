"""Issue 0007: the document a booking rests on, served to the person reviewing it, and the ways it is not.

A hash and a server-side path are not evidence a reviewer can use.  The hash only verifies bytes you already
have, and the path names a file on a machine the reviewer has no account on.  Publishing the path also tells
whoever loads the page where the service keeps files.  So no response carries the path (``test_api_documents``,
``test_statements_shape``); this route serves the bytes instead.  The tests are in two halves, and the second is
larger on purpose:

* it serves the document, with headers that decide what a browser does with it, and it verifies the bytes
  against the hash they were registered under before returning any of them; and
* it refuses, five ways, and no refusal ever contains a filesystem path, not even the one where naming the
  missing file would be the most useful thing to say.

The containment test is the one that matters most.  ``blob_path`` is a string a caller wrote into a
registration.  Serving it as a path, unconfined, would turn ``POST /documents`` into a file-read primitive
against the whole host: register ``/etc/passwd`` with its real hash, then read it back through a ledger.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import API_TOKEN, ApiFixture, build_api

MEDIA = "application/pdf"
BYTES = b"%PDF-1.4\nfictional supplier invoice, for the harness only\n"
SHA = hashlib.sha256(BYTES).hexdigest()


def document(**overrides) -> dict:
    return {"sha256": SHA, "filename": "invoice-24267.pdf", "media_type": MEDIA, "size_bytes": len(BYTES),
            "blob_path": "", "reason": "the supplier's invoice for this cost, filed with the books",
            **overrides}


@pytest.fixture
def blobs(tmp_path) -> Path:
    root = tmp_path.joinpath("blobs")
    root.mkdir()
    return root


@pytest.fixture
def served(provisioned_template, fast_dir, blobs):
    """The app over the small chart, configured to serve document content out of ``blobs``."""
    dst = fast_dir / "api-evidence.db"
    shutil.copy(provisioned_template, dst)
    con = sqlite3.connect(dst)
    try:
        entity_id = con.execute("SELECT id FROM entities").fetchone()[0]
    finally:
        con.close()
    config, client = build_api({entity_id: dst}, blob_root=blobs)
    with client:
        yield ApiFixture(client=client, config=config, entity_id=entity_id, path=dst)


@pytest.fixture
def unserved(provisioned_template, fast_dir):
    """The same app with no evidence root: the default, and an installation that serves no content."""
    dst = fast_dir / "api-evidence-off.db"
    shutil.copy(provisioned_template, dst)
    con = sqlite3.connect(dst)
    try:
        entity_id = con.execute("SELECT id FROM entities").fetchone()[0]
    finally:
        con.close()
    config, client = build_api({entity_id: dst})
    with client:
        yield ApiFixture(client=client, config=config, entity_id=entity_id, path=dst)


def register(api: ApiFixture, body: dict, *, key: str | None = None) -> dict:
    """Registered with a key derived from the body.  An idempotency key is per request, so reusing one across
    two different documents is a 409."""
    response = api.post("/documents", json=body,
                        key=key or "doc-" + hashlib.sha256(repr(sorted(body.items())).encode()).hexdigest()[:24])
    assert response.status_code in (200, 201), response.text
    return response.json()["document"]


def stored(api: ApiFixture, root: Path, *, name: str = "invoice.pdf", data: bytes = BYTES, **overrides) -> dict:
    """A document registered against real bytes sitting under the evidence root."""
    path = root.joinpath(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return register(api, document(blob_path=str(path), **overrides))


def refusal(response) -> dict:
    assert response.status_code == 409, (response.status_code, response.text[:400])
    error = response.json()["error"]
    assert error["code"] == "evidence_unavailable", error
    return error


def no_path_anywhere(error: dict, *needles: str) -> None:
    """The whole envelope, flattened, contains nothing that looks like where the file is.

    Checked against the serialised envelope rather than against the fields this test happens to know about,
    because the failure mode is a path arriving somewhere nobody thought to look: a ``message`` built by
    interpolating the path, a detail key added later, an exception string passed through.
    """
    import json

    flat = json.dumps(error)
    for needle in ("/tmp", "blob_path", *needles):
        assert needle not in flat, f"{needle!r} is in the refusal: {flat}"


# It hands over the actual document
def test_the_document_is_served_with_its_bytes_intact(served, blobs):
    doc = stored(served, blobs)
    response = served.get(f"/documents/{doc['id']}/content")
    assert response.status_code == 200, response.text
    assert response.content == BYTES


def test_a_document_downloads_rather_than_renders_unless_inline_is_safe(served, blobs):
    """The default is an attachment, which no browser renders whatever the media type claims.

    ``inline`` is honoured here because a PDF cannot execute.  The test below is the other half of the rule.
    """
    doc = stored(served, blobs)
    default = served.get(f"/documents/{doc['id']}/content")
    assert default.headers["content-disposition"].startswith("attachment;")
    assert default.headers["x-content-type-options"] == "nosniff"

    inline = served.get(f"/documents/{doc['id']}/content?disposition=inline")
    assert inline.headers["content-disposition"].startswith("inline;")
    assert inline.headers["content-type"].startswith(MEDIA)


def test_a_media_type_that_can_execute_is_never_rendered_inline(served, blobs):
    """The media type is caller-supplied, so "render it as what it claims to be" is a stored-XSS surface on a
    same-origin page.  An SVG is the sharp case: it is an image and it is a script host.

    Both halves are asserted: the disposition stays an attachment and the served content type is not the type
    that was claimed.  Either one alone lets a browser be talked into executing the file.
    """
    for claimed in ("text/html", "image/svg+xml", "application/javascript"):
        doc = stored(served, blobs, name=f"{claimed.replace('/', '-')}.bin",
                     data=b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>",
                     media_type=claimed,
                     sha256=hashlib.sha256(
                         b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>").hexdigest(),
                     size_bytes=len(b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>"))
        response = served.get(f"/documents/{doc['id']}/content?disposition=inline")
        assert response.status_code == 200, response.text
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-type"].startswith("application/octet-stream")


def test_a_filename_cannot_break_out_of_the_content_disposition_header(served, blobs):
    """A document called ``rechnung "märz".pdf`` is an ordinary document.  It must not be able to end the
    header early, and its real name must still survive, which is what the RFC 5987 ``filename*`` is for."""
    doc = stored(served, blobs, filename='rechnung "märz".pdf')
    header = served.get(f"/documents/{doc['id']}/content").headers["content-disposition"]
    assert '"' not in header.split("filename=")[1].split(";")[0].strip('"')
    assert "filename*=UTF-8''" in header and "m%C3%A4rz" in header


def test_the_reply_is_cacheable_only_privately_and_tagged_by_content(served, blobs):
    """A document is content-addressed, so its id never legitimately answers different bytes.  But it is also
    somebody's invoice, and a shared cache is not where it belongs."""
    doc = stored(served, blobs)
    headers = served.get(f"/documents/{doc['id']}/content").headers
    assert headers["etag"] == f'"{SHA}"'
    assert headers["cache-control"].startswith("private")


# And the five refusals
def test_bytes_outside_the_evidence_root_are_refused(served, blobs, tmp_path):
    """The containment rule, and the reason this read is confined at all.

    ``blob_path`` comes from a caller.  Without this, registering a document that names any file on the host
    and then reading it back would be a file-read primitive wearing a ledger's clothes.
    """
    outside = tmp_path.joinpath("not-evidence.pdf")
    outside.write_bytes(BYTES)
    doc = register(served, document(blob_path=str(outside)))
    error = refusal(served.get(f"/documents/{doc['id']}/content"))
    assert error["details"][0]["reason"] == "outside_evidence_root"
    no_path_anywhere(error, "not-evidence")


def test_a_symlink_out_of_the_evidence_root_is_refused(served, blobs, tmp_path):
    """Containment is checked on the resolved path.  Checking the path as written is the classic version of
    this bug: ``blobs/escape.pdf`` is inside the root right up until you follow it."""
    outside = tmp_path.joinpath("elsewhere.pdf")
    outside.write_bytes(BYTES)
    link = blobs.joinpath("escape.pdf")
    link.symlink_to(outside)
    doc = register(served, document(blob_path=str(link)))
    error = refusal(served.get(f"/documents/{doc['id']}/content"))
    assert error["details"][0]["reason"] == "outside_evidence_root"
    no_path_anywhere(error, "elsewhere")


def test_bytes_that_no_longer_hash_to_what_was_registered_are_refused(served, blobs):
    """Registration records a claim about the bytes; this is where the claim is checked.

    A file replaced since it was registered is not the document the entry rests on.  Serving it with a warning
    would leave the reviewer holding something that looks like evidence and is not.
    """
    doc = stored(served, blobs)
    blobs.joinpath("invoice.pdf").write_bytes(BYTES + b"and one more line nobody registered\n")
    error = refusal(served.get(f"/documents/{doc['id']}/content"))
    assert error["details"][0]["reason"] == "hash_mismatch"
    assert error["details"][0]["registered_sha256"] == SHA
    no_path_anywhere(error)


def test_a_missing_file_says_so_without_saying_where_it_was(served, blobs):
    """The case where naming the file is the most helpful thing to do, and is the disclosure being prevented."""
    doc = stored(served, blobs)
    blobs.joinpath("invoice.pdf").unlink()
    error = refusal(served.get(f"/documents/{doc['id']}/content"))
    assert error["details"][0]["reason"] == "missing"
    no_path_anywhere(error, "invoice.pdf")


def test_a_document_held_elsewhere_names_the_reference_and_not_a_path(served):
    """``external_ref`` documents have no bytes here, and the reference is the useful thing to return: it is
    what somebody chases the document with."""
    doc = register(served, document(blob_path="", external_ref="dms://supplier/24267"))
    assert doc["storage"] == "external" and "content" not in doc["links"]
    error = refusal(served.get(f"/documents/{doc['id']}/content"))
    assert error["details"][0]["reason"] == "held_elsewhere"
    assert error["details"][0]["external_ref"] == "dms://supplier/24267"


def test_an_installation_with_no_evidence_root_serves_no_content(unserved):
    """The default.  A service that has never needed to open a blob opens none, and says so in one sentence
    rather than by failing to find a file."""
    doc = register(unserved, document(blob_path="/var/haddon/blobs/invoice.pdf"))
    error = refusal(unserved.get(f"/documents/{doc['id']}/content"))
    assert error["details"][0]["reason"] == "not_served"
    assert "content" in doc["links"], "the link is about the document's storage, not the installation's config"


# And the ordinary entity rules
def test_a_document_from_another_entity_is_a_plain_not_found(served, blobs):
    doc = stored(served, blobs)
    assert served.get("/documents/dc_not_here/content").status_code == 404
    assert served.get(f"/documents/{doc['id']}/content", token=API_TOKEN).status_code == 200


def test_the_content_read_is_published_like_every_other_read(served, blobs):
    """The interface gates this off ``/capabilities``, so it has to be listed there.  It is, without anybody
    adding it, because the surface is derived from the mounted routes (issue 0006)."""
    reads = served.json("/capabilities")["reads"]
    assert any(r.endswith("/documents/{document_id}/content") for r in reads), reads
