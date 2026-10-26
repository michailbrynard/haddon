"""Serving the bytes a booking rests on, and the ways that is refused.

Until issue 0007 the reviewer interface showed a document as a hash and a ``blob_path``, a location on the
server's disk the reader cannot reach.  That looks like evidence, cannot be used as evidence, and tells a public
page where the service keeps files.  So the path left every response (``views`` no longer emits it) and was
replaced by one read, ``GET /documents/{id}/content``, that hands over the actual file.

Ingestion still does not read the bytes (``decisions.md``, "Ingestion records what it is told about the bytes,
and does not read them"), and this does not weaken that.  Registration records a claim, and this is where the
claim is checked, at the moment somebody relies on it.  The recorded ``media_type`` and ``size_bytes`` are never
trusted over what is on disk, and a file whose bytes no longer hash to the recorded ``sha256`` is refused rather
than served with a warning.  A reviewer handed a document gets the document that was registered.

Haddon records authority and does not enforce it (``decisions.md``, "Authorisation").  There is no role model,
so there is nothing finer to check than what the credential already passed to reach an entity-scoped route.
What this module adds is containment, which is what matters for a file read.  A ``blob_path`` is a string
somebody wrote into a request, and honouring it as a path would let a registration name ``/etc/passwd`` and a
later read serve it.  So bytes are served only from under a configured ``blob_root``, resolved through symlinks,
and an installation that configures no root serves no content at all.

Inline rendering is an allowlist.  Being able to look at a document without downloading it is most of the value,
but the media type is caller-supplied, so rendering a file as the type it claims to be is a stored-XSS surface on
a same-origin page.  Only types that cannot execute are served ``inline``.  Everything else is an attachment,
which no browser renders whatever it claims to be.  ``nosniff`` is set on both, because otherwise a browser may
decide for itself that a ``text/csv`` is HTML.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from haddon.domain.errors import EVIDENCE_UNAVAILABLE, HaddonError

#: Media types served ``inline`` when asked for: a browser renders these and none of them can execute.
#: ``image/svg+xml`` is deliberately absent: SVG can host scripts, the one image type that can.
INLINE_SAFE = frozenset({
    "application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp",
    "text/plain", "text/csv",
})

#: The largest file this will read into memory to verify.  A hash cannot be checked before the last byte, so a
#: file too big to hold is refused rather than streamed unverified.
MAX_BYTES = 64 * 1024 * 1024


#: En dash, em dash, curly quotes and a non-breaking space, mapped to ASCII equivalents.  Folding beats dropping:
#: ``1 January – 16 September`` should not come back as ``1 January  16 September``.
_ASCII_FOLD = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'",
                             "\u201c": '"', "\u201d": '"', "\u00a0": " "})


def content_disposition(filename: str, *, inline: bool = False, fallback: str = "download") -> str:
    """Build a ``Content-Disposition`` header (RFC 6266/5987) that survives any filename.

    Ordinary names break a header in two ways.  In ``rechnung "märz".pdf`` the quote would end the
    ``filename="..."`` parameter early.  The ``ä``, or the en dash in a statement titled ``1 January – 16
    September 2026``, cannot go in a header at all, and a raw non-ASCII byte raises an exception on the way out
    of the response, after the document was built.  Both downloads this service offers use this function.

    The plain parameter is reduced to printable ASCII without quotes or backslashes, so a filename cannot end the
    quoted string and write the rest of the header.  The ``filename*`` form carries the real name, which is the
    one current browsers use.
    """
    safe = "".join(c for c in filename.translate(_ASCII_FOLD)
                   if c.isascii() and c.isprintable() and c not in '"\\') or fallback
    return f'{"inline" if inline else "attachment"}; filename="{safe}"; ' \
           f"filename*=UTF-8''{quote(filename, safe='')}"


@dataclass(frozen=True)
class Content:
    """Verified bytes and the three headers that decide what a browser does with them."""

    data: bytes
    media_type: str
    filename: str
    inline: bool

    @property
    def disposition(self) -> str:
        return content_disposition(self.filename, inline=self.inline, fallback="document")


def _refuse(reason: str, message: str, **detail) -> HaddonError:
    """Build a refusal.  All refusals here share one shape and carry no path.

    The reason codes are a closed set the interface renders its own sentence for; the message is what an agent
    reads.  Neither ever contains ``blob_path`` or anything derived from it, not even in the "missing" case,
    where naming the file would be most helpful and is exactly the disclosure this read exists to prevent.
    """
    return HaddonError(EVIDENCE_UNAVAILABLE, message, details=[{"reason": reason, **detail}])


def _resolved(blob_path: str, root: Path) -> Path:
    """Return where ``blob_path`` actually lands, refusing anything outside the root.

    ``blob_path`` is absolute in every real registration, so it is resolved as given and then checked against
    the root, not joined to it.  Joining an absolute path silently discards the root, and the containment check
    would then pass by doing nothing.  A relative path is taken as relative to the root, the only unambiguous
    reading.

    ``resolve()`` follows symlinks, so a link inside the root pointing outside it resolves outside and is
    refused.  Checking the unresolved path instead is the classic version of this bug.
    """
    candidate = Path(blob_path)
    resolved = (candidate if candidate.is_absolute() else root.joinpath(candidate)).resolve()
    if not resolved.is_relative_to(root):
        raise _refuse("outside_evidence_root",
                      "this document's bytes are not kept under the evidence root this service serves from")
    return resolved


def load(*, sha256: str, filename: str, media_type: str, size_bytes: int, blob_path: str,
         external_ref: str | None, blob_root: Path | None, inline: bool) -> Content:
    """Return the registered document's bytes, verified against the registration, or refuse and say why.

    Containment is checked before the file is opened, and the hash before anything is returned.  The recorded
    size is checked after the hash: a hash match with a disagreeing size is a bookkeeping error in the
    registration, not a tampered file, and gets its own refusal.
    """
    if external_ref:
        raise _refuse("held_elsewhere",
                      "this document's bytes are held elsewhere; this service holds only the reference",
                      external_ref=external_ref)
    if blob_root is None:
        raise _refuse("not_served",
                      "this installation does not serve document content: no evidence root is configured")
    if not blob_path:
        raise _refuse("no_content", "this document has no bytes recorded against it")

    resolved = _resolved(blob_path, blob_root.resolve())
    try:
        if resolved.stat().st_size > MAX_BYTES:
            raise _refuse("too_large",
                          f"this document is larger than the {MAX_BYTES} bytes this service will verify and "
                          f"serve in one response", size_bytes=resolved.stat().st_size, limit=MAX_BYTES)
        data = resolved.read_bytes()
    except FileNotFoundError:
        raise _refuse("missing", "this document is registered but its bytes are not where they were kept") from None
    except OSError:
        raise _refuse("unreadable", "this document's bytes could not be read") from None

    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise _refuse("hash_mismatch",
                      "the bytes kept for this document no longer hash to what was registered, so they are "
                      "not the document this entry rests on", registered_sha256=sha256, actual_sha256=actual)
    if len(data) != size_bytes:
        raise _refuse("size_mismatch",
                      "the bytes kept for this document hash correctly but are not the length that was "
                      "registered", registered_size_bytes=size_bytes, actual_size_bytes=len(data))

    served = media_type if media_type in INLINE_SAFE else "application/octet-stream"
    return Content(data=data, media_type=served, filename=filename,
                   inline=inline and media_type in INLINE_SAFE)
