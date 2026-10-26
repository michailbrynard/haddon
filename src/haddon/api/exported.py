"""Serving the journal files an export published, and every way that is refused.

``GET /exports/{id}`` describes the artifacts (``path``, ``sha256``, ``bytes``).  Chunk D1 deliberately served
none of them ("an authenticated download surface has its own rules and this chunk does not open one").  This is
that surface.  It applies the rules :mod:`haddon.api.evidence` worked out for a document's bytes to a file this
service wrote itself:

* Containment.  Bytes come only from under the configured ``export_root``, and only from this entity's
  subdirectory of it.  The directory an export landed in is named by ``journal_mappings``, which is book data
  written by an import, so without the check one book's rows could choose where another book's files are read.
* The recorded digest is the claim.  The ``exports`` row is immutable and lists each file's ``sha256`` and byte
  count, and the file is re-hashed against it before any byte is returned.  A journal that no longer hashes to
  what was verified is refused, not served with a warning: the row exists to say that those exact bytes passed
  ``hledger check --strict`` under the pinned binary.
* Nothing new is stored.  There is no artifact table and no blob store.  The row says which files, the snapshot's
  manifest says which directory, and the configuration says which root.  An export made by ``bin/haddon-export``
  into some other directory is therefore recorded but not downloadable here, rather than inventing a second
  storage model to make it so.

A refusal never names a filesystem path, for the same reason the document reads do not: the path is the server's
business, and revealing it is the disclosure containment exists to prevent.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from haddon.api.evidence import MAX_BYTES, Content
from haddon.domain.errors import EVIDENCE_UNAVAILABLE, HaddonError

#: An export's files are plain hledger journals.  This service wrote the bytes, so unlike a document's type this
#: is not a caller's claim, and ``text/plain`` may be rendered inline.
MEDIA_TYPE = "text/plain; charset=utf-8"

#: One directory or file name the manifest may name.  The export only writes ``<label>.journal``,
#: ``accounts.journal`` and ``<label>-<group>.journal`` under ``<Entity>/<label>/ledger``.  This pattern is wider
#: than that, but narrow enough that ``..``, an absolute path and a hidden name never match.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _refuse(reason: str, message: str, **detail) -> HaddonError:
    """Build a refusal shaped like ``evidence``'s: one of a closed set of reasons, never a path."""
    return HaddonError(EVIDENCE_UNAVAILABLE, message, details=[{"reason": reason, **detail}])


def artifact_named(artifacts: Any, name: str) -> dict[str, Any] | None:
    """Return the recorded artifact this name refers to, or ``None``.

    The lookup is in the row, not on disk, so a name the export never recorded is not found before any path is
    built from it.  The file name is a key into an immutable list, not a string this service goes looking for.
    """
    if not isinstance(artifacts, list):  # pragma: no cover - the column is written by record_export
        return None
    return next((a for a in artifacts if isinstance(a, dict) and a.get("path") == name), None)


def ledger_dir_of(manifest: Any) -> str:
    """Return where this export published its files, from the snapshot's manifest, checked segment by segment.

    The ``exports`` row records the file names but not the directory.  The directory is in the snapshot's
    manifest (``<Entity>/<label>/ledger``), the same document the manifest digest covers.  It is relative by
    construction, and is validated as relative here because it comes from book data.
    """
    if not isinstance(manifest, dict):
        raise _refuse("no_location", "this export did not record where it published its files")
    where = manifest.get("ledger_dir")
    if not isinstance(where, str) or not where:
        raise _refuse("no_location", "this export did not record where it published its files")
    parts = where.split("/")
    if not all(_SEGMENT.match(part) for part in parts):
        raise _refuse("no_location", "this export recorded a publication directory this service will not open")
    return where


def load(*, out_root: Path | None, ledger_dir: str, artifact: dict[str, Any], inline: bool = False) -> Content:
    """Return one published journal file, verified against the ``exports`` row, or refuse and say why.

    The order follows ``evidence.load``: containment before the file is opened, the hash before anything is
    returned, and the recorded byte count after the hash.  A hash match with a disagreeing size is a bookkeeping
    error in the row rather than a changed file, and a distinct refusal says so.
    """
    if out_root is None:
        raise _refuse("not_served",
                      "this installation does not serve exported journal files: no export root is configured")
    name = artifact["path"]
    if not _SEGMENT.match(name):
        raise _refuse("no_location", "this export recorded a file name this service will not open")

    root = out_root.resolve()
    resolved = root.joinpath(ledger_dir, name).resolve()
    if not resolved.is_relative_to(root):
        raise _refuse("outside_export_root",
                      "this export's files are not kept under the export root this service serves from")
    try:
        if resolved.stat().st_size > MAX_BYTES:
            raise _refuse("too_large",
                          f"this file is larger than the {MAX_BYTES} bytes this service will verify and serve "
                          f"in one response", size_bytes=resolved.stat().st_size, limit=MAX_BYTES)
        data = resolved.read_bytes()
    except FileNotFoundError:
        raise _refuse("missing",
                      "this export is recorded but its files are not where it published them") from None
    except OSError:
        raise _refuse("unreadable", "this export's files could not be read") from None

    actual = hashlib.sha256(data).hexdigest()
    if actual != artifact["sha256"]:
        raise _refuse("hash_mismatch",
                      "the bytes on disk no longer hash to what this export recorded, so they are not the "
                      "journal the verifier passed", recorded_sha256=artifact["sha256"], actual_sha256=actual)
    if len(data) != int(artifact["bytes"]):
        raise _refuse("size_mismatch",
                      "the bytes on disk hash correctly but are not the length this export recorded",
                      recorded_size_bytes=int(artifact["bytes"]), actual_size_bytes=len(data))
    return Content(data=data, media_type=MEDIA_TYPE, filename=name, inline=inline)
